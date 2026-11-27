from __future__ import annotations

import pytest

from strandly.core.agent import build_agent
from strandly.core.config import Config
from strandly.core.context import RuntimeContext


@pytest.mark.asyncio
async def test_default_tools_local_no_secrets(fake_model, tmp_path):
    # Bare config: the harness's builtins (shell + read/write/edit + web_fetch + programmatic_tool_caller)
    # + subagent + skills. No github (no token), no MCP web search (no url). strands-agents MCP client is
    # present (its tools list lazily at connect).
    agent = await build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model
    )
    names = set(agent.tool_names)
    assert {"shell", "read", "write", "edit", "web_fetch", "programmatic_tool_caller"} <= names
    # `think` is gone (strands-agents-tools 0.9 removed it).
    assert "think" not in names
    # The old harness-local tools are gone: file_editor is now read/write/edit.
    assert not ({"file_editor", "todo", "grep", "glob"} & names)
    # `subagent` is Strandly's build of the harness tool (no `generalist` preset).
    assert "generalist" not in names
    assert "todo_write" not in names  # no todos plugin
    assert "skills" in names  # the harness's AgentSkills tool
    assert "subagent" in names and "spawn" not in names
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
    assert "`skills`" in prompt and "`subagent`" in prompt  # always present
    # No KB configured → no long-term-memory capability mentioned (don't advertise absent tools).
    assert "search_memory" not in prompt
    assert "use_github" not in prompt  # gated off (no token)
    # Runtime env context is injected per user turn, NOT in the static prompt.
    assert "<environment>" not in prompt
    assert "Your sandbox is ephemeral" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("values", "depth", "scope"),
    [
        ({}, 0, None),
        ({"STRANDLY_GITHUB_TOKEN": "ghp_x", "AGENTCORE_MEMORY_ID": "mem-1"}, 0, None),
        ({"AGENTCORE_MEMORY_ID": "mem-1"}, 1, None),
        ({}, 1, frozenset({"read", "shell"})),
    ],
)
async def test_prompt_capabilities_are_the_built_agents_tools(fake_model, tmp_path, values, depth, scope):
    # The prompt is composed before construction; it must describe exactly the tools the
    # harness and plugins end up registering.
    from strandly.core.constants import BACKGROUND_TOOL_NAMES
    from strandly.core.prompt.compose import _capabilities_section

    agent = await build_agent(
        Config(values=values),
        RuntimeContext(cwd=str(tmp_path)),
        model=fake_model,
        spawn_depth=depth,
        allowed_tools=scope,
    )
    names = set(agent.tool_names)
    expected = _capabilities_section(
        names, BACKGROUND_TOOL_NAMES & names if depth == 0 else (), subagent=depth > 0
    )
    assert expected and expected in agent.system_prompt


@pytest.mark.asyncio
async def test_prompt_mentions_github_when_enabled(fake_model, tmp_path):
    agent = await build_agent(
        Config(values={"STRANDLY_GITHUB_TOKEN": "ghp_x"}),
        RuntimeContext(cwd=str(tmp_path)),
        model=fake_model,
    )
    assert "use_github" in agent.system_prompt  # capability appears only when the tool is present


@pytest.mark.asyncio
async def test_subagent_system_prompt_and_no_nested_subagent(fake_model, tmp_path):
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
    assert "subagent" not in set(agent.tool_names)


@pytest.mark.asyncio
async def test_agentic_context_management(fake_model, tmp_path):
    # context_manager="auto" trades the model-driven context tools (summarize/truncate/pin — those
    # are exclusive to "agentic") for the SDK's own hard-enforced compression threshold, which fires
    # regardless of whether the model ever asks.
    agent = await build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model
    )
    names = set(agent.tool_names)
    assert not ({"summarize_context", "truncate_context", "pin_context"} & names)
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
    # and `subagent` only returns the child's final text anyway, so an in-subagent loop would be
    # invisible to the orchestrator.
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
    from strands.session import RepositorySessionManager, SnapshotSessionManager
    from strands.storage import InMemoryStorage

    from strandly.memory.session_store import FailClosedStorage
    from strandly.memory.stash import ReadableStash

    # The top agent (spawn_depth=0) gets a SNAPSHOT session manager over the session store, and the
    # SAME store as its agent-level storage (so the context manager's stash is durable)...
    top = await build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path), session_id="s1"), model=fake_model
    )
    mgr = top._session_manager
    assert isinstance(mgr, SnapshotSessionManager)
    # Never a repository-format manager: SDK >=1.63 refuses one alongside a context manager.
    assert not isinstance(mgr, RepositorySessionManager)
    assert isinstance(top.storage, ReadableStash) and isinstance(top.storage.inner, FailClosedStorage)
    assert mgr._raw_storage is top.storage.inner
    assert top.context_manager.stash_is_durable is True
    # ...but a subagent does NOT. Subagents are ephemeral and built with a context-less
    # RuntimeContext — attaching a session would make concurrent subagents share one session,
    # interleave tool blocks, and corrupt the history (toolResult > toolUse → ConverseStream
    # ValidationException). Keep subagents session-less, with an in-memory stash.
    sub = await build_agent(
        Config(values={}),
        RuntimeContext(cwd=str(tmp_path)),
        model=fake_model,
        system_prompt="You are a strict reviewer.",
        spawn_depth=1,
    )
    assert sub._session_manager is None
    assert isinstance(sub.storage, ReadableStash) and isinstance(sub.storage.inner, InMemoryStorage)
    assert sub.context_manager.stash_is_durable is False


@pytest.mark.asyncio
async def test_sessionless_top_level_invoke_persists_nothing(fake_model, tmp_path):
    # No session id/key → no session at all (it used to collapse onto one shared literal "session",
    # which on a shared store would let unrelated sessionless runs overwrite each other).
    agent = await build_agent(Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model)
    from strands.storage import InMemoryStorage

    assert agent._session_manager is None
    assert isinstance(agent.storage.inner, InMemoryStorage)


@pytest.mark.asyncio
async def test_prompt_identity_resolved_from_token(fake_model, tmp_path, monkeypatch):
    # The identity comes off the token itself (viewer { login }) — nothing to configure.
    from strandly.tools import github_client as gh

    monkeypatch.setattr(gh, "_query_viewer_login", lambda token: "example-agent")
    agent = await build_agent(
        Config(values={"STRANDLY_GITHUB_TOKEN": "ghp_x"}),
        RuntimeContext(cwd=str(tmp_path)),
        model=fake_model,
    )
    prompt = str(agent.system_prompt)
    assert "## Your GitHub identity" in prompt
    assert "`example-agent`" in prompt


@pytest.mark.asyncio
async def test_prompt_no_identity_when_unresolvable(fake_model, tmp_path):
    # A token whose viewer lookup fails (stubbed to None in conftest for hermeticity), or no token
    # at all → NO identity block. Never guess an identity for the agent.
    agent = await build_agent(
        Config(values={"STRANDLY_GITHUB_TOKEN": "ghp_x"}),
        RuntimeContext(cwd=str(tmp_path)),
        model=fake_model,
    )
    assert "Your GitHub identity" not in str(agent.system_prompt)

    tokenless = await build_agent(
        Config(values={}),
        RuntimeContext(cwd=str(tmp_path)),
        model=fake_model,
    )
    assert "Your GitHub identity" not in str(tokenless.system_prompt)


@pytest.mark.asyncio
async def test_allowed_tools_scopes_the_built_agent(fake_model, tmp_path):
    """`allowed_tools` is a real capability gate: only the named action tools get built.

    This is what makes a scoped reviewer role scoped *by construction* — `subagent(tools=[…])` / a role
    prompt's `allowed-tools:` frontmatter resolve to this argument. Plugin-delivered tools (`skills`) are
    out of scope and stay.
    """
    agent = await build_agent(
        Config(values={"STRANDLY_GITHUB_TOKEN": "ghp_x"}),
        RuntimeContext(cwd=str(tmp_path)),
        model=fake_model,
        allowed_tools=frozenset({"shell", "read", "use_github"}),
    )
    names = set(agent.tool_names)
    assert {"shell", "read", "use_github"} <= names
    # Removed, not merely discouraged: no file mutation, no fetching, no nested delegation.
    assert not ({"write", "edit", "web_fetch", "subagent"} & names)
    # The prompt is rebuilt from the real tool set, so it never advertises a scoped-out tool.
    # `write`/`edit` are the load-bearing case: they used to be named unconditionally in the global
    # prompt ("your file/exec tools are shell, read, write and edit"), which told every read-only
    # reviewer it could edit — the model then plans an edit, can't call it, and improvises.
    prompt = str(agent.system_prompt)
    assert "`subagent`" not in prompt
    assert "`write`" not in prompt and "`edit`" not in prompt
    # ...and the tools it DOES have are still described, with their usage guidance.
    assert "`shell`" in prompt and "`read`" in prompt
    assert "rg" in prompt  # the shell block's search guidance survived the move out of global_prompt


@pytest.mark.asyncio
async def test_unscoped_agent_still_describes_the_file_tools(fake_model, tmp_path):
    """Moving the file/exec guidance into the capabilities section must not lose it by default."""
    agent = await build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model
    )
    prompt = str(agent.system_prompt)
    for expected in ("`shell`", "`read`", "`write`", "`edit`"):
        assert expected in prompt, expected
    assert "overwrite a file" in prompt and "exact-string replacement" in prompt


@pytest.mark.asyncio
async def test_allowed_tools_can_drop_github(fake_model, tmp_path):
    """A scope that omits `use_github` has no GitHub API at all — the bug-verify roles rely on it."""
    agent = await build_agent(
        Config(values={"STRANDLY_GITHUB_TOKEN": "ghp_x"}),
        RuntimeContext(cwd=str(tmp_path)),
        model=fake_model,
        allowed_tools=frozenset({"shell", "read", "write"}),
    )
    assert "use_github" not in set(agent.tool_names)


@pytest.mark.asyncio
async def test_the_factory_never_warms_the_sandbox(fake_model, tmp_path):
    """Neither the top agent nor a subagent wakes the sandbox at build (docs/operations.md).

    An eager ``warm_up`` used to start a sandbox session — an EC2 instance, a 40 GiB EBS volume and
    a TTL schedule — for every run, and the skills push then *waited* for it (~83 s) before the
    first model call. The sandbox now wakes at its first use, so the factory must not call it.
    """
    from strands.sandbox.not_a_sandbox_local_environment import NotASandboxLocalEnvironment

    import strandly.core.agent as agent_mod
    from strandly.skills import loader as loader_module

    warmed: list[str] = []

    class WarmableSandbox(NotASandboxLocalEnvironment):
        def warm_up(self) -> None:
            warmed.append("warm")

    parent_sandbox = WarmableSandbox()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(agent_mod, "build_sandbox", lambda config, ctx=None: parent_sandbox)
        # The loader classifies anything whose class name isn't the local sandbox's as *remote* and
        # pushes skills into it, so redirect the push at a temp dir (in production it is an absolute
        # path under the sandbox runtime's mount, which a test must never write to).
        mp.setattr(loader_module, "_SANDBOX_SKILLS_DIR", str(tmp_path / "sandbox-skills"))
        await build_agent(Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model)
        await build_agent(
            Config(values={}),
            RuntimeContext(cwd=str(tmp_path)),
            model=fake_model,
            sandbox=parent_sandbox,
            spawn_depth=1,
        )
        assert warmed == []


@pytest.mark.asyncio
async def test_a_sandbox_without_warm_up_is_fine(fake_model, tmp_path):
    """The local sandbox has no ``warm_up``; building an agent on it must not blow up.

    ``warm_up`` is a sandbox-runtime affordance, not part of the SDK ``Sandbox`` interface, so the
    factory has to treat it as optional.
    """
    agent = await build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model
    )
    assert agent.tool_names


@pytest.mark.asyncio
async def test_agent_built_logs_model_and_effort(fake_model, tmp_path, caplog):
    fake_model.update_config(
        model_id="global.anthropic.claude-opus-5-5",
        additional_request_fields={
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
            "anthropic_beta": ["x"],
        },
    )
    with caplog.at_level("INFO", logger="strandly.core.agent"):
        await build_agent(Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model)
    [line] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("agent built:")]
    assert "model=global.anthropic.claude-opus-5-5 effort=auto" in line
    assert 'reasoning={"thinking":{"type":"adaptive"},"output_config":{"effort":"high"}}' in line


def test_model_settings_tolerates_any_model():
    from strandly.core.agent import _model_settings

    class _NoConfig:
        def get_config(self):
            raise RuntimeError("nope")

    assert _model_settings(_NoConfig()) == ("-", "-")
    assert _model_settings(object()) == ("-", "-")
