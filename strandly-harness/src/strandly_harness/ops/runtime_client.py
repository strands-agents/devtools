"""Client for a *deployed* AgentCore runtime — stream a chat, or launch + poll a long run.

Wraps ``bedrock-agentcore:InvokeAgentRuntime`` for the two deployed modes:

- **stream** (:func:`stream_run`) — synchronous chat: send ``mode: "stream"`` and read the
  ``text/event-stream`` response back as normalized event dicts.
- **fire-and-forget** (:func:`launch_run` + :func:`poll_run`) — launch returns a ``taskId``
  immediately; the later poll reads the result from AgentCore Memory. A run and its poll share the
  same **runtime** session id so AgentCore routes both to the same instance (session affinity); the
  **Memory** session id (carried as ``sessionId`` in the payload) is what the result is read under.

The runtime session id must be slash-free and >= 33 chars or ``InvokeAgentRuntime`` throws an opaque
validation error, so every call pads it via :func:`strandly_harness.core.session_ids.runtime_session_id`. The
unpadded value travels in the payload as ``sessionId`` (the Memory id the deployed run writes under,
and the poll reads back) — the two ids derive from the same ``--session-id`` but are sanitized
differently.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def _client(region: str) -> Any:
    import boto3

    return boto3.Session(region_name=region).client("bedrock-agentcore")


def _invoke(runtime_arn: str, region: str, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    from strandly_harness.core.session_ids import runtime_session_id

    resp = _client(region).invoke_agent_runtime(
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
) -> dict[str, Any]:
    """Start a fire-and-forget run; returns ``{status: accepted, taskId}`` (or an error dict).

    A GitHub context is **optional** — when given it's merged in so the deployed agent can also
    report out of band via ``use_github``; it is no longer required to invoke.
    """
    payload: dict[str, Any] = {"prompt": prompt, "sessionId": session_id}
    if github_context:
        payload.update(github_context)
    return _invoke(runtime_arn, region, session_id, payload)


def poll_run(runtime_arn: str, region: str, session_id: str, task_id: str) -> dict[str, Any]:
    """Poll a run by task id, using the run's session id for instance affinity + Memory read.

    Returns ``{status: running|completed|failed|unknown, result?|error?}``. The deployed side merges
    an in-instance status sentinel with the durable AgentCore Memory session, so ``completed`` is
    reported even if the original instance was recycled (read back from Memory by ``sessionId``).
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


def stream_run(
    runtime_arn: str, region: str, session_id: str, prompt: str
) -> Iterator[dict[str, Any]]:
    """Invoke the deployed runtime in streaming mode and yield normalized event dicts (SSE).

    Sends ``mode: "stream"``; the deployed entrypoint returns a ``text/event-stream`` the SDK frames
    as ``data: {json}\\n\\n`` per :func:`strandly_harness.serve.agentcore_app`'s ``_event_dict``.
    """
    from strandly_harness.core.session_ids import runtime_session_id

    resp = _client(region).invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=runtime_session_id(session_id),
        payload=json.dumps({"prompt": prompt, "sessionId": session_id, "mode": "stream"}).encode(),
        contentType="application/json",
        accept="text/event-stream",
    )
    body = resp.get("response")
    if hasattr(body, "iter_lines"):
        yield from _parse_sse(body.iter_lines())
    else:  # non-streaming fallback: a single JSON blob
        raw = body.read() if hasattr(body, "read") else body
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode()
        if raw:
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                yield {"kind": "text", "text": raw}


# --- Deployed-runtime resolution (flag > env > user-global record > toolkit yaml) ---------------

_GLOBAL_DIR = Path.home() / ".strandly"
_GLOBAL_FILE = _GLOBAL_DIR / "runtime.json"


def _arn_from_local_yaml(path: Path = Path(".bedrock_agentcore.yaml")) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text().splitlines():
        s = line.strip()
        if s.startswith("agent_arn:"):
            arn = s.split(":", 1)[1].strip()
            if arn and arn != "null":
                return arn
    return None


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
    """Resolve the deployed runtime ARN from (in order) flag, env, global record, local yaml."""
    return (
        explicit
        or os.environ.get("STRANDLY_RUNTIME_ARN")
        or _recorded().get("runtime_arn")
        or _arn_from_local_yaml()
    )


def resolve_region(explicit: str | None = None) -> str | None:
    return (
        explicit
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or _recorded().get("region")
    )
