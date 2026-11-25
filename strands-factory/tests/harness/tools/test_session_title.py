"""The agent's ``set_session_title`` tool: current-session-only writes, bounds and sanitizing, the
person-title guard, the build gate, and its prompt guidance."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from strandly.core.config import Config
from strandly.core.context import RUN_CONTEXT_STATE_KEY, RuntimeContext
from strandly.core.prompt.compose import compose
from strandly.core.session_ids import SESSION_TITLE_MAX_LEN, session_title_key
from strandly.tools.session_title import SESSION_TITLE_TOOL_NAME, make_session_title_tool
from strandly.tools.toolset import build_tools

SID = "portal-alice-20300101-abcdef"


class _ConditionFailed(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    """A ledger ``Table`` that evaluates the tool's one condition against stored rows."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.fail: Exception | None = None

    def put_item(self, *, Item: dict[str, Any], **kwargs: Any) -> None:
        self.calls.append({"Item": Item, **kwargs})
        if self.fail:
            raise self.fail
        old = self.rows.get(Item["task_id"])
        if "ConditionExpression" in kwargs and old and old.get("title") and str(
            old.get("updated_by", "")
        ).startswith(kwargs["ExpressionAttributeValues"][":person"]):
            raise _ConditionFailed()
        self.rows[Item["task_id"]] = Item


def _tool(table: FakeTable, bound: str | None = SID) -> Any:
    return make_session_title_tool(RuntimeContext(session_id=bound), lambda: table)


def _call(tool: Any, title: Any, turn_session: str | None = SID, **kw: Any) -> dict[str, Any]:
    state = {RUN_CONTEXT_STATE_KEY: RuntimeContext(session_id=turn_session)} if turn_session else {}
    return tool(tool_context=SimpleNamespace(invocation_state=state), title=title, **kw)


def _text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


def test_sets_the_current_sessions_title_row():
    table = FakeTable()
    out = _call(_tool(table), "Flaky retry test")
    assert out["status"] == "success" and "Flaky retry test" in _text(out)
    row = table.rows[session_title_key(SID)]
    assert row["title"] == "Flaky retry test"
    assert row["session_id"] == SID and row["updated_by"] == "agent"
    assert "gsi_pk" not in row  # never listed as a run


def test_tool_has_no_session_argument():
    spec = _tool(FakeTable()).tool_spec
    assert set(spec["inputSchema"]["json"]["properties"]) == {"title", "replace_user_title"}
    assert spec["name"] == SESSION_TITLE_TOOL_NAME


@pytest.mark.parametrize("turn_session", [None, "portal-bob-20300101-other1", "gh-o-r-pr-1"])
def test_refuses_when_the_turn_is_not_the_bound_session(turn_session):
    table = FakeTable()
    out = _call(_tool(table), "Hijack", turn_session=turn_session)
    assert out["status"] == "error"
    assert table.calls == []


def test_refuses_without_a_bound_session():
    table = FakeTable()
    out = _call(_tool(table, bound=None), "Title", turn_session=SID)
    assert out["status"] == "error" and table.calls == []


def test_sanitizes_control_characters_and_whitespace():
    table = FakeTable()
    _call(_tool(table), "  Fix\nthe\tbuild\x00\u202e  \r\n now ")
    assert table.rows[session_title_key(SID)]["title"] == "Fix the build now"


def test_bounds_length_and_says_so():
    table = FakeTable()
    out = _call(_tool(table), "x" * 500)
    title = table.rows[session_title_key(SID)]["title"]
    assert len(title) == SESSION_TITLE_MAX_LEN and title.endswith("\u2026")
    assert "shortened" in _text(out)


def test_empty_title_clears():
    table = FakeTable()
    tool = _tool(table)
    _call(tool, "Something")
    out = _call(tool, " \n ")
    assert out["status"] == "success" and "cleared" in _text(out)
    assert "title" not in table.rows[session_title_key(SID)]


def test_non_string_title_is_an_error():
    table = FakeTable()
    out = _call(_tool(table), 42)
    assert out["status"] == "error" and table.calls == []


def test_keeps_a_person_set_title_unless_asked():
    table = FakeTable()
    table.rows[session_title_key(SID)] = {"task_id": session_title_key(SID), "title": "Mine",
                                          "updated_by": "portal:11111111-1111-4111-8111-111111111111"}
    tool = _tool(table)
    out = _call(tool, "Agent's idea")
    assert out["status"] == "error" and "person" in _text(out)
    assert table.rows[session_title_key(SID)]["title"] == "Mine"
    out = _call(tool, "Renamed on request", replace_user_title=True)
    assert out["status"] == "success"
    assert table.rows[session_title_key(SID)]["title"] == "Renamed on request"
    assert "ConditionExpression" not in table.calls[-1]


def test_guard_condition_names_its_attributes():
    table = FakeTable()
    _call(_tool(table), "Title")
    call = table.calls[-1]
    assert call["ConditionExpression"] == "attribute_not_exists(#title) OR NOT begins_with(#by, :person)"
    assert call["ExpressionAttributeNames"] == {"#title": "title", "#by": "updated_by"}


def test_replaces_its_own_title():
    table = FakeTable()
    tool = _tool(table)
    _call(tool, "First topic")
    assert _call(tool, "Second topic")["status"] == "success"
    assert table.rows[session_title_key(SID)]["title"] == "Second topic"


def test_write_failure_is_reported_not_raised():
    table = FakeTable()
    table.fail = RuntimeError("AccessDenied")
    out = _call(_tool(table), "Title")
    assert out["status"] == "error"


@pytest.fixture
def _no_mcp(monkeypatch):
    import strandly.mcp_clients as mcp_clients

    monkeypatch.setattr(mcp_clients, "build_mcp_clients", lambda config: [])


def _names(values: dict[str, str], session_id: str | None, **kw: Any) -> set[str]:
    tools = build_tools(Config(values=values), RuntimeContext(session_id=session_id), sandbox=None, **kw)
    return {str(getattr(t, "tool_name", "")) for t in tools}


@pytest.mark.usefixtures("_no_mcp")
def test_built_only_for_the_top_unscoped_agent_of_a_session_with_a_ledger():
    ledger = {"STRANDLY_RUN_LEDGER_TABLE": "strandly-test-runs"}
    assert SESSION_TITLE_TOOL_NAME in _names(ledger, SID)
    assert SESSION_TITLE_TOOL_NAME not in _names({}, SID)
    assert SESSION_TITLE_TOOL_NAME not in _names(ledger, None)
    assert SESSION_TITLE_TOOL_NAME not in _names(ledger, SID, spawn_depth=1)
    assert SESSION_TITLE_TOOL_NAME not in _names(ledger, SID, allowed_tools=frozenset({"read"}))


def test_prompt_guidance_only_with_the_tool():
    assert "`set_session_title`" in compose(tool_names=["shell", SESSION_TITLE_TOOL_NAME])
    assert "`set_session_title`" not in compose(tool_names=["shell"])


def test_a_title_set_in_a_persons_run_records_the_person():
    table = FakeTable()
    tool = _tool(table)
    event = {"triggerSource": "portal:chat", "portalUser": "11111111-1111-4111-8111-111111111111", "personGithub": "octocat", "personId": "octo"}
    state = {RUN_CONTEXT_STATE_KEY: RuntimeContext(session_id=SID, event=event)}
    assert tool(tool_context=SimpleNamespace(invocation_state=state), title="Release prep")["status"] == "success"
    item = table.calls[-1]["Item"]
    assert item["person"] == "github:octocat" and item["updated_by"] == "agent"
    _call(tool, "Other")
    assert "person" not in table.calls[-1]["Item"]
