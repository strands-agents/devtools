"""Direct SigV4 transport (``serve --agentcore``) — ``bedrock-agentcore:InvokeAgentRuntime``.

The admin/break-glass path: works without the dashboard stack, but needs ambient AWS credentials
that can invoke the runtime — the same requirement as the harness's ``strandly invoke``/``poll``.
``boto3`` is imported lazily and only on this path; it ships behind the ``[aws]`` extra so the
default wheel stays ``mcp`` + stdlib.

The runtime-session-id helpers are vendored at build time from the shared stdlib module.
The invoke/launch/poll/stream client remains a self-contained copy of
``strandly.ops.runtime_client``: the thin package cannot import the harness, and the
harness's ``ops`` tree is bundled into trigger Lambdas with
``--no-deps`` so it cannot import this package either. The harness test tree pins the two copies
against each other (``tests/unit/serve/test_mcp_package.py``).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

from strandly_mcp._session_primitives import (
    RUNTIME_SESSION_ID_MIN_LEN as RUNTIME_SESSION_ID_MIN_LEN,
)
from strandly_mcp._session_primitives import (
    runtime_session_id as runtime_session_id,
)
from strandly_mcp._session_primitives import (
    sanitize_session_id as sanitize_session_id,
)

_SENTINEL = object()


class AwsExtraMissingError(RuntimeError):
    """``--agentcore`` needs boto3, which ships behind the ``[aws]`` extra."""

    def __init__(self) -> None:
        super().__init__(
            "the --agentcore transport needs boto3 — install `strandly-mcp[aws]` "
            "(or drop --agentcore to go through the dashboard API with `strandly-mcp login`)"
        )


# InvokeAgentRuntime client config — mirrors strandly.ops.runtime_client (this thin package
# can't import the harness). A cold session can take over a minute to accept its first
# invoke; botocore's defaults (60 s read timeout + automatic retries) time out and RE-SEND it, and
# AgentCore runs both. So: a 300 s read timeout for every call, and exactly ONE attempt for a launch
# (a payload without ``action`` — not idempotent; ``total_max_attempts=1``, since botocore's
# ``max_attempts=1`` means one *retry*). Poll stays retried (idempotent).
INVOKE_READ_TIMEOUT_S = 300
INVOKE_CONNECT_TIMEOUT_S = 10
LAUNCH_TOTAL_MAX_ATTEMPTS = 1
CONTROL_TOTAL_MAX_ATTEMPTS = 3


def client_config(*, launch: bool) -> Any:
    from botocore.config import Config

    return Config(
        read_timeout=INVOKE_READ_TIMEOUT_S,
        connect_timeout=INVOKE_CONNECT_TIMEOUT_S,
        retries={
            "total_max_attempts": LAUNCH_TOTAL_MAX_ATTEMPTS if launch else CONTROL_TOTAL_MAX_ATTEMPTS,
            "mode": "standard",
        },
    )


def _client(region: str, *, launch: bool = False) -> Any:
    try:
        import boto3
    except ImportError as e:
        raise AwsExtraMissingError() from e
    return boto3.Session(region_name=region).client(
        "bedrock-agentcore", config=client_config(launch=launch)
    )


def _read_body(body: Any) -> str:
    raw = body.read() if hasattr(body, "read") else body
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    return raw or ""


def _invoke(runtime_arn: str, region: str, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = _client(region, launch=not payload.get("action")).invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=runtime_session_id(session_id),
        payload=json.dumps(payload).encode(),
        contentType="application/json",
    )
    raw = _read_body(resp.get("response"))
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {"raw": raw}


def launch_run(
    runtime_arn: str,
    region: str,
    session_id: str,
    prompt: str,
    *,
    trigger_source: str | None = None,
) -> dict[str, Any]:
    """Start a fire-and-forget run → ``{status: accepted, taskId}`` (or an error dict)."""
    payload: dict[str, Any] = {"prompt": prompt, "sessionId": session_id}
    if trigger_source:
        payload["triggerSource"] = trigger_source
    return _invoke(runtime_arn, region, session_id, payload)


def poll_run(runtime_arn: str, region: str, session_id: str, task_id: str) -> dict[str, Any]:
    """Poll a run → ``{status: running|completed|cancelled|failed|unknown, result?|error?}``."""
    return _invoke(
        runtime_arn, region, session_id, {"action": "poll", "taskId": task_id, "sessionId": session_id}
    )


def _parse_sse(lines: Iterator[bytes | str]) -> Iterator[dict[str, Any]]:
    """Parse ``data: {json}`` SSE lines into event dicts (pure; testable without boto)."""
    for line in lines:
        if isinstance(line, (bytes, bytearray)):
            line = line.decode("utf-8", "replace")
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data:
            continue
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            yield {"kind": "text", "text": data}


def _body_lines(body: Any) -> Iterator[bytes]:
    """Yield complete lines without waiting to fill a fixed-size network chunk."""
    raw = getattr(body, "_raw_stream", body)
    read = getattr(raw, "read1", None) or getattr(body, "read1", None)
    if not callable(read):
        yield from body.iter_lines()
        return
    buffer = b""
    while chunk := read(64 * 1024):
        buffer += chunk
        *lines, buffer = buffer.split(b"\n")
        yield from lines
    if buffer:
        yield buffer


def stream_run(
    runtime_arn: str, region: str, session_id: str, prompt: str
) -> Iterator[dict[str, Any]]:
    """Invoke in streaming mode (``mode: "stream"``) and yield normalized event dicts."""
    resp = _client(region, launch=True).invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=runtime_session_id(session_id),
        payload=json.dumps({"prompt": prompt, "sessionId": session_id, "mode": "stream"}).encode(),
        contentType="application/json",
        accept="text/event-stream",
    )
    body = resp.get("response")
    try:
        if hasattr(body, "iter_lines") or callable(getattr(body, "read1", None)):
            yield from _parse_sse(_body_lines(body))
        else:  # non-streaming fallback: a single JSON blob
            raw = _read_body(body)
            if raw:
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    yield {"kind": "text", "text": raw}
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()


# ---- runtime resolution (flag > env > ~/.strandly/runtime.json, written by `strandly deploy`) ----

_GLOBAL_FILE = Path.home() / ".strandly" / "runtime.json"


def _recorded() -> dict[str, Any]:
    try:
        data = json.loads(_GLOBAL_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_runtime_arn(explicit: str | None = None) -> str | None:
    return explicit or os.environ.get("STRANDLY_RUNTIME_ARN") or _recorded().get("runtime_arn")


def resolve_region(explicit: str | None = None) -> str | None:
    return (
        explicit
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or _recorded().get("region")
    )


# ---- the tool bodies over this transport --------------------------------------------------------


CANCELLED_ANSWER = (
    "[cancelled] the run stopped before it finished: a newer message on this session superseded "
    "it, or it was stopped"
)


async def _aiter_events(make_iter: Callable[[], Any]):
    """Drive a blocking event iterator (a streaming boto response) off the event loop thread."""
    it = iter(make_iter())
    while True:
        ev = await asyncio.to_thread(next, it, _SENTINEL)
        if ev is _SENTINEL:
            return
        yield ev


async def run_deployed_collect(
    runtime_arn: str,
    region: str,
    session_id: str,
    prompt: str,
    on_activity: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Stream one deployed turn and return its final assistant text (``ask(wait=True)``).

    A ``turn_start`` resets the buffer: a resumed turn (the goal loop's critic sending the actor
    back) supersedes the draft it re-attempts, and the caller should see only the final answer. A
    ``cancelled`` event (a newer message on the session superseded the run, or a Stop) means the
    text so far is partial, so it is not returned as the answer.
    """
    chunks: list[str] = []
    async for ev in _aiter_events(lambda: stream_run(runtime_arn, region, session_id, prompt)):
        kind = ev.get("kind")
        if kind == "turn_start":
            chunks = []
        elif kind == "text" and ev.get("text"):
            chunks.append(ev["text"])
        elif kind == "tool_start" and on_activity is not None:
            await on_activity(f"→ {ev.get('tool') or 'tool'}")
        elif kind == "error":
            return f"[error] {ev.get('text')}"
        elif kind == "cancelled":
            return CANCELLED_ANSWER
    return "".join(chunks)


def launch_deployed_task(runtime_arn: str, region: str, session_id: str, prompt: str) -> dict[str, Any]:
    """Fire-and-forget on ``session_id`` (``ask(wait=False)``) → ``{status, taskId}``."""
    return launch_run(runtime_arn, region, session_id, prompt, trigger_source="mcp:ask")


def check_deployed_task(runtime_arn: str, region: str, session_id: str, task_id: str) -> dict[str, Any]:
    """Poll a launched run (``session(id)``'s ``latest_run`` on this transport)."""
    return poll_run(runtime_arn, region, session_id, task_id)
