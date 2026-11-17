from __future__ import annotations

import pytest

from strandly_harness.core.config import Config
from strandly_harness.core.context import RuntimeContext
from strandly_harness.tools.spawn import _resolve_system_prompt, make_spawn


def test_resolve_system_prompt_literal(tmp_path):
    ctx = RuntimeContext(cwd=str(tmp_path))
    assert _resolve_system_prompt(ctx, "be terse") == "be terse"


def test_resolve_system_prompt_from_file(tmp_path):
    prompt_file = tmp_path / "skills" / "code-review" / "assets" / "roles" / "reviewer.md"
    prompt_file.parent.mkdir(parents=True)
    prompt_file.write_text("# Reviewer\nYou are a strict code reviewer.")
    ctx = RuntimeContext(cwd=str(tmp_path))
    out = _resolve_system_prompt(ctx, "skills/code-review/assets/roles/reviewer.md")
    assert "strict code reviewer" in out


@pytest.mark.asyncio
async def test_spawn_runs_subagent(tmp_path, monkeypatch, text_model):
    import strandly_harness.core.agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "build_model", lambda s, tier="default": text_model("subagent says hi")
    )
    spawn = make_spawn(Config(values={}), RuntimeContext(cwd=str(tmp_path)), depth=0)
    out = await spawn(prompt="do the thing")
    assert "subagent says hi" in out


@pytest.mark.asyncio
async def test_spawn_model_tier_reaches_build_model(tmp_path, monkeypatch, text_model):
    """`spawn(model="fast")` builds the subagent's model with that tier."""
    import strandly_harness.core.agent as agent_mod

    seen: list[str] = []

    def fake_build_model(config, tier="default"):
        seen.append(tier)
        return text_model("tiered")

    monkeypatch.setattr(agent_mod, "build_model", fake_build_model)
    spawn = make_spawn(Config(values={}), RuntimeContext(cwd=str(tmp_path)), depth=0)

    out = await spawn(prompt="quick task", model="fast")
    assert "tiered" in out
    assert seen == ["fast"]

    # Omitted -> default tier.
    await spawn(prompt="normal task")
    assert seen == ["fast", "default"]


@pytest.mark.asyncio
async def test_spawn_rejects_unknown_model(tmp_path):
    """An arbitrary model id is rejected with a friendly error, before any agent is built."""
    spawn = make_spawn(Config(values={}), RuntimeContext(cwd=str(tmp_path)), depth=0)
    out = await spawn(prompt="x", model="gpt-oss-999")
    assert "Error" in out and "gpt-oss-999" in out
    # The error teaches the valid tiers.
    assert "advanced" in out and "fast" in out and "default" in out


@pytest.mark.asyncio
async def test_spawn_depth_limit(tmp_path):
    spawn = make_spawn(Config(values={}), RuntimeContext(cwd=str(tmp_path)), depth=1)
    out = await spawn(prompt="x")
    assert "depth" in out.lower()


@pytest.mark.asyncio
async def test_spawn_shares_parent_sandbox(tmp_path, monkeypatch, text_model):
    """The subagent must REUSE the parent's sandbox, not build a fresh one.

    Regression guard: building a fresh sandbox per subagent starts a new AgentCore Code Interpreter
    session that evicts the parent's (its next tool call then fails "session ... is not active").
    The parent's sandbox object must reach the subagent's build_agent, and build_sandbox must NOT be
    called during spawn.
    """
    import strandly_harness.core.agent as agent_mod

    # build_sandbox must NOT be called during spawn (a new sandbox = a new CI session = eviction).
    built: list[object] = []
    monkeypatch.setattr(agent_mod, "build_sandbox", lambda config: built.append(object()) or built[-1])

    # Stub build_agent to just capture the sandbox it was handed and return a fake agent.
    seen: dict[str, object] = {}

    class _FakeAgent:
        async def invoke_async(self, prompt):
            return "done"

    async def fake_build_agent(config, ctx, **kwargs):
        seen["sandbox"] = kwargs.get("sandbox")
        return _FakeAgent()

    monkeypatch.setattr(agent_mod, "build_agent", fake_build_agent)

    parent_sandbox = object()
    spawn = make_spawn(Config(values={}), RuntimeContext(cwd=str(tmp_path)), parent_sandbox, depth=0)
    out = await spawn(prompt="do it")

    assert out == "done"
    # The subagent's build_agent received the PARENT's sandbox...
    assert seen["sandbox"] is parent_sandbox
    # ...and no new sandbox was built during the spawn (would be a new CI session → eviction).
    assert built == []
