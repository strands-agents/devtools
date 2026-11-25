"""Wiring tests for the Strands harness base (`build_agent` as a `create_harness` wrapper).

Pins the seams the migration relies on: the harness contract is the base prompt layer, no
context offloader is wired (the SDK context manager handles large results), the harness's file tools
resolve the agent's sandbox at call time, the pre-built model instance (or a plain ModelRouter) is
used verbatim, and none of the harness's ON-by-default sugar (session, memory, subagent, skills)
leaks into an agent Strandly builds.
"""

from __future__ import annotations

import pytest
from strands_harness import HARNESS_CONTRACT

from strandly.core.agent import build_agent
from strandly.core.config import Config
from strandly.core.context import RuntimeContext


@pytest.mark.asyncio
async def test_prompt_base_layer_is_the_harness_contract(fake_model, tmp_path):
    agent = await build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model
    )
    # The recomposed prompt is content blocks (text + cache point); the SDK's getter renders the
    # text, and the contract must open it.
    text = str(agent.system_prompt)
    assert text.startswith(HARNESS_CONTRACT)
    assert "You are Strandly" in text  # the global prompt layers on top


@pytest.mark.asyncio
async def test_no_context_offloader(fake_model, tmp_path):
    # Large tool results go only through the SDK context manager (`retrieve_context`).
    from strands.vended_plugins.context_offloader import ContextOffloader

    agent = await build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model
    )
    plugins = agent._plugin_registry._plugins.values()
    assert not any(isinstance(p, ContextOffloader) for p in plugins)
    assert "retrieve_offloaded_content" not in set(agent.tool_names)
    assert "retrieve_context" in set(agent.tool_names)


@pytest.mark.asyncio
async def test_model_instance_used_verbatim(fake_model, tmp_path):
    agent = await build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model
    )
    assert agent.model is fake_model


@pytest.mark.asyncio
async def test_default_model_runs_on_anthropics_bedrock_client(tmp_path, caplog):
    import logging

    import anthropic
    from strands.models.anthropic import AnthropicModel
    from strands_harness.defaults import DEFAULT_MODEL

    with caplog.at_level(logging.WARNING, logger="strands_harness"):
        agent = await build_agent(Config(values={}), RuntimeContext(cwd=str(tmp_path)))
    assert isinstance(agent.model, AnthropicModel)
    assert isinstance(agent.model.client, anthropic.AsyncAnthropicBedrock)
    config = agent.model.get_config()
    assert f"bedrock/{config['model_id']}" == DEFAULT_MODEL
    assert config["cache_config"].strategy == "auto"
    assert config["params"]["output_config"] == {"effort": "medium"}
    # The harness builds the model from the string, so it applies effort and caching itself.
    assert not [r for r in caplog.records if "not applied to" in r.getMessage()]


@pytest.mark.asyncio
async def test_non_claude_bedrock_model_string_stays_on_the_harness(tmp_path):
    from strands.models import BedrockModel

    agent = await build_agent(
        Config(values={"STRANDLY_MODEL": "bedrock/us.amazon.nova-2-lite-v1:0"}),
        RuntimeContext(cwd=str(tmp_path)),
    )
    assert isinstance(agent.model, BedrockModel)
    assert agent.model.get_config()["model_id"] == "us.amazon.nova-2-lite-v1:0"


@pytest.mark.asyncio
async def test_harness_file_tools_route_through_agent_sandbox(fake_model, tmp_path):
    # The harness's `read` resolves `tool_context.agent.sandbox` at call time. The bare config
    # builds a local sandbox, so reading a real file through the agent's registered tool must work
    # — file access stays behind the sandbox boundary.
    target = tmp_path / "hello.txt"
    target.write_text("alpha\nbeta\n")

    agent = await build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model
    )
    result = agent.tool.read(path=str(target))  # direct tool call (sync caller surface)
    text = str(result)
    assert "alpha" in text and "beta" in text


@pytest.mark.asyncio
async def test_builtin_tools_are_the_harness_defaults_minus_web_search_and_subagent(fake_model, tmp_path):
    # Opt-out, not an allowlist: whatever the harness adds to its defaults flows in.
    from strands_harness import defaults

    agent = await build_agent(Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model)
    expected = set(defaults.DEFAULT_BUILTIN_TOOLS) - {"web_search", "subagent"}
    assert expected <= set(agent.tool_names)
    assert "web_search" not in agent.tool_names
    assert {"shell", "read", "write", "edit", "web_fetch", "programmatic_tool_caller"} <= expected


@pytest.mark.asyncio
async def test_harness_sugar_does_not_leak_into_a_strandly_agent(fake_model, tmp_path, monkeypatch):
    # Every one of these is ON by default in create_harness and would silently change a deployed
    # run: a file session under ./.agent/sessions (fresh random id, so nothing would resume), the
    # harness's file-backed long-term memory (search_memory + extraction to ./.agent/memory on the
    # host), the harness's `subagent`, its default skills dir, and native web search.
    monkeypatch.chdir(tmp_path)
    from strands.session import SnapshotSessionManager
    from strands.vended_plugins.skills import AgentSkills

    from strandly.skills.loader import builtin_skills_dir

    agent = await build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model
    )
    names = set(agent.tool_names)
    assert "subagent" in names and "generalist" not in agent.tool_registry.registry["subagent"].tool_spec["description"]
    assert "web_search" not in names
    assert "search_memory" not in names
    assert agent.memory_manager is None
    # Strandly's own file session (memory/session.py), never the harness's SnapshotSessionManager
    # under ./.agent/sessions.
    assert not isinstance(agent._session_manager, SnapshotSessionManager)
    assert not (tmp_path / ".agent").exists()
    # Exactly one AgentSkills, over the built-in skills only (never ./.agent/skills on the host).
    skills_plugins = [
        p for p in agent._plugin_registry._plugins.values() if isinstance(p, AgentSkills)
    ]
    assert len(skills_plugins) == 1
    loaded = {s.name for s in skills_plugins[0].get_available_skills()}
    builtin = {p.name for p in builtin_skills_dir().iterdir() if (p / "SKILL.md").is_file()}
    assert loaded == builtin
    # The SDK's Background Tasks ARE on at depth 0 — scoped to Strandly's two long tools, not the
    # harness default of every tool (`tests/harness/core/test_background_tasks.py` pins the shape).
    assert agent._background_tasks is not None


@pytest.mark.asyncio
async def test_subagent_build_gets_no_session_from_the_harness(fake_model, tmp_path, monkeypatch):
    # A subagent (depth 1) passes session_manager=None; the harness's `session=True`
    # default would mint a SnapshotSessionManager for it. build_agent passes session=False.
    monkeypatch.chdir(tmp_path)
    agent = await build_agent(
        Config(values={}),
        RuntimeContext(cwd=str(tmp_path)),
        model=fake_model,
        spawn_depth=1,
        sandbox=(
            await build_agent(
                Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model
            )
        ).sandbox,
    )
    assert agent._session_manager is None
    assert not (tmp_path / ".agent").exists()


@pytest.mark.asyncio
async def test_context_management_is_the_sdk_auto_preset(fake_model, tmp_path):
    # `context_manager="auto"` is the SDK preset: it owns context reduction (the conversation
    # manager is a no-op), eagerly cuts tool results over 1,500 tokens to a preview and exposes
    # `retrieve_context` for the stashed original.
    from strands.agent.conversation_manager import NullConversationManager
    from strands.experimental.context_manager import ContextManager

    agent = await build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model
    )
    assert isinstance(agent._context_manager, ContextManager)
    assert isinstance(agent.conversation_manager, NullConversationManager)
    assert "retrieve_context" in set(agent.tool_names)
    # Sessionless → nothing to persist against, so the SDK's in-memory stash.
    assert agent.context_manager.stash_is_durable is False

    # With a session, the stash lives in the session store (agent-level storage), so a
    # `retrieve_context` reference survives a restart instead of dying with the agent instance.
    with_session = await build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path), session_id="s-1"), model=fake_model
    )
    assert with_session.storage is not None
    assert with_session.context_manager.stash_is_durable is True
