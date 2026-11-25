"""Dashboard-API transport (the default) — ``POST/GET /api/chat`` with a Cognito bearer token.

The deployed dashboard's HTTP API launches and polls runs with the Lambda's own IAM role, so the
caller needs a Cognito identity (``strandly-mcp login``), never AWS credentials. Contract:
harness ``dashboard/api/handler.py`` (``_chat`` for ``/api/chat``; ``_sessions`` /
``_session_detail`` for the read routes).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from strandly_mcp import auth as _auth
from strandly_mcp import identity as _identity
from strandly_mcp.auth import NotLoggedInError, ensure_access_token, save_auth

# The poll cadence — matches the SPA's ~1–2s rhythm; fast enough for live tool-activity
# notifications, slow enough to stay negligible for the Lambda.
POLL_INTERVAL_S = 1.0

# session(id) clips: transcripts can run to hundreds of KB; an MCP tool result should not.
SESSION_TEXT_CLIP = 4000
SESSION_MAX_ENTRIES = 60
# ...and with include_tools, each tool input / tool result block is clipped harder than prose.
SESSION_TOOL_CLIP = 600
_TOOL_FIELDS = ("tools", "tool_results", "tool_use")


class DashboardApiError(RuntimeError):
    """The dashboard API refused/failed a call — the message is surfaced verbatim to the tool."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


#: What the portal SPA sends for a chat turn (``web-next/src/api/client.ts``).
PORTAL_CHAT_TRIGGER = "portal:chat"


class DashboardClient:
    """Bearer-token client for the deployed dashboard API.

    Wraps the token-cache seam (``auth.py``): every call presents a fresh access token (silently
    refreshed off the cached refresh token near expiry), a 401 forces ONE refresh + retry (covers a
    token revoked/expired server-side before the local clock says so), and a second 401 means the
    session is dead → :class:`NotLoggedInError`. A refreshed token is written back to
    ``~/.strandly/auth.json`` best-effort so the next proxy process starts warm.

    Every request carries ``X-Strandly-Person`` when an identity is cached next to the login
    (``identity.py``); ``person`` overrides that (``None`` sends no header).
    """

    def __init__(
        self,
        auth: dict[str, Any],
        *,
        request: _auth.HttpRequest = _auth.http_request,
        auth_file: Path | None = None,
        person: str | None | object = _identity.FROM_CACHE,
    ):
        api_url = str(auth.get("api_url") or "").rstrip("/")
        if not api_url:
            raise NotLoggedInError(f"cached login has no api_url — {_auth.LOGIN_HINT}")
        self._auth = dict(auth)
        self._api_url = api_url
        self._request = request
        self._auth_file = auth_file
        if person is _identity.FROM_CACHE:
            person = _identity.person_header(_identity.load_identity(_identity.identity_path(auth_file)))
        self.person: str | None = person if isinstance(person, str) and person else None

    def _bearer(self, *, force_refresh: bool = False) -> str:
        auth = dict(self._auth)
        if force_refresh:
            auth.pop("access_token", None)  # make ensure_access_token take the refresh path
        token, refreshed_auth, refreshed = ensure_access_token(auth, request=self._request)
        if refreshed:
            self._auth = refreshed_auth
            try:
                save_auth(refreshed_auth, self._auth_file)
            except OSError:
                pass  # cache write-back is an optimization, never a failure
        return token

    def _call(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._api_url + path
        if query:
            url += "?" + urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode("utf-8") if body is not None else None

        def send(token: str) -> tuple[int, bytes]:
            headers = {"Authorization": f"Bearer {token}"}
            if self.person:
                headers[_identity.PERSON_HEADER] = self.person
            if data is not None:
                headers["Content-Type"] = "application/json"
            return self._request(method, url, headers=headers, data=data)

        try:
            status, raw = send(self._bearer())
            if status == 401:
                status, raw = send(self._bearer(force_refresh=True))
        except OSError as e:  # DNS/refused/TLS — no status to act on
            raise DashboardApiError(f"could not reach the dashboard API at {url}: {e}") from e
        if status == 401:
            raise NotLoggedInError(
                f"the dashboard API rejected the session (401) — {_auth.LOGIN_HINT}"
            )
        if status == 503:
            raise DashboardApiError(
                "chat is not enabled on this deployment (503 — STRANDLY_RUNTIME_ARN unset on the "
                "dashboard API)",
                status,
            )
        if status >= 400:
            raise DashboardApiError(f"{method} {path} failed ({status}): {raw[:300]!r}", status)
        return json.loads(raw)

    # -- the three /api/chat verbs (contract: dashboard/api/handler.py::_chat) --

    def launch(
        self, session_id: str, message: str, *, trigger_source: str | None = PORTAL_CHAT_TRIGGER
    ) -> dict[str, Any]:
        """Launch a detached turn → ``{status: accepted, taskId}`` (it supersedes a running one).

        ``trigger_source`` defaults to the portal SPA's ``portal:chat``. ``/api/chat`` is a portal
        surface acting for the signed-in Cognito user, and the API rewrites any non-``portal:``
        value to ``portal:chat`` anyway; but a launch that sent none was recorded with an empty
        source, so the agent did not treat the user as the run's requester (``schedule`` was
        read-only). ``None`` sends no source.
        """
        body: dict[str, Any] = {"session_id": session_id, "message": message}
        if trigger_source:
            body["trigger_source"] = trigger_source
        return self._call("POST", "/api/chat", body=body)

    def cancel(self, session_id: str) -> dict[str, Any]:
        """Stop the session's in-flight run → ``{status: cancelled|idle, …}``."""
        return self._call("POST", "/api/chat", body={"action": "cancel", "session_id": session_id})

    def poll(self, session_id: str, task_id: str, since: int | None = None) -> dict[str, Any]:
        """Poll a run; a ``since`` cursor also returns its live events + ``nextSeq``."""
        return self._call(
            "GET",
            "/api/chat",
            query={"task_id": task_id, "session_id": session_id, "since": since},
        )

    def whoami(self) -> dict[str, Any] | None:
        """What the API made of this client's ``X-Strandly-Person`` (``GET /api/chat?whoami=1``).

        ``{"github": login | None, "person": {...} | None}``; ``None`` when the deployment
        predates the probe (it answers 400).
        """
        try:
            return self._call("GET", "/api/chat", query={"whoami": "1"})
        except DashboardApiError as exc:
            if exc.status == 400:
                return None
            raise

    # -- the session read routes (contract: handler.py::_sessions / _session_detail) --

    def sessions(self) -> list[dict[str, Any]]:
        """Recent sessions, newest-active first (``GET /api/sessions``)."""
        return list(self._call("GET", "/api/sessions").get("sessions") or [])

    def session(self, session_id: str) -> dict[str, Any] | None:
        """One session's transcript (``GET /api/sessions/{id}``); ``None`` when nothing ran on it."""
        try:
            return self._call("GET", f"/api/sessions/{quote(session_id, safe='')}")
        except DashboardApiError as e:
            if e.status == 404:
                return None
            raise


def clip_text(text: Any, limit: int) -> Any:
    if not isinstance(text, str) or len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def summarize_sessions(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """The ``session()`` list view: the fields a caller needs to pick a thread, ``limit`` rows.

    The id is keyed ``session`` — the name of the ``ask`` argument it is meant to be passed to.
    """
    keys = ("title", "description", "last_status", "last_activity", "target", "runs")
    return [{"session": row.get("session_id"), **{k: row.get(k) for k in keys}} for row in rows[: max(0, limit)]]


def _clip_block(block: Any) -> Any:
    """One ordered transcript block (text / tool / result), its string fields clipped."""
    if not isinstance(block, dict):
        return block
    limit = SESSION_TEXT_CLIP if block.get("kind") == "text" else SESSION_TOOL_CLIP
    return {k: clip_text(v, limit) for k, v in block.items()}


def _tool_count(entry: dict[str, Any]) -> int:
    """How many tool uses / results a message carries, whichever shape the API sent."""
    blocks = entry.get("blocks")
    if isinstance(blocks, list):
        return sum(1 for b in blocks if isinstance(b, dict) and b.get("kind") in ("tool", "result"))
    return len(entry.get("tools") or []) + len(entry.get("tool_results") or [])


def clip_session(detail: dict[str, Any], *, include_tools: bool = False) -> dict[str, Any]:
    """The ``session(id)`` transcript view: bounded, clipped, and conversation-only by default.

    ``messages`` (verbatim, from AgentCore Memory) is preferred when the API has it; otherwise the
    ledger-derived ``turns`` (prompt/result per run). The API sends each message's content up to
    three times (``text``, ``tools``/``tool_results``, and the ordered ``blocks``), and tool
    payloads are most of a transcript's bytes, so by default the tool activity is dropped: pure
    tool-use / tool-result messages are skipped (counted in ``tool_messages_hidden``), and a
    message that also talks keeps its ``text`` plus a ``tool_calls`` count. ``include_tools``
    returns each message's ordered ``blocks`` instead (text blocks clipped to
    :data:`SESSION_TEXT_CLIP`, tool inputs/results to :data:`SESSION_TOOL_CLIP`), without the
    duplicate flat fields. Only the last :data:`SESSION_MAX_ENTRIES` entries *after* filtering are
    kept (the tail is what a caller joining a thread needs); ``truncated`` counts the rest.
    """
    out: dict[str, Any] = {
        "session": detail.get("session_id"),
        "source": detail.get("source"),
        "runs": detail.get("runs"),
    }
    key = "messages" if detail.get("messages") else "turns"
    shaped: list[dict[str, Any]] = []
    hidden = 0
    for entry in detail.get(key) or []:
        e = dict(entry)
        for field in ("text", "prompt", "result", "error"):
            if field in e:
                e[field] = clip_text(e[field], SESSION_TEXT_CLIP)
        if key == "messages":
            n_tools = _tool_count(e)
            blocks = e.pop("blocks", None)
            for field in _TOOL_FIELDS:
                e.pop(field, None)
            if include_tools:
                if isinstance(blocks, list):
                    e.pop("text", None)  # the text blocks are in `blocks`, in order
                    e["blocks"] = [_clip_block(b) for b in blocks]
                elif n_tools:
                    tools = [_clip_block(t) for t in entry.get("tools") or []]
                    results = [_clip_block(r) for r in entry.get("tool_results") or []]
                    e["blocks"] = tools + results
            else:
                if not (isinstance(e.get("text"), str) and e["text"].strip()):
                    hidden += 1
                    continue
                if n_tools:
                    e["tool_calls"] = n_tools
        shaped.append(e)
    dropped = max(0, len(shaped) - SESSION_MAX_ENTRIES)
    out[key] = shaped[dropped:]
    if dropped:
        out["truncated"] = dropped
    if hidden:
        out["tool_messages_hidden"] = hidden
    return out


async def run_dashboard_collect(
    client: DashboardClient,
    session_id: str,
    prompt: str,
    on_activity: Callable[[str], Awaitable[None]] | None = None,
    *,
    poll_interval: float = POLL_INTERVAL_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """One chat turn over the dashboard API: launch, cursor-poll to completion, return the text.

    The launch is detached (``{taskId}``); we then poll with a ``since`` cursor so tool activity
    surfaces as MCP log notifications while the run is live — the same "streaming over polling"
    channel the SPA uses. A newer message on the same session supersedes this run, which then ends
    ``cancelled``.

    The answer is ``lastTurnResult`` (the final turn) when the runtime says it, not the merged
    multi-turn ``result`` — the same supersede semantics the SigV4 path gets by resetting its
    buffer on ``turn_start``.
    """
    launched = await asyncio.to_thread(client.launch, session_id, prompt, trigger_source="mcp:ask")
    task_id = str(launched.get("taskId") or "")
    if not task_id:
        return f"[error] launch failed: {json.dumps(launched)}"
    cursor = 0
    while True:
        resp = await asyncio.to_thread(client.poll, session_id, task_id, cursor)
        if on_activity is not None:
            for ev in resp.get("events") or []:
                if ev.get("kind") == "tool_start":
                    await on_activity(f"→ {ev.get('tool') or 'tool'}")
        next_seq = resp.get("nextSeq")
        if isinstance(next_seq, int):
            cursor = next_seq
        status = resp.get("status")
        if status == "failed":
            return f"[error] {resp.get('error') or 'the deployed run failed'}"
        if status == "cancelled":
            return (
                "[cancelled] the run was stopped or superseded by a newer message on this "
                f"session; read the session with session({session_id!r})."
            )
        if status == "completed":
            # ``lastTurnResult`` is exactly the final turn's text ("" is a real, tools-only
            # answer); ``result`` merges every turn (a critic retry's superseded draft included).
            # Absent (an older runtime, a Memory-resolved status) → fall back to ``result``.
            last_turn = resp.get("lastTurnResult")
            if isinstance(last_turn, str):
                return last_turn
            return str(resp.get("result") or "")
        await sleep(poll_interval)
