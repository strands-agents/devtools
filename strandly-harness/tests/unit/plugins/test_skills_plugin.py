"""Tests for the ``SystemPromptSkills`` plugin (system-prompt injection of skills).

Exercises sandbox-based skill loading, active-set seeding, the single ``skill`` tool
(activate/deactivate/list/load/unload), the rendered ``<active_skills>`` block, before-model-call
re-injection (full instructions for active skills, no accumulation), and dynamic skill loading
from local folders (persistence across init, refresh/unload semantics, built-in shadowing
guardrail). All run with no AWS / no network: an in-memory fake sandbox + a minimal fake agent.
"""

from __future__ import annotations

import pytest

from strandly_harness.plugins.system_prompt_skills import (
    SystemPromptSkills,
    active_skill_goals,
    load_goals_via_sandbox,
    load_skills_via_sandbox,
)


class FakeSandbox:
    """Minimal in-memory Sandbox: serves list_files/read_text from a {path: bytes} map."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def read_text(self, path: str, encoding: str = "utf-8", **kwargs) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path].decode(encoding)

    async def list_files(self, path: str, **kwargs):
        from strands.sandbox.types import FileInfo

        prefix = path.rstrip("/") + "/"
        names: dict[str, bool] = {}
        for fpath in self.files:
            if not fpath.startswith(prefix):
                continue
            head, _, tail = fpath[len(prefix) :].partition("/")
            names[head] = bool(tail)
        if not names and not any(f == path for f in self.files):
            raise FileNotFoundError(path)
        return [FileInfo(name=n, is_dir=d) for n, d in sorted(names.items())]


class FakeState:
    """Just enough of the agent state API the plugin uses (get/set by key)."""

    def __init__(self) -> None:
        self._data: dict[str, object] = {}

    def get(self, key: str):
        return self._data.get(key)

    def set(self, key: str, value) -> None:
        self._data[key] = value


class FakeAgent:
    """Minimal agent the plugin reads/writes: sandbox, state, plain-string system prompt."""

    def __init__(self, sandbox: FakeSandbox) -> None:
        self.sandbox = sandbox
        self.state = FakeState()
        self.system_prompt: str | None = "BASE PROMPT"
        # Plain-string prompt path (no structured content blocks).
        self.system_prompt_content = None


class Ctx:
    """Stand-in for the SDK tool_context (only ``.agent`` is read)."""

    def __init__(self, agent: FakeAgent) -> None:
        self.agent = agent


def _add_skill(sb: FakeSandbox, root: str, name: str, description: str, body: str) -> None:
    sb.files[f"{root}/{name}/SKILL.md"] = (
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n".encode()
    )


async def _agent_with(skills: dict[str, tuple[str, str]], *, activate=None) -> tuple:
    """Build (plugin, agent) loaded from a fake sandbox holding the given skills."""
    sb = FakeSandbox()
    for name, (desc, body) in skills.items():
        _add_skill(sb, "/opt/skills", name, desc, body)
    plugin = SystemPromptSkills(["/opt/skills"], activate_by_default=activate)
    agent = FakeAgent(sb)
    await plugin.init_agent(agent)
    return plugin, agent


# ---- sandbox loading ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_skills_via_sandbox_parent_dir():
    sb = FakeSandbox()
    _add_skill(sb, "/opt/skills", "pdf", "pdf things", "extract pdfs")
    _add_skill(sb, "/opt/skills", "web", "web things", "search web")
    skills = await load_skills_via_sandbox(sb, ["/opt/skills"])
    assert set(skills) == {"pdf", "web"}
    assert skills["pdf"].instructions == "extract pdfs"


@pytest.mark.asyncio
async def test_load_skills_via_sandbox_skips_bad_skill():
    sb = FakeSandbox()
    _add_skill(sb, "/opt/skills", "good", "ok", "body")
    sb.files["/opt/skills/bad/SKILL.md"] = b"not valid frontmatter"
    skills = await load_skills_via_sandbox(sb, ["/opt/skills"])
    assert set(skills) == {"good"}  # bad one logged-and-skipped


# ---- active-set seeding ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_activates_no_skills():
    plugin, agent = await _agent_with({"pdf": ("pdf things", "x"), "web": ("web things", "y")})
    assert plugin.get_active_skills(agent) == []


@pytest.mark.asyncio
async def test_activate_by_default_subset():
    plugin, agent = await _agent_with(
        {"pdf": ("pdf things", "x"), "web": ("web things", "y")}, activate=["pdf"]
    )
    assert plugin.get_active_skills(agent) == ["pdf"]


@pytest.mark.asyncio
async def test_activate_by_default_skips_unknown():
    plugin, agent = await _agent_with({"pdf": ("pdf things", "x")}, activate=["pdf", "nope"])
    assert plugin.get_active_skills(agent) == ["pdf"]


# ---- the rendered block ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_block_embeds_active_instructions_only():
    plugin, agent = await _agent_with(
        {"pdf": ("pdf things", "EXTRACT_PDFS_INSTR"), "web": ("web things", "SEARCH_WEB_INSTR")},
        activate=["pdf"],
    )
    block = plugin._render_block(agent)
    assert "EXTRACT_PDFS_INSTR" in block
    assert "SEARCH_WEB_INSTR" not in block
    assert 'name="pdf"' in block and 'name="web"' in block
    assert 'active="true"' in block and 'active="false"' in block


@pytest.mark.asyncio
async def test_render_block_escapes_xml():
    plugin, agent = await _agent_with({"danger": ("uses <tags> & ampersands", "body & <x>")})
    block = plugin._render_block(agent)
    assert "&lt;tags&gt;" in block and "&amp;" in block


# ---- activate / deactivate / list tool ------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_tool_activate_deactivate_list():
    plugin, agent = await _agent_with({"pdf": ("pdf things", "extract")})
    ctx = Ctx(agent)

    assert plugin.get_active_skills(agent) == []
    assert "Activated" in await plugin.skill("activate", "pdf", tool_context=ctx)
    assert plugin.get_active_skills(agent) == ["pdf"]
    assert "already active" in await plugin.skill("activate", "pdf", tool_context=ctx)
    assert "not found" in await plugin.skill("activate", "nope", tool_context=ctx)
    assert "requires a 'name'" in await plugin.skill("activate", tool_context=ctx)
    listing = await plugin.skill("list", tool_context=ctx)
    assert "pdf" in listing and "active" in listing
    assert "unknown action" in await plugin.skill("frobnicate", "pdf", tool_context=ctx)
    assert "Deactivated" in await plugin.skill("deactivate", "pdf", tool_context=ctx)
    assert plugin.get_active_skills(agent) == []
    assert "is not active" in await plugin.skill("deactivate", "pdf", tool_context=ctx)


# ---- re-injection: no accumulation, toggling takes effect -----------------------------------


@pytest.mark.asyncio
async def test_reinjection_rebuilds_without_accumulation():
    from strands.hooks import BeforeModelCallEvent

    plugin, agent = await _agent_with({"pdf": ("pdf things", "PDF_INSTR")}, activate=["pdf"])
    event = BeforeModelCallEvent(agent=agent)
    plugin.reinject(event)
    plugin.reinject(event)
    plugin.reinject(event)
    prompt = agent.system_prompt or ""
    assert prompt.count("<active_skills>") == 1  # exactly once despite three injections
    assert "PDF_INSTR" in prompt
    assert prompt.startswith("BASE PROMPT")  # base prompt preserved


@pytest.mark.asyncio
async def test_reinjection_reflects_deactivation():
    from strands.hooks import BeforeModelCallEvent

    plugin, agent = await _agent_with({"pdf": ("pdf things", "PDF_INSTR")}, activate=["pdf"])
    event = BeforeModelCallEvent(agent=agent)
    plugin.reinject(event)
    assert "PDF_INSTR" in (agent.system_prompt or "")

    await plugin.skill("deactivate", "pdf", tool_context=Ctx(agent))
    plugin.reinject(event)
    prompt = agent.system_prompt or ""
    assert "PDF_INSTR" not in prompt  # dropped after deactivation
    assert prompt.count("<active_skills>") == 1  # still no accumulation


# ---- GOALS.md loading (critic-facing acceptance criteria) -----------------------------------


def _add_goals(sb: FakeSandbox, root: str, name: str, goals: str) -> None:
    sb.files[f"{root}/{name}/GOALS.md"] = goals.encode()


@pytest.mark.asyncio
async def test_load_goals_via_sandbox_reads_sibling_goals():
    sb = FakeSandbox()
    _add_skill(sb, "/opt/skills", "review", "review things", "do a review")
    _add_skill(sb, "/opt/skills", "test", "test things", "run tests")
    _add_goals(sb, "/opt/skills", "review", "VERIFY_THE_VERDICT")
    skills = await load_skills_via_sandbox(sb, ["/opt/skills"])
    goals = await load_goals_via_sandbox(sb, skills)
    assert goals == {"review": "VERIFY_THE_VERDICT"}  # only the skill with a GOALS.md


@pytest.mark.asyncio
async def test_load_goals_strips_and_skips_empty():
    sb = FakeSandbox()
    _add_skill(sb, "/opt/skills", "a", "a", "body")
    _add_skill(sb, "/opt/skills", "b", "b", "body")
    _add_goals(sb, "/opt/skills", "a", "   \n  TRIMMED  \n ")
    _add_goals(sb, "/opt/skills", "b", "   \n\n  ")  # whitespace-only → skipped
    skills = await load_skills_via_sandbox(sb, ["/opt/skills"])
    goals = await load_goals_via_sandbox(sb, skills)
    assert goals == {"a": "TRIMMED"}


@pytest.mark.asyncio
async def test_init_agent_stashes_goals_in_state():
    sb = FakeSandbox()
    _add_skill(sb, "/opt/skills", "review", "review things", "do a review")
    _add_goals(sb, "/opt/skills", "review", "CHECK_THIS")
    plugin = SystemPromptSkills(["/opt/skills"])
    agent = FakeAgent(sb)
    await plugin.init_agent(agent)
    assert plugin.get_loaded_goals() == {"review": "CHECK_THIS"}
    # Persisted to state so the critic (which only has the agent) can read it.
    assert agent.state.get("system_prompt_skills")["goals"] == {"review": "CHECK_THIS"}


@pytest.mark.asyncio
async def test_active_skill_goals_only_returns_active_with_goals():
    sb = FakeSandbox()
    _add_skill(sb, "/opt/skills", "review", "review things", "x")
    _add_skill(sb, "/opt/skills", "test", "test things", "y")
    _add_skill(sb, "/opt/skills", "plain", "no goals", "z")
    _add_goals(sb, "/opt/skills", "review", "REVIEW_GOALS")
    _add_goals(sb, "/opt/skills", "test", "TEST_GOALS")
    plugin = SystemPromptSkills(["/opt/skills"])
    agent = FakeAgent(sb)
    await plugin.init_agent(agent)

    # Nothing active → no goals.
    assert active_skill_goals(agent) == {}

    await plugin.skill("activate", "review", tool_context=Ctx(agent))
    await plugin.skill("activate", "plain", tool_context=Ctx(agent))  # active but ships no GOALS.md
    # Only active skills that have goals are returned; "plain" (no goals) and "test" (inactive) excluded.
    assert active_skill_goals(agent) == {"review": "REVIEW_GOALS"}
    assert plugin.get_active_goals(agent) == {"review": "REVIEW_GOALS"}


def test_active_skill_goals_safe_when_uninitialized():
    # No system_prompt_skills state at all → empty dict, no crash.
    sb = FakeSandbox()
    agent = FakeAgent(sb)
    assert active_skill_goals(agent) == {}


@pytest.mark.asyncio
async def test_goals_not_injected_into_actor_prompt():
    from strands.hooks import BeforeModelCallEvent

    sb = FakeSandbox()
    _add_skill(sb, "/opt/skills", "review", "review things", "ACTOR_INSTRUCTIONS")
    _add_goals(sb, "/opt/skills", "review", "CRITIC_ONLY_GOALS")
    plugin = SystemPromptSkills(["/opt/skills"], activate_by_default=["review"])
    agent = FakeAgent(sb)
    await plugin.init_agent(agent)
    plugin.reinject(BeforeModelCallEvent(agent=agent))
    prompt = agent.system_prompt or ""
    assert "ACTOR_INSTRUCTIONS" in prompt  # the skill's instructions ARE injected
    assert "CRITIC_ONLY_GOALS" not in prompt  # the goals are NOT (critic-only)


# ---- engaged-this-turn: critic sees goals for skills toggled off mid-turn -------------------


@pytest.mark.asyncio
async def test_active_skill_goals_includes_skill_deactivated_mid_turn():
    """A skill activated then deactivated within one turn must still surface its GOALS.md.

    Regression for the long-horizon case (implement-then-review): the critic runs once at
    end-of-turn; reading only the currently-active set would silently skip a skill whose work
    happened earlier this turn.
    """
    from strands.hooks import BeforeInvocationEvent

    sb = FakeSandbox()
    _add_skill(sb, "/opt/skills", "implement", "implement things", "x")
    _add_skill(sb, "/opt/skills", "review", "review things", "y")
    _add_goals(sb, "/opt/skills", "implement", "IMPLEMENT_GOALS")
    _add_goals(sb, "/opt/skills", "review", "REVIEW_GOALS")
    plugin = SystemPromptSkills(["/opt/skills"])
    agent = FakeAgent(sb)
    await plugin.init_agent(agent)

    # Turn starts.
    plugin.reset_engaged_this_turn(BeforeInvocationEvent(agent=agent))

    # activate implement -> work -> deactivate implement -> activate review (classic long horizon)
    await plugin.skill("activate", "implement", tool_context=Ctx(agent))
    await plugin.skill("deactivate", "implement", tool_context=Ctx(agent))
    await plugin.skill("activate", "review", tool_context=Ctx(agent))

    # 'implement' is no longer active, but its work happened this turn -> still graded.
    assert active_skill_goals(agent) == {
        "implement": "IMPLEMENT_GOALS",
        "review": "REVIEW_GOALS",
    }


@pytest.mark.asyncio
async def test_engaged_this_turn_resets_each_turn():
    """The engaged set must not bleed across turns: a new turn starts from the active set only."""
    from strands.hooks import BeforeInvocationEvent

    sb = FakeSandbox()
    _add_skill(sb, "/opt/skills", "implement", "implement things", "x")
    _add_skill(sb, "/opt/skills", "review", "review things", "y")
    _add_goals(sb, "/opt/skills", "implement", "IMPLEMENT_GOALS")
    _add_goals(sb, "/opt/skills", "review", "REVIEW_GOALS")
    plugin = SystemPromptSkills(["/opt/skills"])
    agent = FakeAgent(sb)
    await plugin.init_agent(agent)

    # Turn 1: engage 'implement' then drop it.
    plugin.reset_engaged_this_turn(BeforeInvocationEvent(agent=agent))
    await plugin.skill("activate", "implement", tool_context=Ctx(agent))
    await plugin.skill("deactivate", "implement", tool_context=Ctx(agent))
    assert active_skill_goals(agent) == {"implement": "IMPLEMENT_GOALS"}

    # Turn 2 begins: 'implement' work is in the past; it must NOT carry over.
    plugin.reset_engaged_this_turn(BeforeInvocationEvent(agent=agent))
    assert active_skill_goals(agent) == {}
    await plugin.skill("activate", "review", tool_context=Ctx(agent))
    assert active_skill_goals(agent) == {"review": "REVIEW_GOALS"}


@pytest.mark.asyncio
async def test_engaged_this_turn_seeded_from_active_set():
    """A skill carried over active from a prior turn (used but not re-activated) is still graded."""
    from strands.hooks import BeforeInvocationEvent

    sb = FakeSandbox()
    _add_skill(sb, "/opt/skills", "review", "review things", "y")
    _add_goals(sb, "/opt/skills", "review", "REVIEW_GOALS")
    plugin = SystemPromptSkills(["/opt/skills"], activate_by_default=["review"])
    agent = FakeAgent(sb)
    await plugin.init_agent(agent)

    # New turn: 'review' is already active and never re-activated this turn, but reset seeds it.
    plugin.reset_engaged_this_turn(BeforeInvocationEvent(agent=agent))
    assert active_skill_goals(agent) == {"review": "REVIEW_GOALS"}


# ---- dynamic loading: skill(action="load"/"unload", path=...) --------------------------------


@pytest.mark.asyncio
async def test_load_requires_path():
    plugin, agent = await _agent_with({"pdf": ("pdf things", "x")})
    ctx = Ctx(agent)
    assert "requires a 'path'" in await plugin.skill("load", tool_context=ctx)
    assert "requires a 'path'" in await plugin.skill("unload", tool_context=ctx)


@pytest.mark.asyncio
async def test_load_from_folder_merges_into_plugin():
    plugin, agent = await _agent_with({"review": ("review things", "REVIEW_INSTR")})
    sb: FakeSandbox = agent.sandbox
    _add_skill(sb, "/work/repo/skills", "strands-dev", "work with strands", "STRANDS_DEV_INSTR")
    ctx = Ctx(agent)

    msg = await plugin.skill("load", path="/work/repo/skills", tool_context=ctx)
    assert "Loaded 1 skill(s)" in msg and "strands-dev" in msg
    assert plugin.get_dynamic_skills() == {"strands-dev": "/work/repo/skills"}

    # Behaves exactly like a built-in: listable, activatable, rendered when active.
    listing = await plugin.skill("list", tool_context=ctx)
    assert "strands-dev" in listing and "(loaded from /work/repo/skills)" in listing
    assert "Activated" in await plugin.skill("activate", "strands-dev", tool_context=ctx)
    block = plugin._render_block(agent)
    assert "STRANDS_DEV_INSTR" in block and 'name="strands-dev"' in block


@pytest.mark.asyncio
async def test_load_missing_path_reports_no_skills():
    plugin, agent = await _agent_with({"pdf": ("pdf things", "x")})
    msg = await plugin.skill("load", path="/does/not/exist", tool_context=Ctx(agent))
    assert "No skills found" in msg
    assert plugin.get_dynamic_skills() == {}
    # A path that contributed nothing is NOT persisted for reload.
    assert agent.state.get("system_prompt_skills").get("dynamic_paths") in (None, [])


@pytest.mark.asyncio
async def test_load_never_shadows_builtin():
    plugin, agent = await _agent_with({"review": ("review things", "BUILTIN_INSTR")})
    sb: FakeSandbox = agent.sandbox
    _add_skill(sb, "/work/evil", "review", "totally the real review", "HIJACKED_INSTR")
    _add_skill(sb, "/work/evil", "extra", "a fine extra skill", "EXTRA_INSTR")

    msg = await plugin.skill("load", path="/work/evil", tool_context=Ctx(agent))
    assert "Skipped" in msg and "review" in msg
    assert "extra" in msg  # non-colliding sibling still loads
    assert plugin.get_dynamic_skills() == {"extra": "/work/evil"}
    # The built-in's instructions are untouched.
    await plugin.skill("activate", "review", tool_context=Ctx(agent))
    block = plugin._render_block(agent)
    assert "BUILTIN_INSTR" in block
    assert "HIJACKED_INSTR" not in block


@pytest.mark.asyncio
async def test_reload_refreshes_edits_and_removals():
    plugin, agent = await _agent_with({})
    sb: FakeSandbox = agent.sandbox
    _add_skill(sb, "/work/repo/skills", "a", "a things", "A_V1")
    _add_skill(sb, "/work/repo/skills", "b", "b things", "B_V1")
    ctx = Ctx(agent)

    await plugin.skill("load", path="/work/repo/skills", tool_context=ctx)
    await plugin.skill("activate", "b", tool_context=ctx)

    # Edit a, delete b, then re-load the same path.
    _add_skill(sb, "/work/repo/skills", "a", "a things", "A_V2")
    del sb.files["/work/repo/skills/b/SKILL.md"]
    msg = await plugin.skill("load", path="/work/repo/skills", tool_context=ctx)
    assert "Removed on refresh" in msg and "b" in msg

    assert set(plugin.get_dynamic_skills()) == {"a"}
    assert plugin.get_active_skills(agent) == []  # deleted skill left the active set
    await plugin.skill("activate", "a", tool_context=ctx)
    assert "A_V2" in plugin._render_block(agent)  # edits picked up


@pytest.mark.asyncio
async def test_unload_removes_and_deactivates():
    plugin, agent = await _agent_with({"pdf": ("pdf things", "x")})
    sb: FakeSandbox = agent.sandbox
    _add_skill(sb, "/work/repo/skills", "dyn", "dyn things", "DYN_INSTR")
    ctx = Ctx(agent)

    await plugin.skill("load", path="/work/repo/skills", tool_context=ctx)
    await plugin.skill("activate", "dyn", tool_context=ctx)
    msg = await plugin.skill("unload", path="/work/repo/skills", tool_context=ctx)
    assert "Unloaded 1 skill(s)" in msg and "Deactivated: dyn" in msg

    assert plugin.get_dynamic_skills() == {}
    assert plugin.get_active_skills(agent) == []
    assert "dyn" not in plugin._render_block(agent)
    assert agent.state.get("system_prompt_skills").get("dynamic_paths") == []
    # Unloading again (or an unknown path) is a helpful no-op.
    assert "No skills are loaded" in await plugin.skill(
        "unload", path="/work/repo/skills", tool_context=ctx
    )


@pytest.mark.asyncio
async def test_trailing_slash_paths_are_normalized():
    plugin, agent = await _agent_with({})
    sb: FakeSandbox = agent.sandbox
    _add_skill(sb, "/work/repo/skills", "dyn", "dyn things", "DYN")
    ctx = Ctx(agent)
    await plugin.skill("load", path="/work/repo/skills/", tool_context=ctx)  # trailing slash
    msg = await plugin.skill("unload", path="/work/repo/skills", tool_context=ctx)  # without
    assert "Unloaded 1 skill(s)" in msg


@pytest.mark.asyncio
async def test_dynamic_paths_persist_and_reload_on_init():
    """A fresh agent over the same session state re-loads dynamic skills at init_agent."""
    sb = FakeSandbox()
    _add_skill(sb, "/opt/skills", "builtin", "builtin things", "BUILTIN")
    _add_skill(sb, "/work/repo/skills", "dyn", "dyn things", "DYN_INSTR")
    plugin1 = SystemPromptSkills(["/opt/skills"])
    agent1 = FakeAgent(sb)
    await plugin1.init_agent(agent1)
    await plugin1.skill("load", path="/work/repo/skills", tool_context=Ctx(agent1))
    await plugin1.skill("activate", "dyn", tool_context=Ctx(agent1))

    # Simulate session rehydration: a brand-new plugin + agent sharing the persisted state.
    plugin2 = SystemPromptSkills(["/opt/skills"])
    agent2 = FakeAgent(sb)
    agent2.state._data = dict(agent1.state._data)
    await plugin2.init_agent(agent2)

    assert plugin2.get_dynamic_skills() == {"dyn": "/work/repo/skills"}
    assert plugin2.get_active_skills(agent2) == ["dyn"]  # active set survived too
    assert "DYN_INSTR" in plugin2._render_block(agent2)


@pytest.mark.asyncio
async def test_stale_dynamic_path_is_fail_soft_on_init():
    """A persisted path missing from a fresh sandbox must not break init (just loads nothing)."""
    sb = FakeSandbox()
    _add_skill(sb, "/opt/skills", "builtin", "builtin things", "BUILTIN")
    plugin = SystemPromptSkills(["/opt/skills"])
    agent = FakeAgent(sb)
    agent.state.set(
        "system_prompt_skills", {"dynamic_paths": ["/work/gone"], "active": ["dyn"]}
    )
    await plugin.init_agent(agent)  # must not raise
    assert set(s.name for s in plugin.get_loaded_skills()) == {"builtin"}
    assert plugin.get_dynamic_skills() == {}


@pytest.mark.asyncio
async def test_dynamic_skill_goals_feed_the_critic():
    plugin, agent = await _agent_with({})
    sb: FakeSandbox = agent.sandbox
    _add_skill(sb, "/work/repo/skills", "dyn", "dyn things", "DYN_INSTR")
    _add_goals(sb, "/work/repo/skills", "dyn", "DYN_CRITIC_GOALS")
    ctx = Ctx(agent)

    await plugin.skill("load", path="/work/repo/skills", tool_context=ctx)
    await plugin.skill("activate", "dyn", tool_context=ctx)
    assert active_skill_goals(agent) == {"dyn": "DYN_CRITIC_GOALS"}
    # Critic-facing only: never in the actor prompt.
    assert "DYN_CRITIC_GOALS" not in plugin._render_block(agent)

    await plugin.skill("deactivate", "dyn", tool_context=ctx)
    await plugin.skill("unload", path="/work/repo/skills", tool_context=ctx)
    plugin.reset_engaged_this_turn(__import__("strands").hooks.BeforeInvocationEvent(agent=agent))
    assert active_skill_goals(agent) == {}


@pytest.mark.asyncio
async def test_dynamic_collision_between_paths_most_recent_wins():
    plugin, agent = await _agent_with({})
    sb: FakeSandbox = agent.sandbox
    _add_skill(sb, "/work/one", "dup", "from one", "ONE_INSTR")
    _add_skill(sb, "/work/two", "dup", "from two", "TWO_INSTR")
    ctx = Ctx(agent)

    await plugin.skill("load", path="/work/one", tool_context=ctx)
    await plugin.skill("load", path="/work/two", tool_context=ctx)
    assert plugin.get_dynamic_skills() == {"dup": "/work/two"}  # most recent wins

    # Unloading the older path no longer owns 'dup' — the skill stays.
    await plugin.skill("unload", path="/work/one", tool_context=ctx)
    assert "dup" in plugin.get_dynamic_skills()
