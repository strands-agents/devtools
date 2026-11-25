"""A portal chat's model choice through the runtime: built by the harness up front, recorded per run."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import strands_harness.agent as harness_agent
from strands.sandbox.not_a_sandbox_local_environment import NotASandboxLocalEnvironment
from strands_harness.defaults import DEFAULT_MODEL
from strands_harness.tools.subagent import AgentSpec

from conftest import FakeModel, text_response_events
from strandly.core.agent import build_agent
from strandly.core.config import Config
from strandly.core.context import RuntimeContext
from strandly.core.events import HarnessEvent
from strandly.serve.cache import agent_cache_key
from strandly.tools.subagent import make_builder

HAIKU = "bedrock/global.anthropic.claude-haiku-5-5"
FABLE = "bedrock/global.anthropic.claude-fable-5-2"


async def _drain() -> None:
    for _ in range(100):
        await asyncio.sleep(0)


def _app(monkeypatch, values: dict[str, str] | None = None):
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    starts: list[dict[str, Any]] = []
    finishes: list[dict[str, Any]] = []
    contexts: list[RuntimeContext] = []

    class FakeLedger:
        @staticmethod
        def from_config(config):
            return FakeLedger()

        def start(self, *a, **kw):
            starts.append(kw)

        def finish(self, *a, **kw):
            finishes.append(kw)

        def fail(self, *a, **kw):
            pass

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        contexts.append(ctx)

        async def gen():
            yield HarnessEvent(kind="text", text="ok")
            yield HarnessEvent(kind="done", data={"usage": {"inputTokens": 1, "outputTokens": 1}})

        return gen()

    monkeypatch.setattr(ac, "RunLedger", FakeLedger)
    monkeypatch.setattr(ac, "run_turn", fake_run_turn)
    return ac.build_app(Config(values=values or {})), starts, finishes, contexts


@pytest.mark.asyncio
async def test_a_chosen_model_runs_and_is_recorded(monkeypatch):
    app, starts, finishes, contexts = _app(monkeypatch)
    res = await app.handlers["main"](
        {"prompt": "hi", "sessionId": "portal-s1", "model": HAIKU, "effort": "low"}
    )
    assert res["status"] == "accepted"
    await _drain()
    [ctx] = contexts
    assert (ctx.model, ctx.effort) == (HAIKU, "low")
    assert (starts[0]["model"], starts[0]["effort"]) == (HAIKU, "low")
    assert (finishes[0]["model"], finishes[0]["effort"]) == (HAIKU, "low")


@pytest.mark.asyncio
async def test_no_choice_runs_and_records_the_deployment_default(monkeypatch):
    app, starts, finishes, contexts = _app(monkeypatch)
    await app.handlers["main"]({"prompt": "hi", "sessionId": "portal-s1"})
    await _drain()
    [ctx] = contexts
    assert (ctx.model, ctx.effort) == (None, None)
    assert (starts[0]["model"], starts[0]["effort"]) == (DEFAULT_MODEL, "medium")
    assert (finishes[0]["model"], finishes[0]["effort"]) == (DEFAULT_MODEL, "medium")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "recorded"),
    [
        ({"model": "openai/gpt-5", "effort": "high"}, ("openai/gpt-5", "high")),
        ({"model": "bedrock/amazon.nova-pro-v1:0"}, ("bedrock/amazon.nova-pro-v1:0", "auto")),
        ({"effort": "high"}, (DEFAULT_MODEL, "high")),
        ({"model": "", "effort": ""}, (DEFAULT_MODEL, "medium")),
    ],
)
async def test_any_choice_the_harness_builds_runs_without_a_list_of_ours(monkeypatch, choice, recorded):
    app, starts, _, _ = _app(monkeypatch)
    res = await app.handlers["main"]({"prompt": "hi", "sessionId": "portal-s1", **choice})
    assert res["status"] == "accepted"
    await _drain()
    assert (starts[0]["model"], starts[0]["effort"]) == recorded


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "reason"),
    [
        ({"model": "nope/some-model"}, "Unknown model provider 'nope'"),
        ({"model": "bedrock/amazon.nova-pro-v1:0", "effort": "high"}, "Effort 'high' is not supported"),
        ({"model": FABLE, "effort": "extreme"}, "Unknown effort 'extreme'"),
        ({"effort": "extreme"}, "Unknown effort 'extreme'"),
        ({"model": "x" * 257}, "printable characters"),
        ({"effort": "e" * 33}, "printable characters"),
        ({"model": "bedrock/a\nb"}, "printable characters"),
        ({"model": 7}, "printable characters"),
        ({"effort": ["high"]}, "printable characters"),
    ],
)
async def test_a_choice_the_harness_refuses_is_answered_before_any_work(monkeypatch, choice, reason):
    app, starts, _, contexts = _app(monkeypatch)
    res = await app.handlers["main"]({"prompt": "hi", "sessionId": "portal-s1", **choice})
    assert res["status"] == "error" and reason in res["error"]
    await _drain()
    assert contexts == [] and starts == []
    status = await app.handlers["main"]({"action": "cancel", "sessionId": "portal-s1"})
    assert status["status"] == "idle"


@pytest.mark.asyncio
async def test_a_session_switches_model_between_turns(monkeypatch):
    app, starts, _, contexts = _app(monkeypatch)
    await app.handlers["main"]({"prompt": "hi", "sessionId": "portal-s1", "model": HAIKU, "effort": "off"})
    await _drain()
    await app.handlers["main"]({"prompt": "again", "sessionId": "portal-s1", "model": FABLE})
    await _drain()
    assert [(c.model, c.effort) for c in contexts] == [(HAIKU, "off"), (FABLE, "auto")]
    assert [(s["model"], s["effort"]) for s in starts] == [(HAIKU, "off"), (FABLE, "auto")]


def test_a_chosen_model_gets_its_own_cached_agent():
    base = RuntimeContext(session_id="portal-s1")
    chosen = RuntimeContext(session_id="portal-s1", model=HAIKU, effort="off")
    other = RuntimeContext(session_id="portal-s1", model=HAIKU, effort="low")
    keys = {agent_cache_key(ctx) for ctx in (base, chosen, other)}
    assert len(keys) == 3
    assert agent_cache_key(base) == "portal-s1"


@pytest.mark.asyncio
async def test_the_agent_and_its_default_tier_subagent_run_the_chosen_model(monkeypatch, tmp_path):
    seen: list[tuple[str, str]] = []

    def resolve(model: Any, default: str, effort: str = "auto", **kwargs: Any) -> FakeModel:
        seen.append((model, effort))
        return FakeModel(events=text_response_events("ok"))

    monkeypatch.setattr(harness_agent, "resolve_model", resolve)
    ctx = RuntimeContext(cwd=str(tmp_path), model=FABLE, effort="xhigh")
    await build_agent(Config(values={}), ctx)
    builder = make_builder(Config(values={}), ctx, NotASandboxLocalEnvironment())
    builder(AgentSpec(task="x"))
    builder(AgentSpec(task="x", model="fast"))
    assert seen == [(FABLE, "xhigh"), (FABLE, "xhigh"), (HAIKU, "off")]
