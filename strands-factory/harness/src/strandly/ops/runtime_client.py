"""Client for a deployed AgentCore runtime: stream a chat, or launch and poll a long run.

``stream_run`` reads the ``text/event-stream`` response as event dicts; ``launch_run`` returns a task
id and ``poll_run`` reads the result back from the session store. A run and its poll share one
runtime session id (padded by :func:`strandly.core.session_ids.runtime_session_id`, as
``InvokeAgentRuntime`` requires) for instance affinity, while the unpadded store id travels in the
payload as ``sessionId``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# --- Client config: no duplicate launches on a slow cold start ---------------------------------
#
# A new session's first InvokeAgentRuntime is not answered until the session's compute has started:
# usually seconds on the microVM, but 78 s has been observed (and ~1 min is normal on Runtime
# instances). botocore's defaults (60 s read timeout + automatic retries) time out on exactly that
# call and RE-SEND it — and AgentCore delivers both, so the agent runs the same message twice. Two
# rules fix that:
#
# 1. **Read timeout above the worst cold start.** 300 s is ~4x the worst start we have seen, while
#    staying well inside the 900 s Lambda ceiling so the waiting Lambdas (mention poller, Slack
#    ingress, scheduled invoker — see infra/stacks/{ingress,slack,scheduler}_stack.py) can outlive
#    one worst-case call.
#    It is a per-socket-read timeout, so it also bounds the gap between SSE chunks in stream mode.
# 2. **No automatic retries on a launch.** A launch (any payload without an ``action`` key — a new
#    message) is NOT idempotent: AgentCore has no per-request idempotency token
#    (``runtimeSessionId`` is the only member botocore marks ``idempotencyToken`` and it names the
#    session, not the request), so a retried launch is a second run. ``total_max_attempts=1`` means
#    exactly one attempt — NOT ``max_attempts=1``, which botocore reads as one *retry* (2 attempts;
#    ``botocore.args._compute_retry_max_attempts`` adds 1). A failed launch surfaces to the caller,
#    whose own redelivery logic (dedup rollback → next poll/DM) decides.
#
# Control calls (``action: poll`` / ``cancel``) are idempotent reads/stops and keep retrying
# (standard mode, 3 attempts) — with the same long read timeout, since a poll that lands on a
# recycled instance pays the same cold start.

#: Socket read timeout (seconds) for every InvokeAgentRuntime call (see the rationale above).
INVOKE_READ_TIMEOUT_S = 300
#: TCP connect timeout (seconds) — the endpoint is a regional AWS API; a connect never takes long.
INVOKE_CONNECT_TIMEOUT_S = 10
#: Total attempts for a launch (non-idempotent): exactly one.
LAUNCH_TOTAL_MAX_ATTEMPTS = 1
#: Total attempts for an idempotent control call (poll / cancel).
CONTROL_TOTAL_MAX_ATTEMPTS = 3


def client_config(*, launch: bool) -> Any:
    """The botocore ``Config`` for an InvokeAgentRuntime call: ``launch`` → one attempt, else retried."""
    from botocore.config import Config

    return Config(
        read_timeout=INVOKE_READ_TIMEOUT_S,
        connect_timeout=INVOKE_CONNECT_TIMEOUT_S,
        retries={
            "total_max_attempts": LAUNCH_TOTAL_MAX_ATTEMPTS if launch else CONTROL_TOTAL_MAX_ATTEMPTS,
            "mode": "standard",
        },
    )


def launch_maybe_accepted(exc: BaseException) -> bool:
    """True when a launch raised AFTER its request reached the service, so the run may have started.

    A launch is a single attempt with a long read timeout (above). A ``ReadTimeoutError`` — or the
    connection dropping / the body failing mid-response — means the request was SENT and the runtime
    may well be running it (a cold start that outlived 300 s, a response lost on the wire). A trigger
    that rolled its dispatch intent back on that would re-dispatch on the next tick/redelivery and
    run the same message twice — the very duplicate the single attempt exists to prevent. Such a
    trigger must keep its intent ("maybe dispatched") and only report it.

    Everything else is treated as NOT dispatched (roll back, retry later): connect errors / connect
    timeouts / DNS / TLS (the request never left), and a service error response (``ClientError``:
    throttling, validation, access denied — the service answered and refused), plus non-botocore
    errors raised before the call (an unresolved ARN).
    """
    try:
        from botocore.exceptions import (
            ConnectionClosedError,
            IncompleteReadError,
            ReadTimeoutError,
            ResponseStreamingError,
        )
    except ImportError:  # pragma: no cover — botocore is a hard dependency of every caller
        return False
    return isinstance(
        exc, (ReadTimeoutError, ConnectionClosedError, ResponseStreamingError, IncompleteReadError)
    )


def is_launch(payload: dict[str, Any]) -> bool:
    """True when ``payload`` starts a run (no ``action`` key) — i.e. must never be auto-retried."""
    return not payload.get("action")


def _client(region: str, *, launch: bool) -> Any:
    import boto3

    return boto3.Session(region_name=region).client(
        "bedrock-agentcore", config=client_config(launch=launch)
    )


def _invoke(runtime_arn: str, region: str, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    from strandly.core.session_ids import runtime_session_id

    resp = _client(region, launch=is_launch(payload)).invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=runtime_session_id(session_id),
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


def launch_run(
    runtime_arn: str,
    region: str,
    session_id: str,
    prompt: str,
    github_context: dict[str, Any] | None = None,
    *,
    mode: str | None = None,
    trigger_source: str | None = None,
) -> dict[str, Any]:
    """Start a fire-and-forget run; returns ``{status: accepted, taskId}`` (or an error dict).

    A GitHub context is optional; when given it is merged in so the deployed agent can also report
    out of band via ``use_github``.

    ``mode`` (``draft``/``curate``/``post``) and ``trigger_source`` (e.g. ``event:pull_request.opened``,
    ``portal:curate``) are the portal role + provenance of the turn. They travel as
    the payload's ``mode`` / ``triggerSource`` keys — the deployed runtime records them on the ledger
    (``_portal_mode`` reads ``mode`` except the ``"stream"`` transport value; ``_trigger_source``
    reads ``triggerSource``). Set after any ``github_context`` merge so they always win.
    """
    payload: dict[str, Any] = {"prompt": prompt, "sessionId": session_id}
    if github_context:
        payload.update(github_context)
    if mode:
        payload["mode"] = mode
    if trigger_source:
        payload["triggerSource"] = trigger_source
    return _invoke(runtime_arn, region, session_id, payload)


def poll_run(runtime_arn: str, region: str, session_id: str, task_id: str) -> dict[str, Any]:
    """Poll a run by task id, using the run's session id for instance affinity + the durable read.

    Returns ``{status: running|completed|cancelled|failed|unknown, result?|error?}``. The deployed side merges
    an in-instance status sentinel with the durable stored session, so ``completed`` is reported
    even if the original instance was recycled (read back from the session store by ``sessionId``).
    """
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


#: Upper bound of one incremental read of a streamed response body.
_STREAM_READ_BYTES = 64 * 1024


def _body_lines(body: Any) -> Iterator[bytes]:
    """The body's lines as soon as each one arrives.

    ``StreamingBody.iter_lines`` reads fixed 1024-byte chunks and so holds a small event back until
    later ones fill the chunk. ``read1`` (urllib3) returns whatever has arrived, at most one read.
    """
    raw = getattr(body, "_raw_stream", body)
    read = getattr(raw, "read1", None) or getattr(body, "read1", None)
    if not callable(read):
        yield from body.iter_lines()
        return
    buffer = b""
    while True:
        chunk = read(_STREAM_READ_BYTES)
        if not chunk:
            break
        buffer += chunk
        *lines, buffer = buffer.split(b"\n")
        yield from lines
    if buffer:
        yield buffer


def stream_run(
    runtime_arn: str, region: str, session_id: str, prompt: str
) -> Iterator[dict[str, Any]]:
    """Invoke the deployed runtime in streaming mode and yield normalized event dicts (SSE).

    Sends ``mode: "stream"``; the deployed entrypoint returns a ``text/event-stream`` the SDK frames
    as ``data: {json}\\n\\n`` per :func:`strandly.serve.agentcore_app`'s ``_event_dict``.
    """
    from strandly.core.session_ids import runtime_session_id

    resp = _client(region, launch=True).invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=runtime_session_id(session_id),
        payload=json.dumps({"prompt": prompt, "sessionId": session_id, "mode": "stream"}).encode(),
        contentType="application/json",
        accept="text/event-stream",
    )
    body = resp.get("response")
    if hasattr(body, "iter_lines"):
        yield from _parse_sse(_body_lines(body))
    else:  # non-streaming fallback: a single JSON blob
        raw = body.read() if hasattr(body, "read") else body
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode()
        if raw:
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                yield {"kind": "text", "text": raw}


# --- Deployed-runtime resolution (flag > env > user-global record) ------------------------------

_GLOBAL_DIR = Path.home() / ".strandly"
_GLOBAL_FILE = _GLOBAL_DIR / "runtime.json"


def record_runtime(arn: str, region: str) -> None:
    """Persist the deployed runtime ARN + region user-globally so invoke/poll work from anywhere."""
    _GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
    _GLOBAL_FILE.write_text(json.dumps({"runtime_arn": arn, "region": region}, indent=2))


def _recorded() -> dict[str, str]:
    if _GLOBAL_FILE.is_file():
        try:
            data = json.loads(_GLOBAL_FILE.read_text())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {}


def resolve_runtime_arn(explicit: str | None = None) -> str | None:
    """Resolve the deployed runtime ARN from (in order) flag, env, the ``strandly deploy`` record."""
    return explicit or os.environ.get("STRANDLY_RUNTIME_ARN") or _recorded().get("runtime_arn")


def resolve_region(explicit: str | None = None) -> str | None:
    return (
        explicit
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or _recorded().get("region")
    )
