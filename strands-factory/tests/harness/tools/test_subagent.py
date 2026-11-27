"""The ``subagent`` tool: the harness's delegation tool with Strandly's builder and input checks."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

import pytest
from strands.agent.state import AgentState
from strands.sandbox.not_a_sandbox_local_environment import NotASandboxLocalEnvironment
from strands_harness.defaults import DEFAULT_MODEL
from strands_harness.tools.subagent import AgentSpec

from conftest import FakeModel, text_response_events
from model_seam import use_model
from strandly.core.agent import build_agent
from strandly.core.config import Config
from strandly.core.constants import MODEL_TIERS, SUBAGENT_MAX_DEPTH
from strandly.core.context import RuntimeContext
from strandly.tools.subagent import (
    PARENT_SESSION_TRACE_ATTRIBUTE,
    RUN_CANCEL_SIGNAL_STATE_KEY,
    SUBAGENT_TOOL_NAME,
    load_roles,
    make_builder,
    make_subagent_tool,
    offered_tools,
    parse_tool_scope,
    prepare_input,
)

OFFERED = offered_tools(None)
ROLE = "---\nallowed-tools: shell read web_fetch\n---\n\n# Role: Reviewer\nYou inspect and report."


def _roles(tmp_path, files: dict[str, str] | None = None):
    """``load_roles`` over a skills root holding ``files`` (``{"<skill>/<role>": text}``)."""
    for name, text in (files or {"review/reviewer": ROLE}).items():
        skill, role = name.split("/")
        path = tmp_path / "skills" / skill / "assets" / "roles" / f"{role}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return load_roles(tmp_path / "skills")


class _Parent:
    """What the harness tool reads off the delegating agent."""

    def __init__(self) -> None:
        self.state = AgentState()
        self.cancel_signal = threading.Event()
        self.messages: list[Any] = []


def _tool(tmp_path, **kwargs):
    config = Config(values={})
    ctx = RuntimeContext(cwd=str(tmp_path))
    return make_subagent_tool(config, ctx, NotASandboxLocalEnvironment(), **kwargs)


async def _call(tool, inp: dict[str, Any], parent: _Parent | None = None) -> tuple[str, str, list]:
    """(status, text, every event) for one call."""
    tool_use = {"toolUseId": "t1", "name": SUBAGENT_TOOL_NAME, "input": inp}
    events = [ev async for ev in tool.stream(tool_use, {"agent": parent or _Parent()})]
    result = events[-1].tool_result
    text = "".join(b.get("text", "") for b in result["content"])
    return result["status"], text, events


@pytest.fixture
def child_model(monkeypatch):
    """Children run on a text model; records the model string each build asked for."""
    models: list[str] = []

    def factory(model: str) -> FakeModel:
        models.append(model)
        return FakeModel(events=text_response_events("child report"))

    use_model(monkeypatch, factory)
    return models


@pytest.fixture
def assembled(monkeypatch):
    """Capture every child assembly instead of building one."""
    import strandly.core.agent as agent_mod

    calls: list[dict[str, Any]] = []

    class _Child:
        tool_names: list[str] = []
        state = AgentState()

        async def stream_async(self, prompt, **kwargs):
            yield {"data": "ok"}
            yield {"result": _Result("ok")}

    def fake_assemble(config, ctx, **kwargs):
        calls.append({"ctx": ctx, **kwargs})
        return _Child()

    monkeypatch.setattr(agent_mod, "assemble_agent", fake_assemble)
    return calls


class _Result:
    stop_reason = "end_turn"
    interrupts: list[Any] = []

    def __init__(self, text: str) -> None:
        self._text = text

    def __str__(self) -> str:
        return self._text


# --- input checks -----------------------------------------------------------------------------


def test_load_roles_reads_scope_body_and_title(tmp_path):
    roles = _roles(tmp_path, {"review/reviewer": ROLE, "port/implementer": "# Role: Implementer\nGo."})
    assert list(roles) == ["port/implementer", "review/reviewer"]
    reviewer = roles["review/reviewer"]
    assert reviewer.tools == ["read", "shell", "web_fetch"]
    assert reviewer.instructions.startswith("# Role: Reviewer") and "allowed-tools" not in reviewer.instructions
    assert reviewer.description == "Reviewer"
    assert roles["port/implementer"].tools is None  # no frontmatter: the full set


@pytest.mark.parametrize(
    "text",
    [
        "---\nallowed-tools: shell\n",  # unterminated
        "---\nallowed-tools:\n---\nrole",  # empty scope
        "---\nallowed_tools: shell\n---\nrole",  # typo'd key
        "---\nallowed-tools: shell\nmodel: fast\n---\nrole",  # another key
        "# Role: X\nallowed-tools: shell",  # scope outside frontmatter
    ],
)
def test_a_malformed_role_file_fails_loading(tmp_path, text):
    with pytest.raises(ValueError, match="role r/x"):
        _roles(tmp_path, {"r/x": text})


def _numbered(text: str) -> str:
    return "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(text.split("\n"), 1))


@pytest.mark.parametrize(
    "instructions",
    [
        ROLE,
        f"[Truncated: 1 block, ~2,000 tokens]\n\n{ROLE}",
        _numbered(ROLE),
        '{"json":{"output":"' + ROLE.replace("\n", "\\n") + '"}}',
    ],
    ids=["verbatim", "truncated-header", "numbered", "json-escaped"],
)
def test_pasted_role_content_is_an_error_naming_agent_type(instructions):
    # A pasted role would run without its scope; the role is selected by name instead.
    with pytest.raises(ValueError, match="agent_type"):
        prepare_input({"task": "x", "instructions": instructions}, OFFERED)


def test_agent_type_scopes_when_tools_is_omitted(tmp_path):
    roles = _roles(tmp_path)
    out = prepare_input({"task": "x", "agent_type": "review/reviewer"}, OFFERED, roles)
    assert out["tools"] == ["read", "shell", "web_fetch"]
    assert "instructions" not in out


def test_tools_overrides_the_roles_scope(tmp_path):
    out = prepare_input({"task": "x", "agent_type": "review/reviewer", "tools": ["read"]}, OFFERED, _roles(tmp_path))
    assert out["tools"] == ["read"]


def test_agent_type_and_instructions_together_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="not both"):
        prepare_input({"task": "x", "agent_type": "review/reviewer", "instructions": "be terse"}, OFFERED, _roles(tmp_path))


def test_a_role_scope_naming_tools_the_parent_lacks_is_an_error(tmp_path):
    offered = offered_tools(frozenset({"shell", "read"}))
    with pytest.raises(ValueError, match="review/reviewer role's `allowed-tools:`.*don't have.*web_fetch"):
        prepare_input({"task": "x", "agent_type": "review/reviewer"}, offered, _roles(tmp_path))


def test_an_empty_scope_is_an_error_not_the_full_set():
    for inp in (
        {"task": "x", "tools": []},
        {"task": "x", "tools": ""},
    ):
        with pytest.raises(ValueError, match="is empty, and a subagent can't have no tools"):
            prepare_input(inp, OFFERED)


def test_the_spec_states_the_non_empty_tools_rule(tmp_path):
    spec = _tool(tmp_path).tool_spec
    assert spec["inputSchema"]["json"]["properties"]["tools"]["minItems"] == 1
    assert "an empty list is rejected" in spec["description"]


def test_a_scope_naming_tools_the_parent_lacks_is_an_error():
    offered = offered_tools(frozenset({"shell", "read"}))
    with pytest.raises(ValueError, match="don't have.*: edit write\\. .*read shell"):
        prepare_input({"task": "x", "tools": ["read", "write", "edit"]}, offered)
    assert prepare_input({"task": "x", "tools": ["read"]}, offered)["tools"] == ["read"]


def test_a_scope_of_only_subagent_is_an_error():
    with pytest.raises(ValueError, match="no tools"):
        prepare_input({"task": "x", "tools": ["subagent", "skills"]}, OFFERED)


def test_parse_tool_scope_accepts_lists_and_the_frontmatter_spellings():
    expected = frozenset({"shell", "read"})
    for raw in (["shell", "read"], "shell read", "[shell, read]", "shell, read", '"shell" read'):
        assert parse_tool_scope(raw) == expected
    assert parse_tool_scope(None) == frozenset()


def test_unknown_tool_name_is_an_error():
    with pytest.raises(ValueError, match="unknown tool name.*shel\\b"):
        prepare_input({"task": "x", "tools": ["shel", "read"]}, OFFERED)


def test_retired_and_plugin_names_are_accepted_and_grant_nothing():
    for name in ("add_memory", "add_personal_memory", "think", "skills", "search_memory"):
        out = prepare_input({"task": "x", "tools": ["read", name]}, OFFERED)
        assert out["tools"] == ["read"]


def test_a_scope_of_only_ungated_names_is_an_error():
    with pytest.raises(ValueError, match="unrestricted"):
        prepare_input({"task": "x", "tools": ["skills", "add_memory"]}, OFFERED)


def test_no_scope_leaves_tools_out():
    out = prepare_input({"task": "x", "instructions": "be terse"}, OFFERED)
    assert "tools" not in out and out["instructions"] == "be terse"


def test_subagent_in_a_scope_is_inert():
    out = prepare_input({"task": "x", "tools": ["read", "subagent"]}, OFFERED)
    assert out["tools"] == ["read"]


def test_unknown_model_tier_is_an_error():
    with pytest.raises(ValueError, match="gpt-oss-999.*advanced, default, fast"):
        prepare_input({"task": "x", "model": "gpt-oss-999"}, OFFERED)
    assert prepare_input({"task": "x", "model": "fast"}, OFFERED)["model"] == "fast"


# --- the tool --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_role_builds_its_child_with_its_text_and_scope(tmp_path, assembled):
    tool = _tool(tmp_path, roles=_roles(tmp_path))
    schema = tool.tool_spec["inputSchema"]["json"]
    assert schema["properties"]["agent_type"]["enum"] == ["general", "review/reviewer"]
    assert "- review/reviewer: Reviewer" in tool.tool_spec["description"]

    status, _, _ = await _call(tool, {"task": "look", "agent_type": "review/reviewer"})

    assert status == "success"
    [child] = assembled
    assert child["system_prompt"].startswith("# Role: Reviewer")
    assert child["allowed_tools"] == frozenset({"read", "shell", "web_fetch"})


@pytest.mark.asyncio
async def test_a_bare_call_gets_no_role_and_the_full_set(tmp_path, assembled):
    status, _, _ = await _call(_tool(tmp_path, roles=_roles(tmp_path)), {"task": "look"})
    assert status == "success"
    [child] = assembled
    assert child["system_prompt"] == "" and child["allowed_tools"] is None


def test_schema_follows_the_harness_with_strandly_axes(tmp_path):
    tool = _tool(tmp_path)
    schema = tool.tool_spec["inputSchema"]["json"]
    assert tool.tool_name == "subagent"
    assert set(schema["properties"]) == {"task", "instructions", "tools", "model", "agent_type"}
    assert schema["properties"]["agent_type"]["enum"] == ["general"]
    assert schema["required"] == ["task"]
    assert schema["properties"]["model"]["enum"] == ["default", "fast", "advanced"]
    assert set(schema["properties"]["tools"]["items"]["enum"]) == OFFERED
    assert "subagent" not in OFFERED
    assert "agent_type" in tool.tool_spec["description"]
    assert "default tier" in tool.tool_spec["description"]


@pytest.mark.asyncio
async def test_unknown_tool_name_fails_before_any_child_is_built(tmp_path, assembled):
    status, text, _ = await _call(_tool(tmp_path), {"task": "x", "tools": ["shel"]})
    assert status == "error" and "shel" in text
    assert assembled == []


@pytest.mark.asyncio
async def test_unknown_model_fails_before_any_child_is_built(tmp_path, assembled):
    status, text, _ = await _call(_tool(tmp_path), {"task": "x", "model": "opus-9"})
    assert status == "error" and "opus-9" in text
    assert assembled == []


@pytest.mark.asyncio
async def test_children_share_the_parent_sandbox_and_mcp_clients(tmp_path, assembled):
    sandbox, clients = object(), [object()]
    ctx = RuntimeContext(cwd=str(tmp_path), session_id="s1", event={"slackChannelId": "D1"})
    tool = make_subagent_tool(Config(values={}), ctx, sandbox, mcp_clients=clients)

    status, text, _ = await _call(tool, {"task": "go"})

    assert (status, text) == ("success", "ok")
    [call] = assembled
    assert call["sandbox"] is sandbox and call["mcp_clients"] is clients
    assert call["spawn_depth"] == 1 and call["allowed_tools"] is None
    child_ctx = call["ctx"]
    assert child_ctx.session_id is None and child_ctx.session_key is None
    assert child_ctx.parent_session_id == "s1"
    assert child_ctx.event == {"slackChannelId": "D1"} and child_ctx.cwd == str(tmp_path)


@pytest.mark.asyncio
async def test_a_role_scope_and_tier_reach_the_child_build(tmp_path, assembled):
    await _call(_tool(tmp_path, roles=_roles(tmp_path)), {"task": "go", "agent_type": "review/reviewer", "model": "advanced"})
    [call] = assembled
    assert call["allowed_tools"] == frozenset({"shell", "read", "web_fetch"})
    assert (call["model"], call["effort"]) == MODEL_TIERS["advanced"]
    assert call["system_prompt"].startswith("# Role: Reviewer")


@pytest.mark.asyncio
async def test_child_stream_events_stay_off_the_parent_stream(tmp_path, assembled):
    from strands.types._events import ToolResultEvent

    _, _, events = await _call(_tool(tmp_path), {"task": "go"})
    assert len(events) == 1 and isinstance(events[0], ToolResultEvent)


@pytest.mark.asyncio
async def test_depth_budget_refuses_a_spent_parent(tmp_path, assembled):
    parent = _Parent()
    parent.state.set("subagent_depth", 0)
    status, text, _ = await _call(_tool(tmp_path), {"task": "go"}, parent)
    assert status == "error" and "depth limit" in text
    assert assembled == []


@pytest.mark.asyncio
async def test_a_cancelled_parent_cancels_its_child(tmp_path, child_model):
    parent = _Parent()
    parent.cancel_signal.set()
    status, text, _ = await _call(_tool(tmp_path), {"task": "go"}, parent)
    assert status == "error" and "cancelled" in text


@pytest.mark.asyncio
async def test_a_call_after_the_run_was_cancelled_builds_nothing(tmp_path, assembled):
    cancelled = threading.Event()
    cancelled.set()
    tool_use = {"toolUseId": "t1", "name": SUBAGENT_TOOL_NAME, "input": {"task": "go"}}
    state = {"agent": _Parent(), RUN_CANCEL_SIGNAL_STATE_KEY: cancelled}
    [event] = [ev async for ev in _tool(tmp_path).stream(tool_use, state)]
    assert event.tool_result["status"] == "error"
    assert "cancelled before this subagent started" in event.tool_result["content"][0]["text"]
    assert assembled == []


# --- real children ---------------------------------------------------------------------------


def _child(tmp_path, ctx: RuntimeContext | None = None, **spec: Any):
    config = Config(values={})
    ctx = ctx or RuntimeContext(cwd=str(tmp_path))
    return make_builder(config, ctx, NotASandboxLocalEnvironment())(AgentSpec(task="x", **spec))


def test_child_gets_the_strandly_prompt_with_its_role(tmp_path, child_model):
    child = _child(tmp_path, instructions="# Role: Reviewer\nYou inspect and report.")
    prompt = child.system_prompt
    assert "# Role: Reviewer" in prompt
    assert "## Capabilities" in prompt  # the composed Strandly prompt, not the harness default
    assert child.trace_attributes["service.name"] == "strands-factory"
    assert child.callback_handler.__name__ == "null_callback_handler"


def test_child_spans_name_the_parent_session(tmp_path, child_model):
    child = _child(tmp_path, RuntimeContext(cwd=str(tmp_path), session_id="gh-o-r-pr-1"))
    assert child.trace_attributes[PARENT_SESSION_TRACE_ATTRIBUTE] == "gh-o-r-pr-1"
    assert "session.id" not in child.trace_attributes
    assert PARENT_SESSION_TRACE_ATTRIBUTE not in _child(tmp_path).trace_attributes


def test_child_runs_the_chosen_tier(tmp_path, child_model):
    _child(tmp_path, model="fast")
    _child(tmp_path)
    assert child_model == [MODEL_TIERS["fast"][0], DEFAULT_MODEL]


def test_the_default_tier_inherits_the_top_agents_effort(tmp_path, monkeypatch):
    import strands_harness.agent as harness_agent

    seen: list[tuple[str, str]] = []

    def resolve(model: Any, default: str, effort: str = "auto", **kwargs: Any) -> FakeModel:
        seen.append((model, effort))
        return FakeModel(events=text_response_events("child report"))

    monkeypatch.setattr(harness_agent, "resolve_model", resolve)
    _child(tmp_path)
    _child(tmp_path, model="fast")
    _child(tmp_path, model="advanced")
    assert seen == [(DEFAULT_MODEL, "medium"), MODEL_TIERS["fast"], MODEL_TIERS["advanced"]]


def test_child_scope_is_enforced_at_construction(tmp_path, child_model):
    child = _child(tmp_path, tools=["shell", "read"])
    names = set(child.tool_names)
    assert {"shell", "read"} <= names
    assert not names & {"write", "edit", "web_fetch", "use_github"}


def test_child_cannot_delegate(tmp_path, child_model):
    assert SUBAGENT_MAX_DEPTH == 1
    assert "subagent" not in _child(tmp_path).tool_names
    assert "strands_manage_background_task" not in _child(tmp_path).tool_names


@pytest.mark.asyncio
async def test_child_report_is_the_tool_result(tmp_path, child_model):
    status, text, _ = await _call(_tool(tmp_path), {"task": "go", "instructions": "be terse"})
    assert (status, text.strip()) == ("success", "child report")


# --- through a real parent -------------------------------------------------------------------


class _ScriptedModel(FakeModel):
    def __init__(self, turns: list[list[dict[str, Any]]]) -> None:
        super().__init__()
        self._turns = list(turns)
        self.specs: list[dict[str, Any]] = []

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):  # type: ignore[override]
        self.specs.append({t["name"]: t for t in tool_specs or []})
        for event in self._turns.pop(0) if self._turns else text_response_events("done"):
            yield event


def _tool_use(name: str, inp: dict[str, Any], tool_use_id: str) -> list[dict[str, Any]]:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tool_use_id, "name": name}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(inp)}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
    ]


@pytest.mark.asyncio
async def test_a_top_level_call_runs_in_the_background_and_delivers_the_report(
    tmp_path, monkeypatch, caplog, child_model
):
    caplog.set_level(logging.INFO)
    monkeypatch.chdir(tmp_path)
    model = _ScriptedModel(
        [
            _tool_use("subagent", {"task": "review it", "model": "fast"}, "s1"),
            text_response_events("dispatched; waiting"),
            text_response_events("got the report"),
        ]
    )
    agent = await build_agent(Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=model)

    assert [t.tool_name for t in agent._background_tasks._config["always"]] == ["subagent"]
    await agent.invoke_async("go")

    # The subagent's spec carries no background flag: the harness always detaches it.
    assert "_background_execution" not in json.dumps(model.specs[0]["subagent"])
    blocks = [b for m in agent.messages for b in m["content"]]
    ack = next(b["toolResult"] for b in blocks if b.get("toolResult", {}).get("toolUseId") == "s1")
    assert "Background task dispatched" in ack["content"][0]["text"]
    gets = [b["toolUse"]["toolUseId"] for b in blocks if b.get("toolUse", {}).get("name") == (
        "strands_manage_background_task"
    )]
    delivered = [
        b["toolResult"] for b in blocks if b.get("toolResult", {}).get("toolUseId") in gets
    ]
    assert delivered and "child report" in json.dumps(delivered)
    _assert_finished_cleanly(caplog, "s1", "success")


def _assert_finished_cleanly(caplog, tool_use_id: str, status: str = "") -> None:
    """The call logged its finish line and nothing at ERROR (no generator/context teardown error)."""
    assert f"subagent finished: tool_use_id={tool_use_id} status={status}" in caplog.text
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, errors


class _SlowModel(FakeModel):
    """Streams a long answer slowly; records whether it was cut off."""

    def __init__(self, started: threading.Event) -> None:
        super().__init__()
        self.started = started
        self.finished = False
        self.closed = threading.Event()

    async def stream(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        import asyncio

        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        self.started.set()
        try:
            for _ in range(200):
                await asyncio.sleep(0.02)
                yield {"contentBlockDelta": {"delta": {"text": "."}}}
            self.finished = True
        finally:
            self.closed.set()
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


@pytest.mark.asyncio
async def test_cancelling_the_run_stops_a_background_child(tmp_path, monkeypatch, caplog):
    """The serving layer cancels a run through its own ``cancel_signal``; a background subagent
    child watches the parent agent's signal, so it stops instead of running to completion."""

    caplog.set_level(logging.INFO)

    started = threading.Event()
    child_model = _SlowModel(started)

    monkeypatch.setitem(MODEL_TIERS, "default", ("child", "auto"))
    use_model(
        monkeypatch,
        lambda model: child_model if model == "child" else FakeModel(text_response_events("summary")),
    )
    monkeypatch.chdir(tmp_path)
    model = _ScriptedModel(
        [
            _tool_use("subagent", {"task": "long review"}, "s1"),
            text_response_events("waiting on the review"),
        ]
    )
    agent = await build_agent(Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=model)
    run_signal = threading.Event()

    async def cancel_when_the_child_runs() -> None:
        while not started.is_set():
            await asyncio.sleep(0.01)
        run_signal.set()

    began = time.monotonic()
    canceller = asyncio.create_task(cancel_when_the_child_runs())
    async for _ in agent.stream_async("go", invocation_state={}, cancel_signal=run_signal):
        pass
    await canceller

    assert started.is_set()
    assert time.monotonic() - began < 3  # the child alone would stream for ~4s
    for _ in range(100):
        if child_model.closed.is_set():
            break
        await asyncio.sleep(0.01)
    assert child_model.closed.is_set() and not child_model.finished
    await _wait_for(lambda: "subagent finished: tool_use_id=s1" in caplog.text)
    _assert_finished_cleanly(caplog, "s1")


async def _wait_for(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_cancelling_the_run_stops_a_queued_call_before_it_builds(
    tmp_path, monkeypatch, caplog
):
    """Two calls, one background slot: cancelling the run while the first runs means the queued
    second never builds a child (the run's signal reaches it through the invocation state)."""
    import strandly.core.agent as agent_mod
    from strandly.serve.turn import run_turn

    caplog.set_level(logging.INFO)
    monkeypatch.setattr(agent_mod, "BACKGROUND_MAX_CONCURRENCY", 1)
    started = threading.Event()
    child_model = _SlowModel(started)
    monkeypatch.setitem(MODEL_TIERS, "default", ("child", "auto"))
    use_model(
        monkeypatch,
        lambda model: child_model if model == "child" else FakeModel(text_response_events("summary")),
    )
    real_assemble = agent_mod.assemble_agent
    child_builds: list[str] = []

    def counting_assemble(config, ctx, **kwargs):
        if kwargs.get("spawn_depth"):
            child_builds.append(kwargs.get("system_prompt", ""))
        return real_assemble(config, ctx, **kwargs)

    monkeypatch.setattr(agent_mod, "assemble_agent", counting_assemble)
    monkeypatch.chdir(tmp_path)
    model = _ScriptedModel(
        [
            _tool_use("subagent", {"task": "a"}, "s1")[:-1]
            + _tool_use("subagent", {"task": "b"}, "s2")[1:],
            text_response_events("waiting"),
        ]
    )
    run_signal = threading.Event()

    async def cancel_when_the_child_runs() -> None:
        await _wait_for(started.is_set)
        run_signal.set()

    canceller = asyncio.create_task(cancel_when_the_child_runs())
    ctx = RuntimeContext(cwd=str(tmp_path))
    async for _ in run_turn(Config(values={}), "go", ctx, model=model, cancel_signal=run_signal):
        pass
    await canceller

    await _wait_for(lambda: "tool_use_id=s2" in caplog.text)
    assert "subagent not started: tool_use_id=s2: the run was cancelled" in caplog.text
    assert len(child_builds) == 1 and not child_model.finished


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_terminal_events_do_not_leak_the_child_command_context(tmp_path, monkeypatch, interrupted):
    import contextlib

    from strands.types._events import ToolInterruptEvent, ToolResultEvent

    from strandly.sandbox.runtime import _COMMAND_CANCEL_SIGNAL, command_cancel_signal

    delegation = _tool(tmp_path)
    run_signal, outer_signal = threading.Event(), threading.Event()
    state = {RUN_CANCEL_SIGNAL_STATE_KEY: run_signal}
    call = {"toolUseId": "t1", "name": SUBAGENT_TOOL_NAME, "input": {"task": "test"}}
    closed = []

    async def terminal(tool_use, invocation_state, **kwargs):
        assert _COMMAND_CANCEL_SIGNAL.get() is run_signal
        try:
            if interrupted:
                yield ToolInterruptEvent(tool_use, [])
            else:
                yield ToolResultEvent({"toolUseId": "t1", "status": "success", "content": []})
        finally:
            closed.append(_COMMAND_CANCEL_SIGNAL.get())

    monkeypatch.setattr(delegation._inner, "stream", terminal)
    with command_cancel_signal(outer_signal):
        async with contextlib.aclosing(delegation.stream(call, state)) as events:
            await anext(events)
            assert _COMMAND_CANCEL_SIGNAL.get() is outer_signal
            assert closed == [run_signal]
            await asyncio.create_task(events.aclose())
            assert _COMMAND_CANCEL_SIGNAL.get() is outer_signal
    assert _COMMAND_CANCEL_SIGNAL.get() is None


@pytest.mark.asyncio
async def test_a_resumed_child_follows_the_run_signal_after_the_parent_clears_its_signal(
    tmp_path, monkeypatch
):
    from strands import Agent, tool
    from strands.types._events import ToolInterruptEvent

    import strandly.core.agent as agent_mod
    from scripted_model import ScriptedModel, tool_use_events

    started = threading.Event()
    slow = _SlowModel(started)

    @tool(context=True)
    def confirm(tool_context) -> str:
        """Ask for permission."""
        return str(tool_context.interrupt("permission", reason="continue?"))

    child = Agent(
        model=ScriptedModel(tool_use_events("confirm", "ask1")),
        tools=[confirm],
        callback_handler=None,
    )
    monkeypatch.setattr(agent_mod, "assemble_agent", lambda *a, **kw: child)
    delegation = _tool(tmp_path)
    parent = _Parent()
    call = {"toolUseId": "t1", "name": SUBAGENT_TOOL_NAME, "input": {"task": "ask"}}
    first = [ev async for ev in delegation.stream(call, {"agent": parent})]
    assert isinstance(first[-1], ToolInterruptEvent)
    for interrupt in child._interrupt_state.interrupts.values():
        interrupt.response = "yes"
    child.model = slow
    run_signal = threading.Event()
    state = {"agent": parent, RUN_CANCEL_SIGNAL_STATE_KEY: run_signal}

    async def collect():
        return [ev async for ev in delegation.stream(call, state)]

    resumed = asyncio.create_task(collect())
    try:
        deadline = time.monotonic() + 5
        while not started.is_set():
            assert time.monotonic() < deadline, "resumed child did not reach the model"
            await asyncio.sleep(0.01)
        run_signal.set()
        # The parent's invocation has ended; only the caller-owned run signal stays set.
        parent.cancel_signal.clear()
        await asyncio.wait_for(asyncio.shield(resumed), 1.5)
        assert slow.closed.is_set() and not slow.finished
    finally:
        resumed.cancel()
        await asyncio.gather(resumed, return_exceptions=True)
