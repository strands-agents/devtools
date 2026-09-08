"""Strandly dashboard API — read the run-ledger, and chat with the deployed agent.

Routes (behind an API Gateway HTTP API with a Cognito JWT authorizer, except ``/config``):

    GET  /api/config           public — Cognito settings the SPA needs to start the OAuth2/PKCE login
    GET  /api/overview         aggregate stats over a recent window (counts, success rate, tokens)
    GET  /api/runs             recent runs, newest-first (Query on the ``recent`` GSI, no Scan)
    GET  /api/runs/{id}        one run by task id
    GET  /api/sessions         recent runs grouped into sessions, newest-first (with a description)
    GET  /api/health           CloudWatch alarm states + mention-poller liveness (gated on
                               ``ALARM_NAME_PREFIX``; unset → reports itself unconfigured)
    GET  /api/sessions/{id}    one session's transcript — the verbatim conversation from AgentCore
                               Memory when ``AGENTCORE_MEMORY_ID`` is set, else the ledger-derived
                               one (prompt + result per turn)
    POST /api/chat             continue a session: launch a fire-and-forget run; returns a ``taskId``
    GET  /api/chat             poll a launched run by ``task_id`` + ``session_id``

The runtime writes the ledger (see ``src/strandly_harness/serving/run_ledger.py``); the read routes
only ever read it. The **chat** routes invoke the deployed AgentCore runtime
(``bedrock-agentcore:InvokeAgentRuntime``) — fire-and-forget launch + poll, the runtime's native
long-task model (a chat turn can run minutes, past API Gateway's 30s integration cap, so we never
hold the socket open). Chat is **gated on ``STRANDLY_RUNTIME_ARN``**: unset it and the chat routes
return 503 and the runtime/dashboard behave exactly as before.

Core logic is split from the AWS wiring so it unit-tests with fakes and no boto3: ``route()`` takes
an injected :class:`LedgerReader`, :class:`RuntimeInvoker`, and :class:`MemoryReader`;
``lambda_handler`` builds the real ones. The session list aggregation is pure (derived from the
ledger rows the reader returns); the per-session transcript prefers AgentCore Memory (the verbatim
``ListEvents`` conversation) when configured and falls back to the ledger reconstruction otherwise.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

# Mirror the ledger's GSI contract. Canonical source: strandly_harness.core.constants.RUN_LEDGER_GSI_NAME
# (also mirrored in infra/stacks/common.py). This Lambda bundles standalone and can't import the
# harness, so the value is copied; tests/test_infra_constants_sync guards the three against drift.
GSI_NAME = "recent"
GSI_PK_ATTR = "gsi_pk"
GSI_PK_VALUE = "RUN"

# Mention-log table (the poller writes one row per processed @mention; the Mentions tab reads it).
# Mirrors strandly_harness.core.constants.MENTION_LOG_GSI_NAME / MENTION_LOG_GSI_PK_VALUE (canonical,
# also mirrored in infra/stacks/common.py) — tests/test_infra_constants_sync guards the copies.
MENTION_LOG_GSI_NAME = "recent"
MENTION_LOG_GSI_PK_VALUE = "MENTION"

# AgentCore Runtime session ids must be slash-free and at least this many chars or
# InvokeAgentRuntime throws an opaque ValidationException. Mirrors
# strandly_harness.core.constants.RUNTIME_SESSION_ID_MIN_LEN (guarded by tests/test_infra_constants_sync).
RUNTIME_SESSION_ID_MIN_LEN = 33

# AgentCore Memory is the verbatim transcript source for /api/sessions/{id} when configured
# (gated on AGENTCORE_MEMORY_ID, like chat is on STRANDLY_RUNTIME_ARN). These mirror
# strandly_harness.core.constants (DEFAULT_ACTOR_ID, MEMORY_MAX_EVENTS) — the reader must address the
# SAME actor id the run wrote under, and request a high page ceiling so a long run's FINAL
# assistant message isn't dropped at the 100/page default. Guarded by tests/test_infra_constants_sync.
DEFAULT_ACTOR_ID = "strandly"
MEMORY_MAX_EVENTS = 10_000

OVERVIEW_WINDOW = 200  # how many recent rows to aggregate for the Overview tab
SESSIONS_WINDOW = 200  # how many recent rows to group into sessions / scan for a transcript
RUNS_DEFAULT_LIMIT = 50
RUNS_MAX_LIMIT = 100
_DESCRIPTION_LIMIT = 280  # clip the prompt shown as a session's description in the list view
_TOOL_INPUT_LIMIT = 600  # clip a tool use's input JSON shown in the transcript
_TOOL_RESULT_LIMIT = 1_500  # clip a tool result's text shown in the transcript

# The poll-silent alarm doubles as the mention-poller liveness signal: OK means a successful poll
# happened within its window (the mentions check is actively running); ALARM means it went silent.
_POLL_ALARM_SUFFIX = "-poll-silent"

# Cognito settings exposed to the SPA (public values — client id / hosted-UI domain are not secret).
_CONFIG_ENV_KEYS = {
    "region": "AWS_REGION",
    "userPoolId": "COGNITO_USER_POOL_ID",
    "clientId": "COGNITO_CLIENT_ID",
    "cognitoDomain": "COGNITO_DOMAIN",
}

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _sanitize_session_id(raw: str) -> str:
    """Slash-free **Memory** session id (no padding) — mirrors :func:`strandly_harness.memory.session.sanitize_session_id`.

    Distinct from :func:`_runtime_session_id` (which *also* right-pads to ``RUNTIME_SESSION_ID_MIN_LEN``
    for the runtime's instance-affinity key): AgentCore **Memory** is written under the *un-padded*
    sanitized id (the ``AgentCoreMemorySessionManager`` uses ``sanitize_session_id``), so reading a
    transcript back must use exactly that — or ``ListEvents`` addresses an empty/different session.
    """
    return _UNSAFE.sub("-", raw).strip("-") or "session"


def _runtime_session_id(raw: str) -> str:
    """Slash-free, >= ``RUNTIME_SESSION_ID_MIN_LEN`` chars — the runtime's instance-affinity key.

    Mirrors :func:`strandly_harness.memory.session.runtime_session_id` so a chat launched here lands on the
    same Memory/affinity key the harness would use for the same ``session_id`` (deterministic pad).
    """
    sid = _sanitize_session_id(raw)
    if len(sid) < RUNTIME_SESSION_ID_MIN_LEN:
        sid = sid + "-" + "0" * (RUNTIME_SESSION_ID_MIN_LEN - len(sid) - 1)
    return sid


class LedgerReader:
    """Read-side wrapper over the run-ledger table. The boto3 Table is injectable for tests."""

    def __init__(self, table: Any):
        self._table = table

    def recent(self, limit: int) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        resp = self._table.query(
            IndexName=GSI_NAME,
            KeyConditionExpression=Key(GSI_PK_ATTR).eq(GSI_PK_VALUE),
            ScanIndexForward=False,  # newest first (started_at descending)
            Limit=limit,
        )
        return resp.get("Items", [])

    def get(self, task_id: str) -> dict[str, Any] | None:
        resp = self._table.get_item(Key={"task_id": task_id})
        return resp.get("Item")


class MentionLogReader:
    """Read-side wrapper over the mention-log table (poller-written). Table injectable for tests."""

    def __init__(self, table: Any):
        self._table = table

    def recent(self, limit: int) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        resp = self._table.query(
            IndexName=MENTION_LOG_GSI_NAME,
            KeyConditionExpression=Key(GSI_PK_ATTR).eq(MENTION_LOG_GSI_PK_VALUE),
            ScanIndexForward=False,  # newest first (seen_at descending)
            Limit=limit,
        )
        return resp.get("Items", [])


class RuntimeInvoker:
    """Invoke the deployed AgentCore runtime for chat — launch + poll. boto client injectable.

    Both calls are plain JSON in/out (no SSE): launch is fire-and-forget (the runtime's default
    mode → ``{status: accepted, taskId}``); poll merges the in-instance sentinel with durable
    AgentCore Memory (→ ``{status, result?}``). Kept separate from the ledger reader so chat can be
    enabled independently (it's gated on ``STRANDLY_RUNTIME_ARN``).
    """

    def __init__(self, runtime_arn: str, region: str | None = None, *, client: Any | None = None):
        self._runtime_arn = runtime_arn
        self._region = region
        self._client = client

    def client(self) -> Any:
        if self._client is None:
            import boto3

            session = boto3.Session(region_name=self._region) if self._region else boto3.Session()
            self._client = session.client("bedrock-agentcore")
        return self._client

    def _invoke(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self.client().invoke_agent_runtime(
            agentRuntimeArn=self._runtime_arn,
            runtimeSessionId=_runtime_session_id(session_id),
            payload=json.dumps(payload).encode(),
            contentType="application/json",
        )
        body = resp.get("response")
        raw = body.read() if hasattr(body, "read") else body
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode()
        try:
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            return {"raw": raw}

    def launch(self, session_id: str, message: str) -> dict[str, Any]:
        """Start a fire-and-forget chat turn on the session; returns ``{status, taskId}``."""
        return self._invoke(session_id, {"prompt": message, "sessionId": session_id})

    def poll(self, session_id: str, task_id: str) -> dict[str, Any]:
        """Poll a launched turn; returns ``{status: running|completed|failed|unknown, result?}``."""
        return self._invoke(
            session_id, {"action": "poll", "taskId": task_id, "sessionId": session_id}
        )


class MemoryReader:
    """Read a session's **verbatim** transcript from AgentCore Memory (``ListEvents``). Client injectable.

    Gated on ``AGENTCORE_MEMORY_ID``. Reads the data plane (the same ``bedrock-agentcore`` client the
    chat uses) under the run's memory id + a stable ``actor_id`` (``STRANDLY_ACTOR_ID`` or
    :data:`DEFAULT_ACTOR_ID` — reader and writer must agree) + the *sanitized* session id (the
    un-padded id the ``AgentCoreMemorySessionManager`` wrote under). Paginates to
    :data:`MEMORY_MAX_EVENTS` because ``ListEvents`` truncates to 100/page and returns oldest-first —
    so the default would drop the FINAL assistant message of any run longer than 100 events.

    This is what lets the dashboard show the real conversation (with the agent's own narration of
    what it did) instead of the ledger's one-row-per-turn ``prompt``+``result_summary`` reconstruction.
    """

    def __init__(
        self,
        memory_id: str,
        region: str | None = None,
        actor_id: str | None = None,
        *,
        client: Any | None = None,
    ):
        self._memory_id = memory_id
        self._region = region
        self._actor_id = actor_id or DEFAULT_ACTOR_ID
        self._client = client

    def client(self) -> Any:
        if self._client is None:
            import boto3

            session = boto3.Session(region_name=self._region) if self._region else boto3.Session()
            self._client = session.client("bedrock-agentcore")
        return self._client

    def events(self, session_id: str) -> list[dict[str, Any]]:
        """Raw ``ListEvents`` output for a session (paginated to the ceiling, oldest-first)."""
        sid = _sanitize_session_id(session_id)
        client = self.client()
        out: list[dict[str, Any]] = []
        next_token: str | None = None
        while len(out) < MEMORY_MAX_EVENTS:
            params: dict[str, Any] = {
                "memoryId": self._memory_id,
                "actorId": self._actor_id,
                "sessionId": sid,
                "maxResults": 100,  # API per-page cap; we page via nextToken
                "includePayloads": True,
            }
            if next_token:
                params["nextToken"] = next_token
            resp = client.list_events(**params)
            out.extend(resp.get("events", []) or [])
            next_token = resp.get("nextToken")
            if not next_token:
                break
        return out[:MEMORY_MAX_EVENTS]

    def transcript(self, session_id: str) -> list[dict[str, Any]]:
        """The session as ordered chat messages — see :func:`_memory_messages`."""
        return _memory_messages(self.events(session_id))


# CloudWatch log-event ceiling for one run's logs (keeps the response bounded; a run rarely emits
# more, and the drawer is for triage, not a full export).
LOGS_MAX_EVENTS = 1_000


class LogsReader:
    """Read a run's runtime logs from CloudWatch by session + time window. Client injectable.

    Gated on ``RUNTIME_LOG_GROUP``. AgentCore names each invocation's log stream
    ``…[runtime-logs-<sessionId>]<uuid>``, so we filter the group by the **session-id prefix** and the
    run's **time window** (the ledger row's ``started_at``/``ended_at``, padded) — giving the lines
    for that run without needing the unpredictable ``<uuid>`` suffix. Best-effort: any error yields no
    events (the drawer degrades, the run row still shows). This is per-session, not per-task; the time
    window narrows a multi-task (chat) session to roughly the run that's open.
    """

    def __init__(self, log_group: str, region: str | None = None, *, client: Any | None = None):
        self._log_group = log_group
        self._region = region
        self._client = client

    def client(self) -> Any:
        if self._client is None:
            import boto3

            session = boto3.Session(region_name=self._region) if self._region else boto3.Session()
            self._client = session.client("logs")
        return self._client

    def _session_streams(self, sid: str) -> list[str]:
        """Stream names for a session. AgentCore prefixes the stream with a DATE
        (``2026/06/30/[runtime-logs-<sid>]<uuid>``), so ``logStreamNamePrefix`` (anchored at the
        start) can't match ``[runtime-logs-…]``. We list recent streams and substring-match the
        ``runtime-logs-<sid>`` marker instead, then target those exact names.
        """
        marker = f"runtime-logs-{sid}]"
        resp = self.client().describe_log_streams(
            logGroupName=self._log_group, orderBy="LastEventTime", descending=True, limit=50
        )
        return [
            s["logStreamName"]
            for s in (resp.get("logStreams") or [])
            if marker in s.get("logStreamName", "")
        ]

    def for_run(self, session_id: str, *, start_ms: int | None, end_ms: int | None) -> list[dict[str, Any]]:
        """Log events for a run: the session's stream(s), narrowed to [start_ms, end_ms]."""
        sid = _sanitize_session_id(session_id)
        streams = self._session_streams(sid)
        if not streams:
            return []
        params: dict[str, Any] = {
            "logGroupName": self._log_group,
            "logStreamNames": streams,
            "limit": LOGS_MAX_EVENTS,
        }
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        resp = self.client().filter_log_events(**params)
        return [
            {"timestamp": e.get("timestamp"), "message": e.get("message", "")}
            for e in (resp.get("events") or [])
        ]


class AlarmsReader:
    """Read the deployment's CloudWatch alarm states (``DescribeAlarms``). Client injectable.

    Gated on ``ALARM_NAME_PREFIX`` (the MonitoringStack names every alarm ``<naming.hyphen>-…``, so
    one prefix scopes the read to this deployment's alarms). Powers the Overview's health strip:
    the alarm list itself, plus the mention-poller liveness derived from the ``…-poll-silent``
    alarm (its OK state literally means "a successful poll happened recently"). Best-effort: any
    error degrades to an empty list — health telemetry must never take the dashboard down.
    """

    def __init__(self, prefix: str, region: str | None = None, *, client: Any | None = None):
        self._prefix = prefix
        self._region = region
        self._client = client

    def client(self) -> Any:
        if self._client is None:
            import boto3

            session = boto3.Session(region_name=self._region) if self._region else boto3.Session()
            self._client = session.client("cloudwatch")
        return self._client

    def alarms(self) -> list[dict[str, Any]]:
        """All alarms under the prefix as ``{name, state, reason, since}``, name-sorted."""
        out: list[dict[str, Any]] = []
        next_token: str | None = None
        while True:
            params: dict[str, Any] = {"AlarmNamePrefix": self._prefix, "MaxRecords": 100}
            if next_token:
                params["NextToken"] = next_token
            resp = self.client().describe_alarms(**params)
            for a in resp.get("MetricAlarms", []) or []:
                since = a.get("StateUpdatedTimestamp")
                out.append(
                    {
                        "name": a.get("AlarmName", ""),
                        "state": a.get("StateValue", "INSUFFICIENT_DATA"),
                        "reason": _clip(a.get("StateReason"), 300),
                        "since": since.isoformat() if hasattr(since, "isoformat") else since,
                    }
                )
            next_token = resp.get("NextToken")
            if not next_token:
                break
        return sorted(out, key=lambda a: a["name"])


def _health(alarms: AlarmsReader | None) -> dict[str, Any]:
    """The Overview health strip: alarm states + mention-poller liveness.

    The poller status is derived from the ``…-poll-silent`` alarm (see MonitoringStack): ``OK``
    means a successful poll ran inside the alarm's window → the mentions check is **active**;
    ``ALARM`` → **silent** (the trigger died); anything else → **unknown**. Best-effort throughout.
    """
    if alarms is None:
        return {
            "configured": False,
            "alarms": [],
            "poller": {"status": "unknown", "detail": "alarms not configured (ALARM_NAME_PREFIX unset)"},
        }
    try:
        items = alarms.alarms()
    except Exception:  # noqa: BLE001 — health is best-effort; never 500 the overview
        logger.warning("describe_alarms failed", exc_info=True)
        return {
            "configured": True,
            "alarms": [],
            "poller": {"status": "unknown", "detail": "alarm read failed"},
        }
    poll = next((a for a in items if a["name"].endswith(_POLL_ALARM_SUFFIX)), None)
    if poll is None:
        poller = {"status": "unknown", "detail": "poll-silent alarm not found"}
    elif poll["state"] == "OK":
        poller = {"status": "active", "detail": "polled successfully within the alarm window"}
    elif poll["state"] == "ALARM":
        poller = {"status": "silent", "detail": poll.get("reason") or "no successful poll recently"}
    else:
        poller = {"status": "unknown", "detail": "insufficient data"}
    return {"configured": True, "alarms": items, "poller": poller}


def _tool_input_str(value: Any) -> str:
    """A tool use's ``input`` as a display string, clipped to :data:`_TOOL_INPUT_LIMIT`."""
    try:
        text = value if isinstance(value, str) else json.dumps(value)
    except (TypeError, ValueError):
        text = str(value)
    return _clip(text, _TOOL_INPUT_LIMIT) or ""


def _tool_result_text(block: dict[str, Any]) -> str:
    """A ``toolResult`` block's content as one display string, clipped to :data:`_TOOL_RESULT_LIMIT`.

    A tool result's ``content`` is a list of ``{"text": …}`` / ``{"json": …}`` items; anything else
    (images, documents) is summarized by its key so the transcript stays textual.
    """
    parts: list[str] = []
    for item in block.get("content") or []:
        if not isinstance(item, dict):
            continue
        if "text" in item:
            parts.append(str(item["text"]))
        elif "json" in item:
            try:
                parts.append(json.dumps(item["json"]))
            except (TypeError, ValueError):
                parts.append(str(item["json"]))
        else:
            parts.append(f"[{next(iter(item), 'content')}]")
    return _clip("\n".join(parts), _TOOL_RESULT_LIMIT) or ""


def _parse_session_message(raw: str) -> dict[str, Any]:
    """Parse a Memory ``content.text`` into ``{text, tools, tool_results}``.

    The Strands ``AgentCoreMemorySessionManager`` stores each message as a JSON-encoded SDK
    ``SessionMessage`` (double-wrapped: ``{"message": {"role", "content": [{"text"}, {"toolUse"},
    {"toolResult"}]}}``). Returns the concatenated text blocks plus the structured tool activity:
    ``tools`` (each ``toolUse``'s name + clipped input — what the agent *did*) and ``tool_results``
    (each ``toolResult``'s status + clipped output — what came back), so the SPA can render the
    real work inline instead of dropping it. Anything not in that shape is returned verbatim.
    """
    try:
        content = json.loads(raw)["message"]["content"]
        text = "\n".join(b["text"] for b in content if isinstance(b, dict) and "text" in b)
        tools = [
            {
                "name": str((b["toolUse"] or {}).get("name") or "tool"),
                "input": _tool_input_str((b["toolUse"] or {}).get("input")),
            }
            for b in content
            if isinstance(b, dict) and isinstance(b.get("toolUse"), dict)
        ]
        tool_results = [
            {
                "status": str((b["toolResult"] or {}).get("status") or "unknown"),
                "text": _tool_result_text(b["toolResult"] or {}),
            }
            for b in content
            if isinstance(b, dict) and isinstance(b.get("toolResult"), dict)
        ]
        return {"text": text, "tools": tools, "tool_results": tool_results}
    except (json.JSONDecodeError, KeyError, TypeError):
        return {"text": raw, "tools": [], "tool_results": []}


def _memory_messages(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``ListEvents`` output -> ordered chat messages ``[{role, text, tool_use}]`` (empty text dropped).

    Mirrors ``strandly_harness.memory.session._events_to_messages``: each event carries a ``payload`` list of
    ``{"conversational": {"role", "content": {"text"}}}`` items. We sort by ``eventTimestamp``
    defensively (the API returns chronological order, but we don't rely on it). Messages carry the
    human-readable text plus the structured tool activity (``tools`` / ``tool_results``); only
    messages with neither are dropped.
    """

    def _ts(ev: dict[str, Any]) -> Any:
        return ev.get("eventTimestamp") or 0

    out: list[dict[str, Any]] = []
    for ev in sorted(events, key=_ts):
        for item in ev.get("payload", []) or []:
            conv = item.get("conversational") if isinstance(item, dict) else None
            if not conv:
                continue
            role = (conv.get("role") or "").lower()
            parsed = _parse_session_message((conv.get("content") or {}).get("text", ""))
            text = parsed["text"]
            # A message earns a transcript entry if it says something OR did something: pure
            # tool-use / tool-result turns used to be dropped, hiding the agent's actual work.
            if (text and text.strip()) or parsed["tools"] or parsed["tool_results"]:
                out.append(
                    {
                        "role": role,
                        "text": text if text and text.strip() else "",
                        "tool_use": bool(parsed["tools"]),
                        "tools": parsed["tools"],
                        "tool_results": parsed["tool_results"],
                    }
                )
    return out


def _clip(text: Any, limit: int) -> str | None:
    if not text:
        return None
    s = str(text)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _overview(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a recent window of runs into the Overview card numbers."""
    today = datetime.now(timezone.utc).date().isoformat()
    completed = sum(1 for it in items if it.get("status") == "completed")
    failed = sum(1 for it in items if it.get("status") == "failed")
    running = sum(1 for it in items if it.get("status") == "running")
    finished = completed + failed
    runs_today = sum(1 for it in items if str(it.get("started_at", "")).startswith(today))
    tokens = sum(int(it.get("tokens_total", 0) or 0) for it in items)
    return {
        "window": len(items),
        "runs_today": runs_today,
        "active": running,
        "completed": completed,
        "failed": failed,
        "success_rate": round(completed / finished, 4) if finished else None,
        "tokens_total": tokens,
    }


def _sessions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group a recent (newest-first) window of runs into sessions, newest-active first.

    Pure: derived entirely from the ledger rows. Each session carries a ``description`` (the
    session's *originating* prompt — the oldest run's prompt in the window), the latest activity /
    status / target, the run count, and summed tokens. Rows with no ``session_id`` (sessionless
    one-shots) are skipped — there's nothing to continue chatting with.
    """
    sessions: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for it in items:  # newest-first
        sid = it.get("session_id")
        if not sid:
            continue
        if sid not in sessions:
            sessions[sid] = {
                "session_id": sid,
                "runs": 0,
                "tokens_total": 0,
                "last_activity": it.get("started_at") or it.get("ended_at"),
                "last_status": it.get("status"),
                "latest_prompt": _clip(it.get("prompt"), _DESCRIPTION_LIMIT),
                "target": it.get("github_target"),
                "description": None,
            }
            order.append(sid)
        s = sessions[sid]
        s["runs"] += 1
        s["tokens_total"] += int(it.get("tokens_total", 0) or 0)
        if not s["target"] and it.get("github_target"):
            s["target"] = it.get("github_target")
        # Iterating newest→oldest, so the LAST prompt we see is the oldest = the originating ask.
        if it.get("prompt"):
            s["description"] = _clip(it.get("prompt"), _DESCRIPTION_LIMIT)
    return [sessions[sid] for sid in order]


def _session_detail(items: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
    """One session's runs as an ordered (oldest-first) transcript: prompt + result per turn.

    Reconstructed purely from the ledger window (the standalone Lambda can't read AgentCore Memory,
    where the verbatim conversation lives) — each ledger row is one turn: the user's ``prompt`` and
    the agent's ``result_summary``. Bounded by the recent window, which is plenty for a live chat.
    """
    rows = [it for it in items if it.get("session_id") == session_id]
    rows.sort(key=lambda it: str(it.get("started_at") or ""))  # oldest-first for reading top→bottom
    turns = [
        {
            "task_id": it.get("task_id"),
            "status": it.get("status"),
            "started_at": it.get("started_at"),
            "prompt": it.get("prompt"),
            "result": it.get("result_summary"),
            "error": it.get("error"),
        }
        for it in rows
    ]
    return {"session_id": session_id, "runs": len(turns), "turns": turns}


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    """Decode an HTTP API v2 request body to a dict (handles base64); ``{}`` on anything unparseable."""
    raw = event.get("body") or ""
    if event.get("isBase64Encoded") and raw:
        import base64

        try:
            raw = base64.b64decode(raw).decode()
        except (ValueError, UnicodeDecodeError):
            return {}
    try:
        data = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _chat(event: dict[str, Any], method: str, invoker: RuntimeInvoker | None) -> tuple[int, dict[str, Any]]:
    """Handle the chat routes: POST launches a turn, GET polls one. 503 when chat is disabled."""
    if invoker is None:
        return 503, {"error": "chat not configured (STRANDLY_RUNTIME_ARN unset)"}
    if method == "POST":
        body = _parse_body(event)
        session_id = str(body.get("session_id") or "").strip()
        message = str(body.get("message") or "").strip()
        if not session_id or not message:
            return 400, {"error": "chat requires 'session_id' and 'message'"}
        return 200, invoker.launch(session_id, message)
    if method == "GET":
        q = event.get("queryStringParameters") or {}
        task_id = str((q or {}).get("task_id") or "").strip()
        session_id = str((q or {}).get("session_id") or "").strip()
        if not task_id or not session_id:
            return 400, {"error": "poll requires 'task_id' and 'session_id'"}
        return 200, invoker.poll(session_id, task_id)
    return 405, {"error": "method not allowed"}


def route(
    event: dict[str, Any],
    reader: LedgerReader | None,
    invoker: RuntimeInvoker | None = None,
    memory: MemoryReader | None = None,
    logs: LogsReader | None = None,
    alarms: AlarmsReader | None = None,
    mentions: MentionLogReader | None = None,
) -> tuple[int, dict[str, Any]]:
    """Pure router: (status_code, body) for an HTTP API v2 event. No AWS unless reader/invoker/memory used."""
    http = (event.get("requestContext") or {}).get("http") or {}
    method = (http.get("method") or "GET").upper()
    path = event.get("rawPath") or http.get("path") or "/"
    path = path.rstrip("/") or "/"

    # Public route: the SPA needs the Cognito client id/domain *before* it can log in.
    if path.endswith("/config"):
        if method != "GET":
            return 405, {"error": "method not allowed"}
        return 200, {k: os.environ.get(env, "") for k, env in _CONFIG_ENV_KEYS.items()}

    # Chat (launch/poll) talks to the runtime, not the ledger — handle before the GET-only gate.
    if path.endswith("/chat"):
        return _chat(event, method, invoker)

    if method != "GET":
        return 405, {"error": "method not allowed"}

    # Health reads CloudWatch, not the ledger — it must work (and degrade) independently of it.
    if path.endswith("/health"):
        return 200, _health(alarms)

    # Mentions read the poller's mention-log table, not the ledger. Unconfigured → an explicit
    # enabled:false (the tab renders a hint instead of an error) — the feature degrades cleanly.
    if path.endswith("/mentions"):
        if mentions is None:
            return 200, {"mentions": [], "enabled": False}
        limit = _clamp_limit((event.get("queryStringParameters") or {}).get("limit"))
        return 200, {"mentions": mentions.recent(limit), "enabled": True}

    if reader is None:  # config/chat/health are the only routes that don't need the table
        return 500, {"error": "ledger table not configured"}

    if path.endswith("/overview"):
        return 200, _overview(reader.recent(OVERVIEW_WINDOW))

    if path.endswith("/sessions"):
        return 200, {"sessions": _sessions(reader.recent(SESSIONS_WINDOW))}

    if "/sessions/" in path:
        sid = (event.get("pathParameters") or {}).get("id")
        if not sid:
            return 404, {"error": "session not found", "session_id": sid}
        detail = _session_detail(reader.recent(SESSIONS_WINDOW), sid)
        detail["source"] = "ledger"
        # Prefer the verbatim conversation from AgentCore Memory when it's wired (the real messages,
        # including the agent's own narration), falling back to the ledger-derived transcript on any
        # error or when Memory has nothing — telemetry must never take the chat panel down.
        if memory is not None:
            try:
                messages = memory.transcript(sid)
            except Exception:  # noqa: BLE001 — Memory is best-effort; degrade to the ledger transcript
                logger.warning("memory transcript read failed (session_id=%s); using ledger", sid,
                               exc_info=True)
                messages = []
            if messages:
                detail["messages"] = messages
                detail["source"] = "memory"
        if detail["runs"] or detail.get("messages"):
            return 200, detail
        return 404, {"error": "session not found", "session_id": sid}

    # /runs/{id}/logs — the run's runtime logs from CloudWatch (by session + time window).
    params = event.get("pathParameters") or {}
    task_id = params.get("id")
    if task_id and path.endswith("/logs") and "/runs/" in path:
        item = reader.get(task_id)
        if not item:
            return 404, {"error": "run not found", "task_id": task_id}
        if logs is None:
            return 200, {"task_id": task_id, "events": [], "source": "unconfigured"}
        session_id = item.get("session_id")
        if not session_id:
            return 200, {"task_id": task_id, "events": [], "source": "no-session"}
        try:
            events = logs.for_run(
                str(session_id),
                start_ms=_iso_to_ms(item.get("started_at"), pad_ms=-5_000),
                end_ms=_iso_to_ms(item.get("ended_at"), pad_ms=30_000),
            )
        except Exception:  # noqa: BLE001 — logs are best-effort; the run row still renders
            logger.warning("log read failed (task_id=%s)", task_id, exc_info=True)
            return 200, {"task_id": task_id, "events": [], "source": "error"}
        return 200, {"task_id": task_id, "events": events, "source": "cloudwatch"}

    # /runs/{id}
    if task_id and "/runs/" in path:
        item = reader.get(task_id)
        return (200, item) if item else (404, {"error": "run not found", "task_id": task_id})

    if path.endswith("/runs"):
        limit = _clamp_limit((event.get("queryStringParameters") or {}).get("limit"))
        return 200, {"runs": reader.recent(limit), "count": None}

    return 404, {"error": "not found", "path": path}


def _iso_to_ms(iso: Any, *, pad_ms: int = 0) -> int | None:
    """ISO-8601 instant → epoch-ms (+ ``pad_ms`` padding), or ``None`` if unparseable/absent.

    The window is padded — a little before ``started_at``, a little after ``ended_at`` — so a run's
    first/last log lines (emitted just outside the recorded bounds) aren't clipped.
    """
    if not iso or not isinstance(iso, str):
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(dt.timestamp() * 1000) + pad_ms


def _clamp_limit(raw: Any) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return RUNS_DEFAULT_LIMIT
    return max(1, min(n, RUNS_MAX_LIMIT))


class _DecimalEncoder(json.JSONEncoder):
    """DynamoDB returns numbers as ``Decimal``; render whole numbers as int, else float."""

    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return int(o) if o == o.to_integral_value() else float(o)
        return super().default(o)


def _build_reader() -> LedgerReader | None:
    table_name = os.environ.get("RUN_LEDGER_TABLE")
    if not table_name:
        return None
    import boto3

    return LedgerReader(boto3.resource("dynamodb").Table(table_name))


def _build_mentions_reader() -> MentionLogReader | None:
    """A mention-log reader when ``MENTION_LOG_TABLE`` is set, else ``None`` (tab shows a hint)."""
    table_name = os.environ.get("MENTION_LOG_TABLE")
    if not table_name:
        return None
    import boto3

    return MentionLogReader(boto3.resource("dynamodb").Table(table_name))


def _build_invoker() -> RuntimeInvoker | None:
    runtime_arn = os.environ.get("STRANDLY_RUNTIME_ARN")
    if not runtime_arn:
        return None
    return RuntimeInvoker(runtime_arn, os.environ.get("AWS_REGION"))


def _build_memory_reader() -> MemoryReader | None:
    """A Memory transcript reader when ``AGENTCORE_MEMORY_ID`` is set, else ``None`` (ledger only)."""
    memory_id = os.environ.get("AGENTCORE_MEMORY_ID")
    if not memory_id:
        return None
    return MemoryReader(
        memory_id, os.environ.get("AWS_REGION"), os.environ.get("STRANDLY_ACTOR_ID")
    )


def _build_logs_reader() -> LogsReader | None:
    """A CloudWatch logs reader when ``RUNTIME_LOG_GROUP`` is set, else ``None`` (logs route no-ops)."""
    log_group = os.environ.get("RUNTIME_LOG_GROUP")
    if not log_group:
        return None
    return LogsReader(log_group, os.environ.get("AWS_REGION"))


def _build_alarms_reader() -> AlarmsReader | None:
    """An alarm-state reader when ``ALARM_NAME_PREFIX`` is set, else ``None`` (health degrades)."""
    prefix = os.environ.get("ALARM_NAME_PREFIX")
    if not prefix:
        return None
    return AlarmsReader(prefix, os.environ.get("AWS_REGION"))


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """AWS Lambda entrypoint: route, then serialize to an HTTP API v2 proxy response."""
    status, body = route(
        event,
        _build_reader(),
        _build_invoker(),
        _build_memory_reader(),
        _build_logs_reader(),
        _build_alarms_reader(),
        _build_mentions_reader(),
    )
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "cache-control": "no-store",
        },
        "body": json.dumps(body, cls=_DecimalEncoder),
    }
