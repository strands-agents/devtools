from __future__ import annotations

import pytest

from strandly_harness.core.agent import build_agent
from strandly_harness.core.config import Config
from strandly_harness.core.context import RuntimeContext


@pytest.mark.asyncio
async def test_default_tools_local_no_secrets(fake_model, tmp_path):
    # Bare config: bash + file_editor + think + spawn + skill + todo. No github (no token), no MCP
    # web search (no url). strands-agents MCP client is present (its tools list lazily at connect).
    agent = await build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model
    )
    names = set(agent.tool_names)
    assert {"bash", "file_editor", "think"} <= names
    # No dedicated read/grep/glob: file_editor.view reads, bash searches inside the sandbox.
    assert not ({"read", "grep", "glob"} & names)
    assert "todo" in names
    assert "skill" in names  # SystemPromptSkills exposes a single `skill` tool
    assert "spawn" in names
    assert "use_github" not in names  # gated: no token


@pytest.mark.asyncio
async def test_github_enabled_with_token(fake_model, tmp_path):
    agent = await build_agent(
        Config(values={"STRANDLY_GITHUB_TOKEN": "ghp_x"}),
        RuntimeContext(cwd=str(tmp_path)),
        model=fake_model,
    )
    assert "use_github" in set(agent.tool_names)


@pytest.mark.asyncio
async def test_prompt_global_plus_dynamic_capabilities(fake_model, tmp_path):
    agent = await build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model
    )
    prompt = agent.system_prompt
    assert "You are Strandly" in prompt  # the global prompt
    # Capabilities section is built from the agent's ACTUAL tools.
    assert "## Capabilities" in prompt
    assert "`skill`" in prompt and "`spawn`" in prompt  # always present
    # No KB configured → no long-term-memory capability mentioned (don't advertise absent tools).
    assert "search_memory" not in prompt
    assert "use_github" not in prompt  # gated off (no token)
    # Runtime env context is injected per-turn by EventContext, NOT in the static prompt.
    assert "# Environment" not in prompt
    # Local sandbox (no AGENTCORE_CODE_INTERPRETER_ID) = the user's real disk, which persists, so
    # the ephemeral-sandbox guidance must NOT appear. It's gated on the AgentCore sandbox.
    assert "Your sandbox is ephemeral" not in str(prompt)


@pytest.mark.asyncio
async def test_prompt_mentions_github_when_enabled(fake_model, tmp_path):
    agent = await build_agent(
        Config(values={"STRANDLY_GITHUB_TOKEN": "ghp_x"}),
        RuntimeContext(cwd=str(tmp_path)),
        model=fake_model,
    )
    assert "use_github" in agent.system_prompt  # capability appears only when the tool is present


@pytest.mark.asyncio
async def test_subagent_system_prompt_and_no_nested_spawn(fake_model, tmp_path):
    agent = await build_agent(
        Config(values={}),
        RuntimeContext(cwd=str(tmp_path)),
        model=fake_model,
        system_prompt="You are a strict reviewer.",
        spawn_depth=1,
    )
    # The subagent's own layer is present...
    assert "strict reviewer" in agent.system_prompt
    # ...and the global prompt is STILL injected on top of it (every subagent shares it).
    assert "You are Strandly" in agent.system_prompt
    assert "spawn" not in set(agent.tool_names)


@pytest.mark.asyncio
async def test_offloader_present_without_retrieval_tool(fake_model, tmp_path):
    # The ContextOffloader plugin is wired (offloading still happens), but we DON'T expose its
    # retrieve_offloaded_content tool: the agent reads offloaded artifacts via bash/file_editor on
    # its own sandbox, and the tool's document-block path crashes on octet-stream (harness-sdk#3019).
    agent = await build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model
    )
    plugins = agent._plugin_registry._plugins.values()
    assert any(getattr(p, "name", "") == "context_offloader" for p in plugins)
    assert "retrieve_offloaded_content" not in set(agent.tool_names)


@pytest.mark.asyncio
async def test_agentic_context_management(fake_model, tmp_path):
    # context_manager="agentic" injects the model-driven context tools so the model manages its own
    # history (summarize/truncate/pin), and our sandbox-routed offloader is still present.
    agent = await build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model
    )
    names = set(agent.tool_names)
    assert {"summarize_context", "truncate_context", "pin_context"} <= names
    # The offloader is wired but exposes no tool (see test_offloader_present_without_retrieval_tool).
    assert "retrieve_offloaded_content" not in names


def _has_goal_loop(agent) -> bool:
    """True if the agent carries the SDK ``GoalLoop`` plugin (the actor-critic loop)."""
    from strands.vended_plugins.goal import GoalLoop

    return any(isinstance(p, GoalLoop) for p in agent._plugin_registry._plugins.values())


@pytest.mark.asyncio
async def test_goal_loop_only_at_top_level(fake_model, tmp_path):
    # The top agent (spawn_depth=0) gets the actor-critic goal loop...
    top = await build_agent(Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model)
    assert _has_goal_loop(top)


@pytest.mark.asyncio
async def test_subagent_has_no_goal_loop(fake_model, tmp_path):
    # ...but a spawned subagent (spawn_depth=1) does NOT — convergence stays at the highest level,
    # and `spawn` only returns the subagent's final text anyway, so an in-subagent loop would be
    # invisible to the orchestrator (issue #357).
    sub = await build_agent(
        Config(values={}),
        RuntimeContext(cwd=str(tmp_path)),
        model=fake_model,
        system_prompt="You are a strict reviewer.",
        spawn_depth=1,
    )
    assert not _has_goal_loop(sub)


@pytest.mark.asyncio
async def test_session_manager_only_at_top_level(fake_model, tmp_path):
    # The top agent (spawn_depth=0) gets a session manager (file-backed here, no Memory id)...
    top = await build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path), session_id="s1"), model=fake_model
    )
    assert top._session_manager is not None
    # ...but a spawned subagent does NOT. Subagents are ephemeral and built with a context-less
    # RuntimeContext, which would collapse to the shared "session" id — attaching a session manager
    # makes concurrent subagents bind the SAME session, interleave tool blocks, and corrupt the
    # history (toolResult > toolUse → ConverseStream ValidationException). Keep subagents session-less.
    sub = await build_agent(
        Config(values={}),
        RuntimeContext(cwd=str(tmp_path)),
        model=fake_model,
        system_prompt="You are a strict reviewer.",
        spawn_depth=1,
    )
    assert sub._session_manager is None
