"""The dashboard API: read sessions and work products, and chat with the deployed agent.

Routes (behind an API Gateway HTTP API with a Cognito JWT authorizer):

    GET  /api/sessions         recent runs grouped into sessions, newest-first (with a description)
    GET  /api/sessions/{id}    one session's transcript — its verbatim AgentCore Memory transcript
                               events, else the session store's snapshot, else the ledger-derived
                               one (prompt + result per turn). Both session routes carry the
                               session's ``title`` (``null`` when it has none)
    PATCH /api/sessions/{id}   ``{"title": string | null}`` sets/replaces the title; empty or null
                               clears it. 404 for a session the detail route would not find
    POST /api/chat             continue a session: launch a detached run; returns a ``taskId``.
                               An optional ``model`` (a harness model string) and ``effort``
                               go to the runtime's harness model resolution; a choice it
                               refuses, or one that is not a short string, is a 400.
                               When the session is already running, the new message supersedes it:
                               the running turn ends ``cancelled`` and a new run (a new ``taskId``)
                               answers the new message with the whole conversation in view.
                               ``{"action": "cancel"}`` instead of ``message`` stops the session's
                               in-flight run.
    GET  /api/chat             poll a launched run by ``task_id`` + ``session_id``; an optional
                               ``since`` cursor also returns the run's live events from that
                               sequence on (incremental streaming for the session page).
                               A poll / cancel whose session is still cold-starting answers 504
                               ``{"status": "starting", "retryable": true}``: repeat the same call.
    GET  /api/chat?whoami=1    the person bound to the signed-in user: ``{github, person}``
    GET  /api/workproducts     one GitHub item's work products, newest-first
                               (``?session_id=`` required; ``limit`` optional). Gated on
                               ``STRANDLY_WORK_PRODUCT_TABLE`` (unset → 503).

``/api/actions/invoke`` (GitHub Actions, OIDC) is served by ``actions.py`` from this asset as its own
Lambda, which may only invoke the runtime; this handler answers it 404.

The caller's person is the enabled people-directory row bound to the verified Cognito ``sub``
(``by_cognito``): chat launches forward it (``personId``, ``personGithub``, ``personName``,
``personRole``, ``personSlackUserId``) to the runtime. No binding, a disabled row and a directory
error all mean "no person". Through API Gateway ``X-Strandly-Person: github:<login>`` is advisory
and never selects a person: a login other than the bound person's is ignored and logged. A direct
Lambda invoke (no API Gateway and no authorizer context, so only an IAM-trusted principal) is
trusted like an ingress: its header names the person (``by_github``), and the launch adds
``directInvoke: true`` so the runtime accepts that person without a ``portalUser``.

Chat launches a detached run on the agent runtime and polls it with a ``since`` cursor (a turn can
outlast API Gateway's 30-second limit, and a dropped client never stops the run); it is off (503)
without ``STRANDLY_RUNTIME_ARN``. A session's transcript prefers the verbatim Memory events, then the
session store snapshot, then the ledger. ``route()`` takes injected readers and an invoker so it
tests without boto3; ``lambda_handler`` builds the real ones.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from strandly.core.constants import (
    CHAT_EFFORT_KEY,
    CHAT_EFFORT_MAX_CHARS,
    CHAT_MODEL_KEY,
    CHAT_MODEL_MAX_CHARS,
    DIRECT_INVOKE_KEY,
)
from strandly.core.session_ids import (
    actor_marker_key,
    clean_session_title,
    memory_session_id,
    session_title_key,
    snapshot_key,
    snapshot_messages,
    transcript_payload_key,
)
from strandly.core.session_ids import (
    runtime_session_id as _runtime_session_id,
)
from strandly.core.session_ids import (
    sanitize_session_id as _sanitize_session_id,
)
from strandly.core.session_ids import (
    store_session_id as _store_session_id,  # noqa: F401 — compatibility
)
from strandly.ops.people import (
    PEOPLE_TABLE_ENV,
    PERSON_HEADER,
    PeopleDirectory,
    Person,
    claimed_github,
    direct_invoke_person,
    is_direct_invoke,
)

logger = logging.getLogger(__name__)

# Mirror the ledger's GSI contract. Canonical source: strandly.core.constants.RUN_LEDGER_GSI_NAME
# (also mirrored in infra/stacks/common.py). This Lambda bundles standalone and can't import the
# harness, so the value is copied; tests/test_infra_constants_sync guards the three against drift.
GSI_NAME = "recent"
GSI_PK_ATTR = "gsi_pk"
GSI_PK_VALUE = "RUN"

# Work-product table's per-GitHub-item index (pk=session_id, sort=created_at). Mirrors
# strandly.core.constants.WORK_PRODUCT_SESSION_GSI_NAME (canonical, also mirrored in
# infra/stacks/common.py) — tests/test_infra_constants_sync guards the copies.
WORK_PRODUCT_SESSION_GSI_NAME = "by_session"

# AgentCore Runtime session ids must be slash-free and at least this many chars or
# InvokeAgentRuntime throws an opaque ValidationException. Mirrors
# strandly.core.constants.RUNTIME_SESSION_ID_MIN_LEN (guarded by tests/test_infra_constants_sync).
RUNTIME_SESSION_ID_MIN_LEN = 33

# Chat's InvokeAgentRuntime timeouts. This Lambda has 29 s (infra/stacks/dashboard_stack.py) behind an
# HTTP API with a hard 30 s integration timeout, and a cold session's first InvokeAgentRuntime can
# take longer than that. So a slow launch cannot be *acknowledged* in time — the budget below
# just makes it fail inside the Lambda (connect 3 s +
# read 22 s = 25 s, < 29 s) with ONE attempt, so it is reported as ``status: starting`` instead of
# botocore's 60 s default being cut off by the Lambda timeout (and, in a longer-lived caller,
# retried into a duplicate run). Polls are idempotent: 2 attempts x (3 + 10) s still fit the budget.
CHAT_CONNECT_TIMEOUT_S = 3
CHAT_LAUNCH_READ_TIMEOUT_S = 22
CHAT_CONTROL_READ_TIMEOUT_S = 10

# What a poll / cancel / capability probe gets (HTTP 504) while its session cold-starts: nothing
# failed, the caller repeats the SAME idempotent call. Not 503, which clients read as "chat is
# not configured"; ``status`` tells it apart from a gateway's own 504. A launch never gets it
# (200 ``starting``).
RUNTIME_STARTING = {
    "status": "starting",
    "retryable": True,
    "error": "agent session is starting; retry this call",
}

# Poll payload flag and response key for a poll bound to the tasks the runtime issued on that
# session; mirror strandly.serve.agentcore_app (pinned by tests/test_infra_constants_sync).
POLL_ISSUED_ONLY_KEY = "issuedTaskOnly"
POLL_ISSUED_KEY = "issued"


class RuntimeStarting(Exception):
    """A control call read-timed-out on every attempt: its session is still cold-starting."""

# /api/sessions/{id}: the verbatim transcript is AgentCore Memory events (one per message, written by
# strandly.memory.transcript under the session's actor), else the session store's latest
# snapshot (managed, compacted). The actor is read from session/<sid>/memory-actor.json next to the
# snapshot. Layout and limits mirror strandly.core.constants (SESSION_STORE_*,
# MEMORY_TEAM_ACTOR, TRANSCRIPT_MAX_MESSAGES) — guarded by tests/test_infra_constants_sync.
SESSION_STORE_SESSION_PREFIX = "session"
SESSION_STORE_AGENT_ID = "default"
SESSION_STORE_LATEST_SNAPSHOT = "snapshot_latest.json"
SESSION_STORE_ACTOR_MARKER = "memory-actor.json"
MEMORY_TEAM_ACTOR = "team"
TRANSCRIPT_MAX_MESSAGES = 2_000
# Wall-clock budget for one transcript read (ListEvents pages), well inside the API's 29 s limit.
# When it runs out the read keeps the NEWEST events fetched so far and flags the transcript truncated.
_TRANSCRIPT_READ_BUDGET_SECONDS = 15.0

# Page ceiling for one transcript read: stop paging ListEvents (100 per page) past this many events.
MEMORY_MAX_EVENTS = 10_000
# How many offloaded events (metadata ``offloaded``: the payload was too large for Memory) one read
# loads in full from the session store, one GetObject each; the rest show their recorded preview.
_TRANSCRIPT_OFFLOAD_RESOLVE_MAX = 50
_TRANSCRIPT_SEQ = re.compile(r"^\d{1,32}-\d{1,16}$")

SESSIONS_WINDOW = 200  # how many recent rows to group into sessions / scan for a transcript
SESSION_MESSAGES_DEFAULT_LIMIT = 50
SESSION_MESSAGES_MAX_LIMIT = 200
RUNS_DEFAULT_LIMIT = 50
RUNS_MAX_LIMIT = 100
_DESCRIPTION_LIMIT = 280  # clip the prompt shown as a session's description in the list view
_TOOL_INPUT_LIMIT = 600  # clip a tool use's input JSON shown in the transcript
_TOOL_RESULT_LIMIT = 1_500  # clip a tool result's text shown in the transcript

def _memory_session_id(raw: str) -> str:
    """``memory_session_id`` under the ``raw=`` keyword this module's callers use."""
    return memory_session_id(raw)


def _event_item_doc(item: Any) -> Any:
    if not isinstance(item, dict):
        return None
    if "blob" in item:
        return item["blob"]
    return (item.get("json") or {}).get("content")


def _event_message(event: dict[str, Any]) -> dict[str, Any] | None:
    """The message one transcript event carries — mirrors
    ``strandly.memory.transcript.event_message`` (parity-tested). JSON or blob items hold
    the verbatim message (or its parts) and win; conversational items hold the text, split across
    items. A reply with reasoning carries both: the text for extraction, the blob for readers."""
    payload = event.get("payload") or []
    parts: dict[int, str] = {}
    for item in payload:
        doc = _event_item_doc(item)
        if isinstance(doc, dict) and isinstance(doc.get("message"), dict):
            return dict(doc["message"])
        if isinstance(doc, dict) and isinstance(doc.get("data"), str):
            parts[int(doc.get("part", len(parts)))] = doc["data"]
    if parts:
        try:
            whole = json.loads("".join(parts[i] for i in sorted(parts)))
        except ValueError:
            return None
        message = whole.get("message") if isinstance(whole, dict) else None
        return dict(message) if isinstance(message, dict) else None
    texts: list[str] = []
    role = ""
    for item in payload:
        conv = item.get("conversational") if isinstance(item, dict) else None
        if isinstance(conv, dict):
            role = role or str(conv.get("role") or "").lower()
            texts.append(str((conv.get("content") or {}).get("text") or ""))
    if not texts:
        return None
    return {"role": role, "content": [{"text": "".join(texts)}]}


# botocore's stand-in for a union member its service model doesn't know:
# ``{"SDK_UNKNOWN_MEMBER": {"name": "<member>"}}``. A ListEvents payload item shaped like that is a
# message this Lambda's SDK cannot read — the failure that once hid every tool call (the runtime's
# bundled botocore predated PayloadType ``json``; infra ships a pinned SDK layer since). Counted per
# invocation and reported in ONE warning by :func:`lambda_handler`, so it can never be silent again.
_SDK_UNKNOWN_MEMBER = "SDK_UNKNOWN_MEMBER"
_unknown_payload_members: Counter[str] = Counter()
_unknown_payload_lock = threading.Lock()


def _count_unknown_payload_members(events: list[dict[str, Any]]) -> None:
    """Tally payload items botocore returned as unknown union members (see above)."""
    found: Counter[str] = Counter()
    for event in events:
        payload = event.get("payload") if isinstance(event, dict) else None
        for item in payload or []:
            unknown = item.get(_SDK_UNKNOWN_MEMBER) if isinstance(item, dict) else None
            if unknown is not None:
                name = unknown.get("name") if isinstance(unknown, dict) else None
                found[str(name or "?")] += 1
    if found:
        with _unknown_payload_lock:
            _unknown_payload_members.update(found)


def _report_unknown_payload_members() -> dict[str, int]:
    """Log the invocation's unknown-member tally as one WARNING (if any) and reset it."""
    with _unknown_payload_lock:
        counts = dict(_unknown_payload_members)
        _unknown_payload_members.clear()
    if counts:
        try:
            import botocore

            sdk = botocore.__version__
        except Exception:  # noqa: BLE001 — the version is diagnostic garnish only
            sdk = "unknown"
        logger.warning(
            "SDK_UNKNOWN_MEMBER ListEvents payload items dropped: %s — botocore %s does not know "
            "these PayloadType members, so those transcript messages are missing (is the "
            "pinned AWS SDK layer attached to this function?)",
            ", ".join(f"{name}={n}" for name, n in sorted(counts.items())), sdk,
        )
    return counts


def _is_transcript_event(event: dict[str, Any]) -> bool:
    """True for an event the transcript recorder wrote (its metadata carries ``kind``)."""
    metadata = event.get("metadata")
    return isinstance(metadata, dict) and "kind" in metadata


def _metadata_value(metadata: Any, key: str) -> str | None:
    value = (metadata.get(key) or {}).get("stringValue") if isinstance(metadata, dict) else None
    return value if isinstance(value, str) else None


def _last_user(items: list[tuple[str, dict[str, Any], Any]]) -> int | None:
    return next(
        (i for i in range(len(items) - 1, -1, -1) if str(items[i][1].get("role") or "").lower() == "user"),
        None,
    )


def _attach_context(
    items: list[tuple[str, dict[str, Any], Any]], index: int | None, context: dict[str, Any]
) -> None:
    """Append a ``context`` event's ``injected`` blocks to the message at ``index`` (once each).

    The runtime records them after the message they were written into; without that message in
    this read (``index`` is ``None``) they are dropped rather than attached to another.
    """
    if index is None:
        return
    identity, message, ts = items[index]
    content = list(message.get("content") or [])
    content += [b for b in context.get("content") or [] if b not in content]
    items[index] = (identity, {**message, "content": content}, ts)


def _event_sort_key(event: dict[str, Any]) -> tuple[float, str]:
    ts = event.get("eventTimestamp")
    epoch = ts.timestamp() if isinstance(ts, datetime) else float(ts or 0) if isinstance(ts, (int, float)) else 0.0
    seq = ((event.get("metadata") or {}).get("seq") or {}).get("stringValue") or ""
    return epoch, str(seq)


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


class SessionTitleStore:
    """Session titles: one run-ledger row per session (``task_id`` = ``session_title_key``).

    Takes the boto3 DynamoDB service resource (batch reads live there) and the ledger table name;
    both injectable for tests. Rows carry no ``gsi_pk``, so the recent-runs Query never sees them.
    """

    _BATCH = 100  # BatchGetItem's per-request key limit
    _BATCH_ATTEMPTS = 3

    def __init__(self, resource: Any, table_name: str):
        self._resource = resource
        self._table_name = table_name

    def titles(self, session_ids: list[str]) -> dict[str, str]:
        """``{session_id: title}`` for the sessions that have one."""
        by_key = {session_title_key(sid): sid for sid in session_ids}
        found: dict[str, str] = {}
        keys = list(by_key)
        for start in range(0, len(keys), self._BATCH):
            pending: dict[str, Any] = {self._table_name: {
                "Keys": [{"task_id": k} for k in keys[start:start + self._BATCH]],
                "ProjectionExpression": "#key, #title",
                "ExpressionAttributeNames": {"#key": "task_id", "#title": "title"},
            }}
            for _ in range(self._BATCH_ATTEMPTS):
                resp = self._resource.batch_get_item(RequestItems=pending)
                for item in (resp.get("Responses") or {}).get(self._table_name, []):
                    title = item.get("title")
                    if isinstance(title, str) and title and item.get("task_id") in by_key:
                        found[by_key[item["task_id"]]] = title
                pending = resp.get("UnprocessedKeys") or {}
                if not pending:
                    break
            if pending:
                raise RuntimeError("session titles: unprocessed keys after retries")
        return found

    def title(self, session_id: str) -> str | None:
        resp = self._resource.Table(self._table_name).get_item(
            Key={"task_id": session_title_key(session_id)},
            ProjectionExpression="#title", ExpressionAttributeNames={"#title": "title"},
        )
        title = (resp.get("Item") or {}).get("title")
        return title if isinstance(title, str) and title else None

    def put(self, session_id: str, title: str, *, updated_by: str, person: str | None = None) -> None:
        """Replace the title; an empty ``title`` clears it (the row stays, without ``title``).

        ``person`` (``github:<login>`` or ``person:<id>``) records the bound person who renamed it.
        """
        item: dict[str, Any] = {
            "task_id": session_title_key(session_id),
            "session_id": session_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_by": updated_by,
        }
        if title:
            item["title"] = title
        if person:
            item["person"] = person
        self._resource.Table(self._table_name).put_item(Item=item)


class WorkProductReader:
    """Read the portal work-product table (runtime-written). Table injectable for tests.

    The runtime persists one row per review/triage/first-response artifact (see
    ``strandly.ops.work_product``); this read side serves the per-GitHub-item history
    (``by_session``). Items are already snake_case with DynamoDB ``Decimal`` numbers, so they are
    returned verbatim and serialized by :class:`_DecimalEncoder` — no camel-casing (see
    ``strandly.ops.work_product``).
    """

    def __init__(self, table: Any):
        self._table = table

    def by_session(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Every artifact for one GitHub item, newest-first (``by_session`` GSI)."""
        from boto3.dynamodb.conditions import Key

        resp = self._table.query(
            IndexName=WORK_PRODUCT_SESSION_GSI_NAME,
            KeyConditionExpression=Key("session_id").eq(session_id),
            ScanIndexForward=False,  # newest first (created_at descending)
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

    def __init__(
        self,
        runtime_arn: str,
        region: str | None = None,
        *,
        client: Any | None = None,
        launch_client: Any | None = None,
    ):
        self._runtime_arn = runtime_arn
        self._region = region
        self._client = client
        # A launch gets its OWN client (one attempt, no retries); an injected ``client`` serves
        # both unless a ``launch_client`` is injected too (tests).
        self._launch_client = launch_client if launch_client is not None else client

    @staticmethod
    def client_config(*, launch: bool) -> Any:
        """botocore ``Config`` for chat's InvokeAgentRuntime calls (see :data:`CHAT_LAUNCH_READ_TIMEOUT_S`).

        A launch is a new message — NOT idempotent (AgentCore has no per-request idempotency token)
        — so it gets exactly one attempt (``total_max_attempts=1``; botocore's ``max_attempts=1``
        would mean one *retry*). Poll / cancel are idempotent and keep standard retries.
        """
        from botocore.config import Config as BotocoreConfig

        return BotocoreConfig(
            read_timeout=CHAT_LAUNCH_READ_TIMEOUT_S if launch else CHAT_CONTROL_READ_TIMEOUT_S,
            connect_timeout=CHAT_CONNECT_TIMEOUT_S,
            retries={"total_max_attempts": 1 if launch else 2, "mode": "standard"},
        )

    def _make_client(self, *, launch: bool) -> Any:
        import boto3

        session = boto3.Session(region_name=self._region) if self._region else boto3.Session()
        return session.client("bedrock-agentcore", config=self.client_config(launch=launch))

    def client(self) -> Any:
        if self._client is None:
            self._client = self._make_client(launch=False)
        return self._client

    def launch_client(self) -> Any:
        if self._launch_client is None:
            self._launch_client = self._make_client(launch=True)
        return self._launch_client

    def _invoke(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        launch = not payload.get("action")
        client = self.launch_client() if launch else self.client()
        try:
            return self._invoke_with(client, session_id, payload)
        except Exception as e:  # noqa: BLE001 — only a read-timeout is translated
            if launch and type(e).__name__ == "ReadTimeoutError":
                # The request reached AgentCore and the session is cold-starting (its instance is
                # booting, ~1 min) — the run WILL start; it just can't be acknowledged inside this
                # Lambda's 29 s / API Gateway's 30 s budget. Say so instead of failing, so the portal
                # neither reports "not sent" nor invites a resend (= a duplicate run). The panel
                # attaches once the run shows up on the session (GET /api/sessions/{id}).
                logger.warning(
                    "chat launch for %s still cold-starting after %ss",
                    session_id,
                    CHAT_LAUNCH_READ_TIMEOUT_S,
                )
                return {"status": "starting", "sessionId": session_id}
            if type(e).__name__ == "ReadTimeoutError":
                # Poll / cancel / probe on a cold session: idempotent, so the caller retries it
                # (``RUNTIME_STARTING``) instead of this Lambda dying with the raw timeout.
                logger.warning("chat %s for %s: session still cold-starting", payload["action"], session_id)
                raise RuntimeStarting(session_id) from None
            raise

    def _invoke_with(self, client: Any, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = client.invoke_agent_runtime(
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

    def launch(
        self,
        session_id: str,
        message: str,
        *,
        mode: str | None = None,
        trigger_source: str | None = None,
        invocation_context: dict[str, Any] | None = None,
        portal_user: str | None = None,
        portal_issuer: str | None = None,
        person: dict[str, Any] | None = None,
        model: tuple[str | None, str | None] | None = None,
    ) -> dict[str, Any]:
        """Start a fire-and-forget chat turn on the session; returns ``{status, taskId}``.

        ``status`` is ``accepted`` and ``taskId`` is the run answering this message; a run already
        going on the session is superseded (it ends ``cancelled``).

        ``mode`` / ``trigger_source`` carry the portal role + provenance: the
        portal's publish handshake fires this with ``mode="post"`` + ``trigger_source="portal:curate"``
        so the run is recorded as a post turn. They travel as the payload's ``mode`` / ``triggerSource``
        keys; ``mode`` must never be ``"stream"`` here (that's the transport selector).

        ``portal_user`` is the immutable Cognito sub and ``portal_issuer`` its verified issuer,
        forwarded as ``portalUser`` / ``portalIssuer``: the
        runtime's ``schedule`` tool records it as the requester.

        ``person`` holds the payload fields of :func:`person_fields` (``person*`` and, for a
        direct invoke, ``directInvoke``).

        ``model`` is the ``(model, effort)`` the user chose for this turn (:func:`_chat_model`),
        each forwarded as ``model`` / ``effort`` when set; ``None`` runs the deployment's default.

        ``invocation_context`` is the caller's ambient UI state — what the human is currently
        viewing in the portal (issue/PR of the triage/review pane) — forwarded as the payload's
        ``invocationContext`` key. The runtime injects it per turn as a labeled block; it gates
        nothing (see ``core/context.py`` in the harness).
        """
        payload: dict[str, Any] = {"prompt": message, "sessionId": session_id}
        if mode:
            payload["mode"] = mode
        if trigger_source:
            payload["triggerSource"] = trigger_source
        if invocation_context:
            payload["invocationContext"] = invocation_context
        if portal_user and portal_issuer:
            payload["portalUser"] = portal_user
            payload["portalIssuer"] = portal_issuer
        if person:
            payload.update(person)
        if model is not None:
            for key, value in zip((CHAT_MODEL_KEY, CHAT_EFFORT_KEY), model, strict=True):
                if value is not None:
                    payload[key] = value
        return self._invoke(session_id, payload)

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a payload the caller built (the GitHub Actions ingress); its ``sessionId`` routes it."""
        return self._invoke(payload["sessionId"], payload)

    def receipt(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Invoke the native opt-in exact-run receipt path (no transport facade)."""
        return self._invoke(payload["sessionId"], payload)

    def cancel(self, session_id: str) -> dict[str, Any]:
        """Stop the session's in-flight run (the dashboard's Stop button).

        Sets the running turn's cancel signal; starts nothing. Returns ``{status: cancelled|idle}``.
        """
        return self._invoke(session_id, {"action": "cancel", "sessionId": session_id})

    def poll(
        self, session_id: str, task_id: str, since: int | None = None, *, issued_only: bool = False
    ) -> dict[str, Any]:
        """Poll a launched turn; ``{status: running|completed|cancelled|failed|unknown, result?}``.

        ``since`` is the live-event cursor: when given, the runtime also returns the run's
        buffered events at/after that sequence number plus ``nextSeq`` (see the runtime's
        ``poll_result``) — this is what makes the chat panel stream instead of fire-and-forget.
        ``runtimeSessionId`` affinity routes the poll to the instance holding the buffer.
        ``issued_only`` asks the runtime to answer only for a task it started on this session; it
        answers any other task id with ``{POLL_ISSUED_KEY: False}`` and reads nothing.
        """
        payload: dict[str, Any] = {"action": "poll", "taskId": task_id, "sessionId": session_id}
        if since is not None:
            payload["since"] = since
        if issued_only:
            payload[POLL_ISSUED_ONLY_KEY] = True
        return self._invoke(session_id, payload)


class MemoryReader:
    """Read a session's transcript events from AgentCore Memory (``ListEvents``). Client injectable.

    Gated on ``AGENTCORE_MEMORY_ID``. One event per message, written by the runtime's transcript
    recorder under the session's actor (``team``, ``slack-user-<UID>`` or ``person-<person_id>``)
    and the session id mapped by :func:`_memory_session_id`.
    """

    def __init__(self, memory_id: str, region: str | None = None, *, client: Any | None = None):
        self._memory_id = memory_id
        self._region = region
        self._client = client

    def client(self) -> Any:
        if self._client is None:
            import boto3

            session = boto3.Session(region_name=self._region) if self._region else boto3.Session()
            self._client = session.client("bedrock-agentcore")
        return self._client

    def transcript_events(
        self, actor_id: str, session_id: str, *, deadline: float
    ) -> tuple[list[dict[str, Any]], bool]:
        """A session's transcript events under ``actor_id``, the newest :data:`TRANSCRIPT_MAX_MESSAGES`.

        Pages ``ListEvents`` (100 per page) up to :data:`MEMORY_MAX_EVENTS` or ``deadline``, then sorts
        by ``(eventTimestamp, seq)`` and keeps the newest. ``truncated`` is set when anything was
        left out (the cap, the page ceiling or the time budget).
        """
        client = self.client()
        out: list[dict[str, Any]] = []
        token: str | None = None
        truncated = False
        while True:
            params: dict[str, Any] = {
                "memoryId": self._memory_id,
                "actorId": actor_id,
                "sessionId": _memory_session_id(session_id),
                "maxResults": 100,
                "includePayloads": True,
            }
            if token:
                params["nextToken"] = token
            resp = client.list_events(**params)
            page = resp.get("events", []) or []
            _count_unknown_payload_members(page)
            out.extend(ev for ev in page if _is_transcript_event(ev))
            token = resp.get("nextToken")
            if not token:
                break
            if len(out) >= MEMORY_MAX_EVENTS or time.monotonic() >= deadline:
                truncated = True
                break
        out.sort(key=_event_sort_key)
        if len(out) > TRANSCRIPT_MAX_MESSAGES:
            out, truncated = out[-TRANSCRIPT_MAX_MESSAGES:], True
        return out, truncated

class SessionStoreReader:
    """Read a session's transcript: AgentCore Memory events, else the session store. Clients injectable.

    Gated on ``STRANDLY_SESSION_BUCKET``. Source order, per session:

    1. the **verbatim** transcript — one AgentCore Memory event per message, under the actor named by
       ``session/<sid>/memory-actor.json`` (``team`` when the marker is missing), read through the
       ``fallback`` :class:`MemoryReader` (so only when ``AGENTCORE_MEMORY_ID`` is configured).
       A ``context`` event's ``injected`` blocks join the message they were written into, and an
       ``offloaded`` event (too large for Memory) is read in full from
       ``context/<sid>/transcript/<seq>.json``;
    2. else the latest **snapshot**'s messages, ``session/<sid>/…/snapshot_latest.json`` — the
       *managed* history (old turns summarized, big tool results cut to previews), but still the
       real conversation.
    """

    def __init__(
        self,
        bucket: str,
        region: str | None = None,
        *,
        client: Any | None = None,
        fallback: MemoryReader | None = None,
    ):
        self._bucket = bucket
        self._region = region
        self._client = client
        self._fallback = fallback

    def client(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config as BotocoreConfig

            session = boto3.Session(region_name=self._region) if self._region else boto3.Session()
            self._client = session.client(
                "s3",
                config=BotocoreConfig(
                    connect_timeout=2,
                    read_timeout=5,
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        return self._client

    def _get(self, key: str) -> bytes | None:
        try:
            resp = self.client().get_object(Bucket=self._bucket, Key=key)
        except Exception as e:  # noqa: BLE001 — only "missing" is mapped; everything else re-raises
            code = getattr(e, "response", {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise
        body: bytes = resp["Body"].read()
        return body

    def _actor(self, session_id: str) -> str:
        """The actor the session's transcript events are under (``team`` without a marker)."""
        raw = self._get(actor_marker_key(session_id))
        try:
            actor = json.loads(raw).get("actor") if raw else None
        except (ValueError, AttributeError):
            actor = None
        return actor if isinstance(actor, str) and actor else MEMORY_TEAM_ACTOR

    def _transcript_items(self, session_id: str) -> tuple[list[tuple[str, dict[str, Any], Any]], bool]:
        if self._fallback is None:
            return [], False
        deadline = time.monotonic() + _TRANSCRIPT_READ_BUDGET_SECONDS
        try:
            events, truncated = self._fallback.transcript_events(
                self._actor(session_id), session_id, deadline=deadline
            )
        except Exception:  # noqa: BLE001 — shown as the snapshot, flagged, never as an error page
            logger.warning(
                "SESSION_STORAGE_FAILURE op=transcript-read session=%s: AgentCore Memory read failed; "
                "showing the session store's snapshot", session_id, exc_info=True,
            )
            return [], True
        items: list[tuple[str, dict[str, Any], Any]] = []
        by_tid: dict[str, int] = {}
        resolved = 0
        for ev in events:
            metadata = ev.get("metadata") or {}
            if (
                _metadata_value(metadata, "offloaded")
                and resolved < _TRANSCRIPT_OFFLOAD_RESOLVE_MAX
                and time.monotonic() < deadline
            ):
                resolved += 1
                ev = self._full_event(session_id, ev)
            message = _event_message(ev)
            if message is None:
                continue
            tid = _metadata_value(metadata, "tid")
            if _metadata_value(metadata, "kind") == "context":
                _attach_context(items, by_tid.get(tid) if tid else _last_user(items), message)
                continue
            raw_ts = ev.get("eventTimestamp")
            ts = raw_ts.isoformat() if hasattr(raw_ts, "isoformat") else raw_ts
            items.append((str(ev.get("eventId") or len(items)), message, ts))
            if tid:
                by_tid[tid] = len(items) - 1
        return items, truncated

    def _full_event(self, session_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """``event`` with the full payload the runtime stored for it, else as recorded (a preview)."""
        seq = _metadata_value(event.get("metadata") or {}, "seq") or ""
        if not _TRANSCRIPT_SEQ.match(seq):
            return event
        try:
            raw = self._get(transcript_payload_key(session_id, seq))
            payload = json.loads(raw) if raw is not None else None
        except Exception:  # noqa: BLE001 — the recorded preview still shows the message
            logger.warning(
                "SESSION_STORAGE_FAILURE op=transcript-payload-read session=%s seq=%s: showing the "
                "preview", session_id, seq, exc_info=True,
            )
            return event
        return {**event, "payload": payload} if isinstance(payload, list) else event

    def _snapshot_items(self, session_id: str) -> list[tuple[str, dict[str, Any], Any]]:
        raw = self._get(snapshot_key(session_id))
        if raw is None:
            return []
        messages = snapshot_messages(raw)
        # Position is the identity: the snapshot is rewritten in place, its order is the history's.
        return [(f"snapshot:{i}", m, None) for i, m in enumerate(messages) if isinstance(m, dict)]

    def _messages(self, session_id: str, *, identified: bool) -> tuple[list[dict[str, Any]], bool] | None:
        items, truncated = self._transcript_items(session_id)
        if items:
            return _store_messages(items, identified=identified), truncated
        snapshot = self._snapshot_items(session_id)
        if not snapshot:
            return None
        return _store_messages(snapshot, identified=identified), truncated

    def pagination_transcript(self, session_id: str) -> tuple[list[dict[str, Any]], bool]:
        """Identified messages plus a truncation flag (older history left out or unreadable)."""
        return self._messages(session_id, identified=True) or ([], False)

    def transcript(self, session_id: str) -> list[dict[str, Any]]:
        """The session as ordered chat messages — see :func:`_store_messages`."""
        found = self._messages(session_id, identified=False)
        return found[0] if found is not None else []


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


def _context_text(block: dict[str, Any]) -> str:
    """An ``injected`` or ``summary`` block's text, clipped to :data:`_TOOL_RESULT_LIMIT`."""
    return _clip(block.get("text"), _TOOL_RESULT_LIMIT) or ""


def _tool_use_id(block: dict[str, Any]) -> dict[str, str]:
    """``{"id": toolUseId}`` when the SDK block carries one, else ``{}`` — the key that pairs a
    ``toolResult`` with its ``toolUse`` (a multi-call turn's results need not arrive in call order)."""
    tid = block.get("toolUseId")
    return {"id": str(tid)} if isinstance(tid, (str, int)) and str(tid) else {}


def _parse_content(content: Any) -> dict[str, Any]:
    """An SDK message's ``content`` list → ``{text, blocks, tools, tool_results}``.

    ``blocks`` is the content IN ITS ORIGINAL ORDER — each text block, each ``toolUse`` (name +
    clipped input) and each ``toolResult`` (status + clipped output) as ``{"kind": "text" | "tool" |
    "result", ...}`` — so a transcript renders the message exactly as the model produced it.
    ``text`` (the text blocks joined), ``tools`` and ``tool_results`` are the same content split by
    kind, for flat renderers. Text the agent added to a turn (an SDK ``injected`` block, or a
    ``summary`` of earlier history) is a ``"context"`` block with its ``source``, only in ``blocks``,
    so it never reads as the user's or the model's words. Raises ``TypeError`` for a non-list so
    callers can treat a malformed message uniformly."""
    if not isinstance(content, list):
        raise TypeError("message content is not a list")
    blocks: list[dict[str, Any]] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        if "text" in b:
            blocks.append({"kind": "text", "text": str(b["text"] if b["text"] is not None else "")})
        elif isinstance(b.get("injected"), dict):
            source = str(b["injected"].get("source") or "")
            blocks.append({"kind": "context", "source": source, "text": _context_text(b["injected"])})
        elif isinstance(b.get("summary"), dict):
            blocks.append({"kind": "context", "source": "summary", "text": _context_text(b["summary"])})
        elif isinstance(b.get("toolUse"), dict):
            blocks.append(
                {
                    "kind": "tool",
                    "name": str(b["toolUse"].get("name") or "tool"),
                    "input": _tool_input_str(b["toolUse"].get("input")),
                    **_tool_use_id(b["toolUse"]),
                }
            )
        elif isinstance(b.get("toolResult"), dict):
            blocks.append(
                {
                    "kind": "result",
                    "status": str(b["toolResult"].get("status") or "unknown"),
                    "text": _tool_result_text(b["toolResult"]),
                    **_tool_use_id(b["toolResult"]),
                }
            )
    text = "\n".join(b["text"] for b in blocks if b["kind"] == "text")
    tools = [{"name": b["name"], "input": b["input"]} for b in blocks if b["kind"] == "tool"]
    tool_results = [{"status": b["status"], "text": b["text"]} for b in blocks if b["kind"] == "result"]
    return {"text": text, "blocks": blocks, "tools": tools, "tool_results": tool_results}


# Block kinds that are dropped when their text is blank.
_TEXT_KINDS = frozenset({"text", "context"})


def _store_messages(
    items: list[tuple[str, dict[str, Any], Any]], *, identified: bool = False,
) -> list[dict[str, Any]]:
    """Session-store messages ``[(identity, sdk_message, timestamp)]`` -> chat messages.

    Each message is ``role``/``text``/``tool_use``/``blocks``/``tools``/``tool_results``/``timestamp``
    (plus ``id`` when ``identified``) — the portal renders either source identically. ``identity`` is
    the event id (transcript) or the snapshot position, so an ``id`` is stable across reads.
    """
    out: list[dict[str, Any]] = []
    for identity, message, ts in items:
        role = str(message.get("role") or "").lower()
        try:
            parsed = _parse_content(message.get("content"))
        except TypeError:
            continue
        text = parsed["text"]
        if not ((text and text.strip()) or parsed["tools"] or parsed["tool_results"]):
            continue
        out.append(
            {
                "role": role,
                "text": text if text and text.strip() else "",
                "tool_use": bool(parsed["tools"]),
                "blocks": [b for b in parsed["blocks"] if b["kind"] not in _TEXT_KINDS or b["text"].strip()],
                "tools": parsed["tools"],
                "tool_results": parsed["tool_results"],
                "timestamp": ts,
            }
        )
        if identified:
            out[-1]["id"] = _transcript_hash(["store", identity])
            del out[-1]["tools"], out[-1]["tool_results"]
    return out


def _clip(text: Any, limit: int) -> str | None:
    if not text:
        return None
    s = str(text)
    return s if len(s) <= limit else s[: limit - 1] + "…"


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
            "ended_at": it.get("ended_at"),
            "prompt": it.get("prompt"),
            "result": it.get("result_summary"),
            "error": it.get("error"),
            # The model the run recorded (rows from before model selection have none).
            **{key: it[key] for key in ("model", "effort") if it.get(key)},
        }
        for it in rows
    ]
    return {"session_id": session_id, "runs": len(turns), "turns": turns}


def _transcript_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _decode_transcript_cursor(raw: Any, session_id: str) -> dict[str, Any]:
    try:
        if not isinstance(raw, str) or len(raw) > 2048 or not re.fullmatch(r"[A-Za-z0-9_-]+", raw):
            raise ValueError
        cursor = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True))
        if (
            not isinstance(cursor, dict)
            or set(cursor) != {"v", "session_id", "source", "anchor", "prefix"}
            or type(cursor["v"]) is not int or cursor["v"] != 1
            or cursor["session_id"] != session_id
            or cursor["source"] not in ("memory", "ledger")
            or not all(isinstance(cursor[k], str) and re.fullmatch(r"[0-9a-f]{64}", cursor[k])
                       for k in ("anchor", "prefix"))
        ):
            raise ValueError
        return cursor
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("invalid transcript cursor") from exc


def _ledger_messages(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages = []
    for turn in sorted(turns, key=lambda t: (str(t.get("started_at") or ""), str(t.get("task_id") or ""))):
        for field, role, timestamp in (
            ("prompt", "user", turn["started_at"]),
            ("error" if turn["error"] else "result", "assistant", turn["ended_at"]),
        ):
            if turn[field]:
                messages.append({
                    "id": _transcript_hash([turn["task_id"], turn["started_at"], field]),
                    "role": role, "text": turn[field], "timestamp": timestamp,
                    "tool_use": False, "failed": field == "error",
                })
    return messages


def _paginated_session_detail(
    reader: LedgerReader, memory: SessionStoreReader | None, session_id: str, query: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    raw_limit = query.get("limit", str(SESSION_MESSAGES_DEFAULT_LIMIT))
    if not isinstance(raw_limit, str) or not re.fullmatch(r"[0-9]{1,3}", raw_limit):
        return 400, {"error": f"limit must be an integer from 1 to {SESSION_MESSAGES_MAX_LIMIT}"}
    limit = int(raw_limit)
    if not 1 <= limit <= SESSION_MESSAGES_MAX_LIMIT:
        return 400, {"error": f"limit must be an integer from 1 to {SESSION_MESSAGES_MAX_LIMIT}"}
    try:
        cursor = _decode_transcript_cursor(query["before"], session_id) if "before" in query else None
    except ValueError as exc:
        return 400, {"error": str(exc)}

    try:
        detail = _session_detail(reader.recent(SESSIONS_WINDOW), session_id)
    except Exception:  # noqa: BLE001 — an unavailable source is not an empty page
        logger.exception("paginated ledger read failed (session_id=%s)", session_id)
        return 503, {"error": "session ledger unavailable", "session_id": session_id}
    source = cursor["source"] if cursor else "memory"
    messages: list[dict[str, Any]] = []
    boundary = None
    if source == "memory":
        if memory is None and cursor:
            return 503, {"error": "memory transcript unavailable", "session_id": session_id}
        if memory is not None:
            try:
                messages, truncated = memory.pagination_transcript(session_id)
                boundary = "memory_event_limit" if truncated else None
            except Exception:  # noqa: BLE001 — only the FIRST page may degrade to ledger
                logger.warning("paginated memory read failed (session_id=%s)", session_id, exc_info=True)
                if cursor:
                    return 503, {"error": "memory transcript unavailable", "session_id": session_id}
        if not messages and not cursor:
            source = "ledger"
    if source == "ledger":
        messages = _ledger_messages(detail["turns"])
        boundary = "ledger_window"

    end = len(messages)
    ids = [m["id"] for m in messages]
    if cursor:
        try:
            end = ids.index(cursor["anchor"])
        except ValueError:
            end = -1
        # Guard the whole pending prefix: a rolling ledger window can lose older rows even
        # while retaining the anchor. Never silently skip that gap or switch transcript sources.
        if end < 0 or _transcript_hash(ids[:end]) != cursor["prefix"]:
            return 409, {"error": "history changed or expired; reload the newest page",
                         "code": "cursor_expired", "session_id": session_id}
    elif not detail["runs"] and not messages:
        return 404, {"error": "session not found", "session_id": session_id}

    start = max(0, end - limit)
    next_cursor = None
    if start:
        payload = {"v": 1, "session_id": session_id, "source": source,
                   "anchor": ids[start], "prefix": _transcript_hash(ids[:start])}
        next_cursor = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    detail.update({"source": source, "messages": messages[start:end],
                   "next_cursor": next_cursor, "history_boundary": boundary})
    detail["turns"] = [
        {k: v for k, v in turn.items() if k not in ("prompt", "result", "error")}
        for turn in detail["turns"]
    ]
    return 200, detail


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    """Decode an HTTP API v2 request body to a dict (handles base64); ``{}`` on anything unparseable."""
    raw = event.get("body") or ""
    if event.get("isBase64Encoded") and raw:
        try:
            raw = base64.b64decode(raw).decode()
        except (ValueError, UnicodeDecodeError):
            return {}
    try:
        data = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


# The runtime's "attach at the live head" poll cursor (``agentcore_app.TAIL_CURSOR``): no backlog,
# just the next sequence number. Mirrored rather than imported — this Lambda ships without the
# strandly package — and pinned against the runtime's value by ``test_infra_constants_sync``.
TAIL_CURSOR = -1


# Upper bound for a forwarded ``invocation_context``, in serialized-JSON characters. The runtime
# clamps what it renders into the prompt (a few KB — ``MAX_INVOCATION_CONTEXT_CHARS`` in the
# harness); this earlier, looser bound just keeps an absurd body from riding the invoke payload at
# all. Oversized/invalid contexts are DROPPED, not truncated: truncated JSON is junk, and the
# context is purely additive — the chat must work identically without it.
_MAX_INVOCATION_CONTEXT_JSON = 16384


def _invocation_context(body: dict[str, Any]) -> dict[str, Any] | None:
    """The chat body's caller context (``invocation_context``/``invocationContext``), validated.

    Only a non-empty JSON object within :data:`_MAX_INVOCATION_CONTEXT_JSON` passes; anything else
    yields ``None`` (never an error — ambient context must not be able to break a chat turn).
    """
    raw = body.get("invocation_context")
    if raw is None:
        raw = body.get("invocationContext")
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        if len(json.dumps(raw)) > _MAX_INVOCATION_CONTEXT_JSON:
            return None
    except (TypeError, ValueError):
        return None
    return raw


_PORTAL_USER_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


def _portal_identity(event: dict[str, Any]) -> tuple[str | None, str | None]:
    """API Gateway verified issuer+subject, never email/username or body identity.

    Direct Lambda invoke principals are trusted: they can synthesize authorizer context,
    just as trusted runtime invoke principals can synthesize trigger fields.
    """
    pool = os.environ.get("COGNITO_USER_POOL_ID", "")
    region = os.environ.get("AWS_REGION", "")
    if not pool or not region:
        return None, None
    expected = f"https://cognito-idp.{region}.amazonaws.com/{pool}"
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    claims = (authorizer.get("jwt") or {}).get("claims") or {}
    subject = claims.get("sub")
    if (
        claims.get("iss") == expected and claims.get("token_use") == "access"
        and isinstance(subject, str) and _PORTAL_USER_RE.fullmatch(subject)
    ):
        return subject, expected
    return None, None


def bound_person(event: dict[str, Any], people: PeopleDirectory | None) -> Person | None:
    """The caller's enabled person, or ``None``.

    Through API Gateway (an authorizer context) the person is only the row bound to the verified
    Cognito ``sub``; ``X-Strandly-Person`` is advisory there, and a login that is not the bound
    person's is logged and ignored, never looked up. A direct Lambda invoke (:func:`is_direct_invoke`:
    no API Gateway and no authorizer context, so only an IAM-trusted principal) is trusted like an
    ingress and names its person with that header (``by_github``, see :func:`direct_invoke_person`).
    """
    if is_direct_invoke(event):
        person = direct_invoke_person(event, people)
        logger.info("chat person (direct invoke): person_id=%s", person.person_id if person else "-")
        return person
    subject, _issuer = _portal_identity(event)
    person = (
        people.introduces(people.find_by_cognito(subject))
        if subject and people is not None else None
    )
    claimed = claimed_github(event)
    if claimed and (person is None or claimed != person.github_login):
        logger.warning(
            "ignoring %s github:%s: the signed-in user (sub=%s) is bound to %s", PERSON_HEADER,
            claimed, subject or "-", person.person_id if person else "no person",
        )
    logger.info("chat person: sub=%s person_id=%s", subject or "-", person.person_id if person else "-")
    return person


def person_fields(event: dict[str, Any], people: PeopleDirectory | None) -> dict[str, Any]:
    """The runtime payload fields for the caller's person (``{}`` when there is none).

    A person a direct invoke named also carries :data:`DIRECT_INVOKE_KEY` (``directInvoke: true``),
    the runtime's only reason to accept person fields without a verified ``portalUser``. Only this
    function sets it, from :func:`is_direct_invoke`; nothing is copied from the body or headers.
    """
    person = bound_person(event, people)
    if person is None:
        return {}
    fields: dict[str, Any] = {"personId": person.person_id}
    if is_direct_invoke(event):
        fields[DIRECT_INVOKE_KEY] = True
    for key, value in (("personGithub", person.github_login), ("personName", person.name),
                       ("personRole", person.role), ("personSlackUserId", person.slack_user_id)):
        if value:
            fields[key] = value
    return fields


def _whoami(event: dict[str, Any], people: PeopleDirectory | None) -> dict[str, Any]:
    """The caller's own bound row and nothing else: no header selects another person's."""
    person = bound_person(event, people)
    if person is None:
        return {"github": None, "person": None}
    return {"github": person.github_login or None,
            "person": {"person_id": person.person_id, "name": person.name, "role": person.role,
                       "github_login": person.github_login, "slack_user_id": person.slack_user_id}}


def _chat_model(body: dict[str, Any]) -> tuple[str | None, str | None] | None:
    """The ``(model, effort)`` a chat body asks for, each ``None`` when omitted; ``None`` for neither.

    Checks type and size only, with the runtime's rules (``_model_choice_of`` in
    serve/agentcore_app.py); the runtime's harness model resolution decides what a choice means.

    Raises:
        ValueError: If ``model`` or ``effort`` is not a short string of printable characters.
    """
    choice: list[str | None] = []
    for key, limit in ((CHAT_MODEL_KEY, CHAT_MODEL_MAX_CHARS), (CHAT_EFFORT_KEY, CHAT_EFFORT_MAX_CHARS)):
        value = body.get(key)
        if value is None or value == "":
            choice.append(None)
        elif not isinstance(value, str) or len(value) > limit or not value.isprintable():
            raise ValueError(f"'{key}' must be a string of at most {limit} printable characters")
        else:
            choice.append(value)
    model, effort = choice
    return None if model is None and effort is None else (model, effort)


def _canonical_session_id(raw: Any) -> str:
    """Use the native key normalization, while keeping missing/blank IDs invalid."""
    text = str(raw or "").strip()
    return _sanitize_session_id(text) if text else ""


def _session_values(values: dict[str, Any]) -> dict[str, Any]:
    """Resolve aliases once, before authorization; never mutate the request."""
    sessions = [_canonical_session_id(values[key]) for key in ("session_id", "sessionId")
                if key in values]
    if not sessions:
        return values
    if len(set(sessions)) != 1:
        raise ValueError("conflicting session_id and sessionId")
    return {**{key: value for key, value in values.items() if key != "sessionId"},
            "session_id": sessions[0]}


def _receipt_chat(event: dict[str, Any], method: str, invoker: RuntimeInvoker,
                  values: dict[str, Any], people: PeopleDirectory | None = None) -> tuple[int, dict[str, Any]]:
    """Exact-run operations are reserved to a verified caller's ACP session namespace."""
    subject, issuer = _portal_identity(event)
    if not subject or not issuer:
        return 401, {"error": "verified access-token context required"}
    prefix = "acp-" + hashlib.sha256(f"{issuer}:{subject}".encode()).hexdigest()[:24] + "-"
    if method == "GET" and values.get("capabilities") == "acp":
        # A poll is read-only even on old runtimes that do not understand this probe.
        support = invoker.receipt({"sessionId": prefix + "0" * 32, "action": "poll",
                                   "capabilities": "runReceipts"})
        if support.get("runReceipts") != 1:
            return 503, {"error": "runtime lacks exact-run receipt support"}
        return 200, {"runReceipts": 1, "sessionPrefix": prefix}
    sid, rid = values.get("session_id"), values.get("result_id")
    if (not isinstance(sid, str) or not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{32}", sid)
            or not isinstance(rid, str) or not re.fullmatch(r"[0-9a-f]{32}", rid)):
        return 400, {"error": "invalid receipt or session ID"}
    payload: dict[str, Any] = {"sessionId": sid, "resultId": rid}
    allowed = {"session_id", "result_id", "since"} if method == "GET" else {"session_id", "result_id", "action", "message"}
    if set(values) - allowed:
        return 400, {"error": "receipt overrides are unsupported"}
    if method == "GET":
        payload["action"] = "poll"
        since = values.get("since", "0")
        try:
            payload["since"] = max(0, int(since))
        except (ValueError, TypeError):
            return 400, {"error": "invalid event cursor"}
    elif values.get("action") == "cancel":
        if set(values) - {"session_id", "result_id", "action"}:
            return 400, {"error": "receipt cancellation overrides are unsupported"}
        payload["action"] = "cancel"
    else:
        message = values.get("message")
        if not isinstance(message, str) or not message.strip():
            return 400, {"error": "message required"}
        if set(values) - {"session_id", "result_id", "message"}:
            return 400, {"error": "receipt launch overrides are unsupported"}
        payload.update(prompt=message, triggerSource="acp:prompt", portalUser=subject, portalIssuer=issuer)
        payload.update(person_fields(event, people))
    return 200, invoker.receipt(payload)


def _chat(event: dict[str, Any], method: str, invoker: RuntimeInvoker | None,
          people: PeopleDirectory | None = None) -> tuple[int, dict[str, Any]]:
    """Handle the chat routes: POST launches a turn, GET polls one. 503 when chat is disabled."""
    if method == "GET" and (event.get("queryStringParameters") or {}).get("whoami") == "1":
        return 200, _whoami(event, people)
    if invoker is None:
        return 503, {"error": "chat not configured (STRANDLY_RUNTIME_ARN unset)"}
    values = _parse_body(event) if method == "POST" else (event.get("queryStringParameters") or {})
    try:
        values = _session_values(values)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    session_id = values.get("session_id", "")
    if "result_id" in values or values.get("capabilities") == "acp":
        return _receipt_chat(event, method, invoker, values, people)
    if session_id.startswith("acp-"):
        return 400, {"error": "ACP sessions require exact-run operations"}
    if method == "POST":
        body = values
        message = str(body.get("message") or "").strip()
        # Stop what's running on this session without starting another run.
        if str(body.get("action") or "").strip() == "cancel":
            if not session_id:
                return 400, {"error": "cancel requires 'session_id'"}
            return 200, invoker.cancel(session_id)
        if not session_id or not message:
            return 400, {"error": "chat requires 'session_id' and 'message'"}
        # Portal role + provenance, forwarded to the runtime for the ledger (see the work-product contract). The
        # publish handshake sends mode="post" + trigger_source="portal:curate"; accept the camelCase
        # spelling too. "stream" is the transport selector, never a portal mode — reject it here.
        mode = str(body.get("mode") or "").strip() or None
        if mode == "stream":
            mode = None
        trigger_source = (
            str(body.get("trigger_source") or body.get("triggerSource") or "").strip() or None
        )
        # The body is the browser's: it may only name a portal trigger, never another surface's.
        if trigger_source and not trigger_source.startswith("portal:"):
            trigger_source = "portal:chat"
        try:
            model = _chat_model(body)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        invocation_context = _invocation_context(body)
        user, issuer = _portal_identity(event)
        person = person_fields(event, people)
        # The runtime takes person fields only from a portal or ACP source.
        if person and not trigger_source:
            trigger_source = "portal:chat"
        launched = invoker.launch(
            session_id,
            message,
            mode=mode,
            trigger_source=trigger_source,
            invocation_context=invocation_context,
            **({"person": person} if person else {}),
            **({"portal_user": user, "portal_issuer": issuer} if user and issuer else {}),
            **({"model": model} if model is not None else {}),
        )
        # The runtime refuses a model choice its harness cannot build in a 200 body (AgentCore
        # hides a 4xx body); it is the caller's error, so it is ours to answer as a 400.
        if model is not None and launched.get("status") == "error":
            return 400, {"error": str(launched.get("error") or "the runtime refused the model choice")}
        return 200, launched
    if method == "GET":
        q = values
        task_id = str(q.get("task_id") or "").strip()
        if not task_id or not session_id:
            return 400, {"error": "poll requires 'task_id' and 'session_id'"}
        # Optional live-event cursor: `since=<seq>` asks the runtime for the run's buffered
        # events from that sequence on (incremental streaming over polling — the portal advances the
        # cursor each poll). Absent/garbage → classic status-only poll, fully backward compatible.
        # A NEGATIVE cursor is the runtime's "attach at the live head" sentinel (TAIL_CURSOR): no
        # backlog, just the cursor. The SPA uses it when it joins a run that was already in flight,
        # so it must survive this hop — clamping it to 0 replayed the whole buffer (and a full
        # buffer is megabytes, well past what this Lambda can return).
        since: int | None = None
        raw_since = (q or {}).get("since")
        if raw_since is not None and str(raw_since).strip() != "":
            try:
                since = max(TAIL_CURSOR, int(str(raw_since).strip()))
            except (TypeError, ValueError):
                since = None
        return 200, invoker.poll(session_id, task_id, since=since)
    return 405, {"error": "method not allowed"}


def _work_products(
    event: dict[str, Any], method: str, reader: WorkProductReader | None
) -> tuple[int, dict[str, Any]]:
    """``GET /api/workproducts?session_id=<sid>[&kind=][&limit=]`` — one GitHub item's work
    products, newest-first, off their own DynamoDB table, optionally of one ``kind``.

    Gated on the table (``STRANDLY_WORK_PRODUCT_TABLE``): unconfigured → 503, exactly like chat on
    ``STRANDLY_RUNTIME_ARN``. ``session_id`` is required (400 without it).
    """
    if reader is None:
        return 503, {"error": "work products not configured (STRANDLY_WORK_PRODUCT_TABLE unset)"}
    if method != "GET":
        return 405, {"error": "method not allowed"}
    try:
        q = _session_values(event.get("queryStringParameters") or {})
    except ValueError as exc:
        return 400, {"error": str(exc)}
    session_id = q.get("session_id", "")
    if not session_id:
        return 400, {"error": "session_id query parameter required"}
    limit = _clamp_limit(q.get("limit"))
    kind = (q.get("kind") or "").strip()
    items = reader.by_session(session_id, limit=limit)
    if kind:
        items = [item for item in items if item.get("kind") == kind]
    return 200, {"items": items}


def _path_segments(path: str) -> list[str]:
    return [seg for seg in path.split("/") if seg]


def route(
    event: dict[str, Any],
    reader: LedgerReader | None,
    invoker: RuntimeInvoker | None = None,
    memory: SessionStoreReader | None = None,
    work_products: WorkProductReader | None = None,
    titles: SessionTitleStore | None = None,
    people: PeopleDirectory | None = None,
) -> tuple[int, dict[str, Any]]:
    """Pure router: (status_code, body) for an HTTP API v2 event. No AWS unless a reader is used."""
    http = (event.get("requestContext") or {}).get("http") or {}
    method = (http.get("method") or "GET").upper()
    path = event.get("rawPath") or http.get("path") or "/"
    path = path.rstrip("/") or "/"
    segments = _path_segments(path)
    not_found = (404, {"error": "not found", "path": path})

    # Chat (launch/poll) talks to the runtime, not the ledger — handle before the GET-only gate.
    if segments == ["api", "chat"]:
        try:
            return _chat(event, method, invoker, people)
        except RuntimeStarting:
            return 504, dict(RUNTIME_STARTING)

    if segments == ["api", "workproducts"]:
        return _work_products(event, method, work_products)

    is_sessions = segments == ["api", "sessions"]
    is_session_detail = len(segments) == 3 and segments[:2] == ["api", "sessions"]
    if not (is_sessions or is_session_detail):
        return not_found

    if method != "GET" and not (method == "PATCH" and is_session_detail):
        return 405, {"error": "method not allowed"}

    if reader is None:  # chat and work products are the only routes that don't need the table
        return 500, {"error": "ledger table not configured"}

    if is_sessions:
        sessions = _sessions(reader.recent(SESSIONS_WINDOW))
        known = _read_titles(titles, [s["session_id"] for s in sessions])
        for s in sessions:
            s["title"] = known.get(s["session_id"])
        return 200, {"sessions": sessions}

    sid = _canonical_session_id((event.get("pathParameters") or {}).get("id"))
    if not sid:
        return 404, {"error": "session not found", "session_id": sid}
    if method == "PATCH":
        return _update_session(event, reader, memory, titles, sid, bound_person(event, people))
    query = event.get("queryStringParameters") or {}
    if "limit" in query or "before" in query:
        status, page = _paginated_session_detail(reader, memory, sid, query)
        if status == 200:
            page["title"] = _read_title(titles, sid)
        return status, page
    detail = _session_detail(reader.recent(SESSIONS_WINDOW), sid)
    detail["source"] = "ledger"
    # Prefer the stored conversation when a transcript reader is wired (the session store's
    # verbatim log / snapshot, or AgentCore Memory for a pre-store session — the real messages,
    # including the agent's own narration), falling back to the ledger-derived transcript on any
    # error or when it has nothing — telemetry must never take the chat panel down. The
    # response's `source` stays "memory" for any stored transcript (the portal contract).
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
        detail["title"] = _read_title(titles, sid)
        return 200, detail
    return 404, {"error": "session not found", "session_id": sid}


def _read_titles(titles: SessionTitleStore | None, session_ids: list[str]) -> dict[str, str]:
    """Titles for a session list; a failed read degrades to untitled rows, never a failed list."""
    if titles is None or not session_ids:
        return {}
    try:
        return titles.titles(session_ids)
    except Exception:  # noqa: BLE001 - titles are decoration; the list must still load
        logger.warning("session title batch read failed (%d sessions)", len(session_ids), exc_info=True)
        return {}


def _read_title(titles: SessionTitleStore | None, session_id: str) -> str | None:
    if titles is None:
        return None
    try:
        return titles.title(session_id)
    except Exception:  # noqa: BLE001 - titles are decoration; the transcript must still load
        logger.warning("session title read failed (session_id=%s)", session_id, exc_info=True)
        return None


def _session_exists(reader: LedgerReader, memory: SessionStoreReader | None, session_id: str) -> bool:
    """The same existence rule as GET /api/sessions/{id}: a ledger run, else a stored transcript."""
    if _session_detail(reader.recent(SESSIONS_WINDOW), session_id)["runs"]:
        return True
    if memory is None:
        return False
    try:
        return bool(memory.transcript(session_id))
    except Exception:  # noqa: BLE001 - unreadable transcript store: treat as unknown, not as found
        logger.warning("session existence check failed (session_id=%s)", session_id, exc_info=True)
        return False


def _update_session(
    event: dict[str, Any],
    reader: LedgerReader,
    memory: SessionStoreReader | None,
    titles: SessionTitleStore | None,
    session_id: str,
    person: Person | None = None,
) -> tuple[int, dict[str, Any]]:
    """PATCH /api/sessions/{id} ``{"title": str | null}``: set, replace, or (empty/null) clear."""
    if titles is None:
        return 503, {"error": "session titles not configured"}
    body = _parse_body(event)
    if set(body) != {"title"}:
        return 400, {"error": "body must be exactly {\"title\": string | null}"}
    raw = body["title"]
    try:
        title = "" if raw is None else clean_session_title(raw)
    except (TypeError, ValueError) as exc:
        return 400, {"error": str(exc)}
    if not _session_exists(reader, memory, session_id):
        return 404, {"error": "session not found", "session_id": session_id}
    user, _issuer = _portal_identity(event)
    try:
        titles.put(session_id, title, updated_by=f"portal:{user}" if user else "portal",
                   person=_person_label(person))
    except Exception:  # noqa: BLE001 - surfaced to the caller, which rolls back its optimistic edit
        logger.exception("session title write failed (session_id=%s)", session_id)
        return 503, {"error": "session title store unavailable", "session_id": session_id}
    logger.info("session title %s (session_id=%s)", "set" if title else "cleared", session_id)
    return 200, {"session_id": session_id, "title": title or None}


def _person_label(person: Person | None) -> str | None:
    """How a session row names a bound person: ``github:<login>``, else ``person:<id>``."""
    if person is None:
        return None
    return f"github:{person.github_login}" if person.github_login else f"person:{person.person_id}"


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


def _build_title_store() -> SessionTitleStore | None:
    table_name = os.environ.get("RUN_LEDGER_TABLE")
    if not table_name:
        return None
    import boto3

    return SessionTitleStore(boto3.resource("dynamodb"), table_name)


def _build_work_product_reader() -> WorkProductReader | None:
    """A work-product reader when ``STRANDLY_WORK_PRODUCT_TABLE`` is set, else ``None`` (the portal
    routes 503, like chat without a runtime arn)."""
    table_name = os.environ.get("STRANDLY_WORK_PRODUCT_TABLE")
    if not table_name:
        return None
    import boto3

    return WorkProductReader(boto3.resource("dynamodb").Table(table_name))


def _build_invoker() -> RuntimeInvoker | None:
    runtime_arn = os.environ.get("STRANDLY_RUNTIME_ARN")
    if not runtime_arn:
        return None
    return RuntimeInvoker(runtime_arn, os.environ.get("AWS_REGION"))


_PEOPLE: list[PeopleDirectory] = []


def _build_people() -> PeopleDirectory | None:
    """The people directory when ``STRANDLY_PEOPLE_TABLE`` is set (one client per container)."""
    table = os.environ.get(PEOPLE_TABLE_ENV)
    if not table:
        return None
    if not _PEOPLE:
        import boto3

        _PEOPLE.append(PeopleDirectory(boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION")), table))
    return _PEOPLE[0]


def _build_memory_reader() -> MemoryReader | None:
    """A Memory transcript reader when ``AGENTCORE_MEMORY_ID`` is set, else ``None``."""
    memory_id = os.environ.get("AGENTCORE_MEMORY_ID")
    if not memory_id:
        return None
    return MemoryReader(memory_id, os.environ.get("AWS_REGION"))


def _build_transcript_reader() -> SessionStoreReader | None:
    """The transcript reader: the session store (``STRANDLY_SESSION_BUCKET``: actor markers and
    snapshots) over AgentCore Memory transcript events (when ``AGENTCORE_MEMORY_ID`` is set); else
    ``None`` (the ledger-derived transcript)."""
    bucket = os.environ.get("STRANDLY_SESSION_BUCKET")
    if not bucket:
        return None
    return SessionStoreReader(bucket, os.environ.get("AWS_REGION"), fallback=_build_memory_reader())


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """AWS Lambda entrypoint: route, then serialize to an HTTP API v2 proxy response."""
    try:
        status, body = route(
            event,
            _build_reader(),
            _build_invoker(),
            _build_transcript_reader(),
            _build_work_product_reader(),
            _build_title_store(),
            _build_people(),
        )
    finally:
        _report_unknown_payload_members()
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "cache-control": "no-store",
        },
        "body": json.dumps(body, cls=_DecimalEncoder),
    }
