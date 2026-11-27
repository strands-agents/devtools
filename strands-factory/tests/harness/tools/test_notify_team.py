"""``notify_team``: a home-channel title, the details in its thread, and the recorded thread link.

Hermetic: Slack is monkeypatched at the ``_slack_api`` seam and the run ledger is an in-memory fake.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from strandly.core.config import SlackSettings
from strandly.core.context import RUN_CONTEXT_STATE_KEY, RuntimeContext
from strandly.ops.slack import THREAD_LINK_KEY_PREFIX
from strandly.tools import slack as slack_mod
from strandly.tools.slack import make_notify_team

HOME = "C0HOME"


class FakeLedger:
    def __init__(self, error: Exception | None = None) -> None:
        self.puts: list[dict[str, Any]] = []
        self.error = error

    def put_item(self, **kwargs: Any) -> None:
        if self.error:
            raise self.error
        self.puts.append(kwargs)


@pytest.fixture
def calls(monkeypatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    def _fake(method: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
        captured.append({"method": method, **payload})
        return {"ok": True, "channel": payload.get("channel"), "ts": f"1712345678.{len(captured):06d}"}

    monkeypatch.setattr(slack_mod, "_slack_api", _fake)
    return captured


def _state(**event: Any) -> SimpleNamespace:
    ctx = RuntimeContext(session_id="gh-o-r-issue-1", event={"triggerSource": "github:@alice", **event})
    return SimpleNamespace(invocation_state={RUN_CONTEXT_STATE_KEY: ctx})


def _tool(ledger: FakeLedger | None = None, token: str = "xoxb-test"):
    return make_notify_team(
        SlackSettings(bot_token=token, home_channel=HOME, home_channel_enabled=True),
        home_channel=HOME,
        ledger_table=(lambda: ledger) if ledger is not None else None,
    )


def test_posts_a_title_then_the_details_in_its_thread_and_links_the_thread(calls):
    ledger = FakeLedger()
    result = _tool(ledger)(
        title="Nightly eval failed on main: needs a look",
        details="*What:* 3 of 40 tasks failed.\n<https://example.com/run/1|run>",
        tool_context=_state(),
    )
    assert result["status"] == "success"
    assert "thread_ts=1712345678.000001" in result["content"][0]["text"]
    title, details = calls
    assert title == {
        "method": "chat.postMessage", "channel": HOME,
        "text": "Nightly eval failed on main: needs a look", "unfurl_links": False,
    }
    assert details["thread_ts"] == "1712345678.000001" and details["text"].startswith("*What:*")
    (put,) = ledger.puts
    item = put["Item"]
    assert "ConditionExpression" not in put
    assert item["task_id"] == f"{THREAD_LINK_KEY_PREFIX}{HOME}#1712345678.000001"
    assert item["title"] == "Nightly eval failed on main: needs a look"
    assert item["details"].startswith("*What:* 3 of 40")
    assert item["origin_session_id"] == "gh-o-r-issue-1" and item["origin"] == "github:@alice"
    assert "gsi_pk" not in item  # never listed as a run


def test_follow_up_goes_into_the_existing_thread_without_a_new_top_level_post(calls):
    ledger = FakeLedger()
    result = _tool(ledger)(
        title="Eval re-run passed", details="All 40 green.", thread_ts="1712345678.000100",
        tool_context=_state(),
    )
    assert result["status"] == "success"
    (reply,) = calls
    assert reply["thread_ts"] == "1712345678.000100"
    assert reply["text"] == "*Eval re-run passed*\nAll 40 green."
    # Kept: an existing link (the original notification) is never overwritten.
    assert ledger.puts[0]["ConditionExpression"] == "attribute_not_exists(task_id)"


def test_title_is_one_line_and_clipped(calls):
    _tool()(title="  a\nmulti   line " + "x" * 400, details="d", tool_context=_state())
    assert "\n" not in calls[0]["text"] and len(calls[0]["text"]) <= 150


@pytest.mark.parametrize("title,details", [("", "d"), ("t", ""), ("   ", "  ")])
def test_title_and_details_are_required(calls, title, details):
    assert _tool()(title=title, details=details, tool_context=_state())["status"] == "error"
    assert calls == []


def test_rejects_a_malformed_thread_ts(calls):
    result = _tool()(title="t", details="d", thread_ts="../x", tool_context=_state())
    assert result["status"] == "error" and calls == []


def test_a_dm_run_cannot_post_to_the_team(calls):
    state = _state(slackChannelId="D0DM", slackUserId="U1")
    result = _tool()(title="t", details="d", tool_context=state)
    assert result["status"] == "error" and "DM" in result["content"][0]["text"]
    assert calls == []


def test_without_a_token_nothing_posts(calls):
    assert _tool(token="")(title="t", details="d", tool_context=_state())["status"] == "error"
    assert calls == []


def test_a_failed_link_write_does_not_fail_the_notification(calls):
    result = _tool(FakeLedger(RuntimeError("AccessDenied")))(title="t", details="d", tool_context=_state())
    assert result["status"] == "success" and len(calls) == 2


def test_no_ledger_still_posts(calls):
    assert _tool(None)(title="t", details="d", tool_context=_state())["status"] == "success"
    assert len(calls) == 2


def test_a_failed_title_posts_nothing_else(monkeypatch):
    seen: list[str] = []

    def _fail(method: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
        seen.append(method)
        return {"ok": False, "error": "not_in_channel"}

    monkeypatch.setattr(slack_mod, "_slack_api", _fail)
    ledger = FakeLedger()
    result = _tool(ledger)(title="t", details="d", tool_context=_state())
    assert result["status"] == "error" and "not_in_channel" in result["content"][0]["text"]
    assert seen == ["chat.postMessage"] and ledger.puts == []


def test_failed_details_point_at_the_posted_thread(monkeypatch):
    def _fake(method: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
        if payload.get("thread_ts"):
            return {"ok": False, "error": "ratelimited"}
        return {"ok": True, "ts": "1712345678.000001"}

    monkeypatch.setattr(slack_mod, "_slack_api", _fake)
    ledger = FakeLedger()
    result = _tool(ledger)(title="t", details="d", tool_context=_state())
    assert result["status"] == "error"
    assert 'thread_ts="1712345678.000001"' in result["content"][0]["text"]
    assert ledger.puts  # the link is written once the title lands, so replies still get context
