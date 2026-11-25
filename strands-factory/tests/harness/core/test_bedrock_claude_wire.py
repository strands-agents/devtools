"""The default model end to end over a stubbed Bedrock InvokeModel stream (no AWS, no network).

The real Strandly agent, built on the harness's ``AnthropicModel`` + ``AsyncAnthropicBedrock``, sends
its requests to an ``httpx2.MockTransport`` that answers in Bedrock's binary event-stream framing.
These pin what each model call of a turn carries (thinking, caching, injected context), that each
call replays the one before it unchanged (a signed thinking block is bound to the history before
it), and that the model-call retry and the usage counts keep working on this provider.
"""

from __future__ import annotations

import base64
import json
import threading
import zlib
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
import strands.event_loop._retry as strands_retry
from strands.models.anthropic import AnthropicModel

import strandly.core.agent as core_agent
import strandly.memory.long_term as long_term
import strandly.memory.transcript as transcript
import strandly.plugins.github_threads.plugin as github_plugin
import strandly.tools.session_title as session_title
from fake_agentcore import MEMORY_ID, FakeAgentCoreClient
from strandly.core.config import Config
from strandly.core.context import RuntimeContext
from strandly.core.events import _extract_usage
from strandly.serve.turn import run_turn

_CALLER = "CALLER_MARKER"
_GITHUB = "GH_MARKER"
_MEMORY = "MEM_MARKER"
_ASK = "Look at https://github.com/o/r/issues/1 and plan"
_CONFIG = Config(values={"AGENTCORE_MEMORY_ID": MEMORY_ID, "AWS_REGION": "us-west-2"})
_SLACK_ID = "U0OCTO"


def _frame(headers: dict[str, str], payload: bytes) -> bytes:
    encoded = b"".join(
        bytes([len(name)])
        + name.encode()
        + b"\x07"
        + len(value).to_bytes(2, "big")
        + value.encode()
        for name, value in headers.items()
    )
    prelude = (16 + len(encoded) + len(payload)).to_bytes(4, "big") + len(encoded).to_bytes(
        4, "big"
    )
    message = prelude + zlib.crc32(prelude).to_bytes(4, "big") + encoded + payload
    return message + zlib.crc32(message).to_bytes(4, "big")


def _chunk(event: dict[str, Any]) -> bytes:
    payload = json.dumps({"bytes": base64.b64encode(json.dumps(event).encode()).decode()})
    headers = {
        ":event-type": "chunk",
        ":content-type": "application/json",
        ":message-type": "event",
    }
    return _frame(headers, payload.encode())


def _throttled() -> bytes:
    headers = {
        ":exception-type": "throttlingException",
        ":content-type": "application/json",
        ":message-type": "exception",
    }
    return _frame(headers, json.dumps({"message": "Too many tokens, please wait."}).encode())


def _block_events(index: int, block: dict[str, Any]) -> list[dict[str, Any]]:
    if block["type"] == "thinking":
        start = {"type": "thinking", "thinking": "", "signature": ""}
        deltas = [
            {"type": "thinking_delta", "thinking": block["thinking"]},
            {"type": "signature_delta", "signature": block["signature"]},
        ]
    elif block["type"] == "text":
        start = {"type": "text", "text": ""}
        deltas = [{"type": "text_delta", "text": block["text"]}]
    else:
        start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
        deltas = [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
    return [
        {"type": "content_block_start", "index": index, "content_block": start},
        *({"type": "content_block_delta", "index": index, "delta": d} for d in deltas),
        {"type": "content_block_stop", "index": index},
    ]


def _message(
    block: dict[str, Any] | list[dict[str, Any]], stop: str, cache_read: int = 0, cache_write: int = 0
) -> bytes:
    usage = {
        "input_tokens": 10,
        "output_tokens": 1,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
    }
    start = {"id": "msg", "type": "message", "role": "assistant", "model": "m", "content": []}
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": {**start, "stop_reason": None, "usage": usage}}
    ]
    for index, each in enumerate(block if isinstance(block, list) else [block]):
        events += _block_events(index, each)
    events += [
        {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]
    return b"".join(_chunk(event) for event in events)


def _seed_memory(plane: FakeAgentCoreClient, namespace: str) -> None:
    """A long-term memory that the ask's words recall (the fake ranks by word overlap)."""
    now = datetime.now(timezone.utc)
    plane.records.append(
        {
            "memoryRecordId": f"mem-{len(plane.records):040d}",
            "content": {"text": f"{_MEMORY}: the team wants a plan before work starts"},
            "memoryStrategyId": "facts-0123456789",
            "namespaces": [namespace],
            "createdAt": now,
            "timestamp": now,
        }
    )


@pytest.fixture
def bedrock(monkeypatch):
    """Route the shim's Bedrock client through a scripted event stream; returns the requests.

    A test may replace ``script``, the actor's responses in order; the goal loop's critic passes.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    script = [
        _throttled(),
        _message(
            {"type": "tool_use", "id": "t1", "name": "set_session_title", "input": {"title": "Plan"}},
            "tool_use",
            1000,
            500,
        ),
        _message({"type": "text", "text": "done"}, "end_turn", 1500, 20),
    ]
    # Anything after the actor's turn is the goal loop's critic.
    verdict = _message(
        {"type": "text", "text": '{"verdict": "PASS", "reason": "ok"}'}, "end_turn", 0, 0
    )
    requests: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append({"path": request.url.path, "body": json.loads(request.content)})
        content = script.pop(0) if script else verdict
        return httpx2.Response(
            200, headers={"content-type": "application/vnd.amazon.eventstream"}, content=content
        )

    class Wired(anthropic.AsyncAnthropicBedrock):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(
                http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)), **kwargs
            )

    monkeypatch.setattr(anthropic, "AsyncAnthropicBedrock", Wired)
    monkeypatch.setattr(
        github_plugin, "build_github_context", lambda urls, **_: f"{_GITHUB} issue body"
    )
    # The real long-term memory over a fake AgentCore data plane.
    plane = FakeAgentCoreClient()
    _seed_memory(plane, "/facts/team/")
    _seed_memory(plane, f"/facts/slack-user-{_SLACK_ID}/")
    monkeypatch.setattr(long_term, "_memory_client", lambda _p, _r: SimpleNamespace(gmdp_client=plane))
    monkeypatch.setattr(transcript, "_client", lambda _p, _r: plane)
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(strands_retry, "asyncio", SimpleNamespace(sleep=sleep))
    return SimpleNamespace(requests=requests, delays=delays, script=script, plane=plane)


def _texts(message: dict[str, Any]) -> str:
    return "\n".join(
        block.get("text", "") for block in message["content"] if isinstance(block, dict)
    )


@pytest.mark.asyncio
async def test_a_turn_on_the_default_model(bedrock, tmp_path):
    agent = await core_agent.build_agent(_CONFIG, RuntimeContext(cwd=str(tmp_path)))
    assert isinstance(agent.model, AnthropicModel)

    result = await agent.invoke_async(
        _ASK,
        invocation_state={"callerContext": {"selection": _CALLER}},
    )

    throttled, first, after_tool = bedrock.requests[:3]
    for request in (throttled, first, after_tool):
        body = request["body"]
        assert (
            request["path"]
            == "/model/global.anthropic.claude-opus-5-5/invoke-with-response-stream"
        )
        assert body["anthropic_version"] == "bedrock-2023-05-31"
        assert body["max_tokens"] == 128_000
        # Thinking is on, at the configured effort.
        assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert body["output_config"] == {"effort": "medium"}
        assert body["anthropic_beta"] == ["mid-conversation-system-clear-at-2026-08-21"]
        # Prompt caching on every call: the system prompt, the tools and the latest user message.
        assert body["system"][-1].get("cache_control"), body["system"]
        assert body["tools"][-1].get("cache_control")
        latest_user = [m for m in body["messages"] if m["role"] == "user"][-1]
        assert any("cache_control" in block for block in latest_user["content"])

    # The re-issued call after the throttle is the same request.
    assert throttled["body"] == first["body"]
    assert bedrock.delays == [8]

    # Call 1: the ask, then its injected context (memory recall included) as a turn-scoped system
    # message.
    ask, system = first["body"]["messages"]
    assert _MEMORY not in _texts(ask)
    assert system["role"] == "system" and system["clear_at"] == "next_user_message"
    assert _CALLER in _texts(system) and _GITHUB in _texts(system) and _MEMORY in _texts(system)

    # Call 2 (after a tool result): the history before it is byte-stable (the ask's system message
    # stays; the API clears it now that a later user message exists), and nothing follows the result.
    ask, system, _assistant, tool_result = after_tool["body"]["messages"]
    assert system == first["body"]["messages"][1]
    assert _MEMORY not in _texts(ask)
    assert tool_result["content"][0]["type"] == "tool_result"

    # The history keeps the injected blocks apart from the ask.
    stored = agent.messages[0]["content"]
    assert [block["injected"]["source"] for block in stored if "injected" in block] == [
        "strands:environment:injector",
        "caller_context:injector",
        "github-context-injector:injector",
        "memory-recall:injector",
    ]

    usage = result.metrics.accumulated_usage
    assert usage["cacheReadInputTokens"] == 2500
    assert usage["cacheWriteInputTokens"] == 520
    assert _extract_usage(result) == {
        "input": 20,
        "output": 10,
        "total": 20 + 10 + 2500 + 520,
        "cacheRead": 2500,
        "cacheWrite": 520,
    }


# ---- every call replays the one before it --------------------------------------------------------

_PORTAL_EVENT = {
    "triggerSource": "portal:chat",
    "portalUser": "11111111-1111-4111-8111-111111111111",
    "personGithub": "octocat",
    "personId": "octo",
    "personName": "Octo Cat",
    "personRole": "maintainer",
    "personSlackUserId": _SLACK_ID,
    "invocationContext": {"selection": _CALLER},
}
_PERSON = (
    "## Who you are talking to\nThe signed-in portal user, bound to this people-directory row: "
    f"Octo Cat — maintainer (GitHub @octocat, Slack {_SLACK_ID})."
)
_SESSION_CONFIG = Config(values={**_CONFIG.values, "STRANDLY_RUN_LEDGER_TABLE": "runs"})


def _thinking(step: int) -> dict[str, Any]:
    return {"type": "thinking", "thinking": f"step {step}", "signature": f"sig-{step}"}


def _title(title: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": "title", "name": "set_session_title", "input": {"title": title}}


class _Ledger:
    """The run-ledger table ``set_session_title`` writes to; ``on_write`` runs on each write."""

    def __init__(self, on_write: Any = None) -> None:
        self.on_write = on_write

    def put_item(self, **_: Any) -> None:
        if self.on_write is not None:
            self.on_write()


def _without_cache_points(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_cache_points(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_without_cache_points(v) for v in value]
    return value


def _appended(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """What ``after`` appends to ``before``, asserting that it changes nothing else.

    Every field and every earlier message is equal; only the cache points differ, since the
    latest-message cache point moves to the newest message on every call.
    """
    first, then = _without_cache_points(before["body"]), _without_cache_points(after["body"])
    assert {k: v for k, v in then.items() if k != "messages"} == {
        k: v for k, v in first.items() if k != "messages"
    }
    sent = len(first["messages"])
    assert then["messages"][:sent] == first["messages"]
    return list(then["messages"][sent:])


def _system_text(request: dict[str, Any]) -> str:
    return "\n".join(block["text"] for block in request["body"]["system"])


async def _turn(ctx: RuntimeContext, ask: str, **kwargs: Any) -> None:
    async for _ in run_turn(_SESSION_CONFIG, ask, ctx, **kwargs):
        pass


@pytest.mark.asyncio
async def test_each_model_call_of_a_turn_replays_the_one_before_it(bedrock, monkeypatch, tmp_path):
    bedrock.script[:] = [
        _message([_thinking(1), _title("Plan issue 1")], "tool_use"),
        _message([_thinking(2), {**_title("Plan issue one"), "id": "retitle"}], "tool_use"),
        _message([_thinking(3), {"type": "text", "text": "done"}], "end_turn"),
    ]
    monkeypatch.setattr(session_title, "ledger_table_factory", lambda config: lambda: _Ledger())
    ctx = RuntimeContext(cwd=str(tmp_path), session_id="portal-wire-replay", event=_PORTAL_EVENT)

    await _turn(ctx, _ASK)

    first, second, third = bedrock.requests[:3]
    # Each later call only appends: the signed thinking block, its tool call and the result.
    assistant, result = _appended(first, second)
    assert assistant["content"][0] == {"type": "thinking", "thinking": "step 1", "signature": "sig-1"}
    assert assistant["content"][1]["name"] == "set_session_title"
    assert result["content"][0]["type"] == "tool_result"
    assistant, result = _appended(second, third)
    assert assistant["content"][0]["signature"] == "sig-2"
    assert result["content"][0]["tool_use_id"] == "retitle"
    # The ask's injected context (environment, caller context, GitHub thread, memory recall) follows it.
    ask, injected = first["body"]["messages"]
    assert _ASK in _texts(ask) and _MEMORY not in _texts(ask) and "<environment>" not in _texts(ask)
    assert injected["clear_at"] == "next_user_message"
    assert all(marker in _texts(injected) for marker in ("<environment>", _CALLER, _GITHUB, _MEMORY))
    # The transcript records the recall with the ask's context, never for extraction.
    recorded = [p for op, p in bedrock.plane.calls if op == "CreateEvent" and _MEMORY in json.dumps(p, default=str)]
    assert recorded and all(p["extractionMode"] == "SKIP" for p in recorded)
    # The person is in the system prompt of every call, and nowhere in the conversation.
    for request in (first, second, third):
        assert _system_text(request).count(_PERSON) == 1
        assert "Who you are talking to" not in json.dumps(request["body"]["messages"])


@pytest.mark.asyncio
async def test_the_first_call_after_a_cancel_replays_the_cancelled_turn(
    bedrock, monkeypatch, tmp_path
):
    cancel = threading.Event()
    bedrock.script[:] = [
        _message([_thinking(1), _title("Plan issue 1")], "tool_use"),
        _message({"type": "text", "text": "back"}, "end_turn"),
    ]
    ledger = _Ledger(on_write=cancel.set)
    monkeypatch.setattr(session_title, "ledger_table_factory", lambda config: lambda: ledger)
    ctx = RuntimeContext(cwd=str(tmp_path), session_id="portal-wire-cancel", event=_PORTAL_EVENT)

    # The cancel lands during the first tool round, so the turn makes one model call.
    await _turn(ctx, _ASK, cancel_signal=cancel)
    assert len(bedrock.requests) == 1
    ledger.on_write = None
    await _turn(ctx, "carry on", cancel_signal=threading.Event())

    cancelled, resumed = bedrock.requests[:2]
    assistant, result, marker, ask, _injected = _appended(cancelled, resumed)
    assert assistant["content"][0]["signature"] == "sig-1"
    assert result["content"][0]["tool_use_id"] == "title"
    assert marker["role"] == "assistant"
    assert _texts(ask) == "carry on"
    assert _system_text(resumed).count(_PERSON) == 1
