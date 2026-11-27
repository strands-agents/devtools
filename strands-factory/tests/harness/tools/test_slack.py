"""Hermetic unit tests for the ``use_slack`` tool — no network: everything is monkeypatched at the
``_slack_api`` seam (mirrors ``test_github_tool.py``'s approach to ``tools/github.py``)."""

from __future__ import annotations

import json
import urllib.error
from types import SimpleNamespace
from typing import Any

import pytest

from strandly.core.config import SlackSettings
from strandly.core.context import RUN_CONTEXT_STATE_KEY, RuntimeContext
from strandly.tools import slack as slack_mod
from strandly.tools.slack import make_use_slack


@pytest.fixture
def calls(monkeypatch) -> list[dict[str, Any]]:
    """Capture ``_slack_api`` calls; respond ``ok: true`` by default."""
    captured: list[dict[str, Any]] = []

    def _fake(method: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
        captured.append({"method": method, "payload": payload, "token": token})
        return {"ok": True, "channel": payload.get("channel"), "ts": "1712345678.000100"}

    monkeypatch.setattr(slack_mod, "_slack_api", _fake)
    return captured


def _tool(**kw: Any):
    settings = SlackSettings(bot_token=kw.pop("bot_token", "xoxb-test"), **kw)
    return make_use_slack(settings, default_channel_id="D0123")


# ---------------------------------------------------------------------------
# happy paths
# ---------------------------------------------------------------------------
def test_post_message_builds_payload_and_succeeds(calls):
    tool = _tool()
    result = tool(action="chat.postMessage", channel_id="D0123", text="hello *world*")
    assert result["status"] == "success"
    assert calls == [
        {
            "method": "chat.postMessage",
            "payload": {"channel": "D0123", "text": "hello *world*"},
            "token": "xoxb-test",
        }
    ]
    # The API response is surfaced to the model (it may need the ts for a follow-up reaction).
    assert json.loads(result["content"][0]["text"])["ts"] == "1712345678.000100"


def test_reaction_builds_payload(calls):
    tool = _tool()
    result = tool(
        action="reactions.add", channel_id="D0123", timestamp="1712345678.000200", name="eyes"
    )
    assert result["status"] == "success"
    assert calls[0]["method"] == "reactions.add"
    assert calls[0]["payload"] == {
        "channel": "D0123",
        "timestamp": "1712345678.000200",
        "name": "eyes",
    }


def test_thread_ts_included_when_given(calls):
    tool = make_use_slack(
        SlackSettings(bot_token="xoxb-test"),
        default_channel_id="D0123",
        default_thread_ts="1712345678.000300",
    )
    tool(action="chat.postMessage", channel_id="D0123", text="x", thread_ts="1712345678.000300")
    assert calls[0]["payload"]["thread_ts"] == "1712345678.000300"


# ---------------------------------------------------------------------------
# input validation (no network call may happen on a rejected input)
# ---------------------------------------------------------------------------
def test_missing_token_is_an_error_without_network(calls):
    tool = _tool(bot_token="")
    result = tool(action="chat.postMessage", channel_id="D0123", text="x")
    assert result["status"] == "error"
    assert "STRANDLY_SLACK_BOT_TOKEN" in result["content"][0]["text"]
    assert calls == []


@pytest.mark.parametrize(
    "bad_method",
    [
        "",
        "chat",  # no dot — not a Web API method
        "chat.postMessage?x=1",  # query-string smuggling
        "../admin",  # path traversal
        "chat.postMessage/extra",  # extra path segment
        "chat.postMessage#frag",
        "Chat.postMessage",  # groups are lowercase-led
    ],
)
def test_invalid_method_rejected_without_network(calls, bad_method):
    tool = _tool()
    result = tool(action=bad_method, channel_id="D0123", text="x")
    assert result["status"] == "error"
    assert calls == []


def test_missing_channel_rejected_without_network(calls):
    tool = _tool()
    result = tool(action="chat.postMessage", channel_id="", text="x")
    assert result["status"] == "error"
    assert calls == []


# ---------------------------------------------------------------------------
# error surfacing — Slack's ok:false and transport failures become error results, never raises
# ---------------------------------------------------------------------------
def test_slack_ok_false_is_an_error_result(monkeypatch):
    monkeypatch.setattr(
        slack_mod, "_slack_api", lambda m, p, t: {"ok": False, "error": "channel_not_found"}
    )
    result = _tool()(action="chat.postMessage", channel_id="D0123", text="x")
    assert result["status"] == "error"
    assert "channel_not_found" in result["content"][0]["text"]


@pytest.mark.parametrize(
    ("action", "error"),
    [("reactions.add", "already_reacted"), ("reactions.remove", "no_reaction")],
)
def test_reaction_already_in_requested_state_is_success(monkeypatch, action, error):
    # Prod: ~half of all use_slack errors were a repeat reaction — an idempotent no-op, not a failure.
    monkeypatch.setattr(slack_mod, "_slack_api", lambda m, p, t: {"ok": False, "error": error})
    result = _tool()(action=action, channel_id="D0123", timestamp="1.2", name="eyes")
    assert result["status"] == "success"
    assert error in result["content"][0]["text"]


def test_already_reacted_on_another_method_is_still_an_error(monkeypatch):
    monkeypatch.setattr(
        slack_mod, "_slack_api", lambda m, p, t: {"ok": False, "error": "already_reacted"}
    )
    result = _tool()(action="chat.postMessage", channel_id="D0123", text="x")
    assert result["status"] == "error"


def test_missing_scope_gets_the_dm_scope_hint(monkeypatch):
    monkeypatch.setattr(
        slack_mod, "_slack_api", lambda m, p, t: {"ok": False, "error": "missing_scope"}
    )
    result = _tool()(action="reactions.add", channel_id="D0123", timestamp="1.2", name="eyes")
    assert result["status"] == "error"
    assert "missing_scope" in result["content"][0]["text"]


def test_http_error_is_an_error_result(monkeypatch):
    def _raise(m, p, t):
        raise urllib.error.HTTPError("https://slack.com/api/x", 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(slack_mod, "_slack_api", _raise)
    result = _tool()(action="chat.postMessage", channel_id="D0123", text="x")
    assert result["status"] == "error"
    assert "429" in result["content"][0]["text"]


def test_url_error_is_an_error_result(monkeypatch):
    def _raise(m, p, t):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(slack_mod, "_slack_api", _raise)
    result = _tool()(action="chat.postMessage", channel_id="D0123", text="x")
    assert result["status"] == "error"
    assert "connection refused" in result["content"][0]["text"]


def test_unexpected_exception_is_an_error_result(monkeypatch):
    def _raise(m, p, t):
        raise RuntimeError("boom")

    monkeypatch.setattr(slack_mod, "_slack_api", _raise)
    result = _tool()(action="chat.postMessage", channel_id="D0123", text="x")
    assert result["status"] == "error"
    assert "boom" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# default thread: every Slack DM conversation is a thread (one session per thread)
# ---------------------------------------------------------------------------
def _threaded_tool():
    return make_use_slack(
        SlackSettings(bot_token="xoxb-test"),
        default_channel_id="D0123",
        default_thread_ts="1712345600.000100",
    )


def test_post_without_thread_ts_defaults_to_the_conversation_thread(calls):
    _threaded_tool()(action="chat.postMessage", channel_id="D0123", text="done")
    assert calls[0]["payload"] == {
        "channel": "D0123",
        "text": "done",
        "thread_ts": "1712345600.000100",
    }


def test_explicit_own_thread_ts_is_accepted(calls):
    result = _threaded_tool()(
        action="chat.postMessage", channel_id="D0123", text="x", thread_ts="1712345600.000100"
    )
    assert result["status"] == "success"
    assert calls[0]["payload"]["thread_ts"] == "1712345600.000100"


def test_default_thread_not_applied_to_reactions(calls):
    _threaded_tool()(
        action="reactions.add", channel_id="D0123", timestamp="1712345678.000100", name="eyes"
    )
    assert "thread_ts" not in calls[0]["payload"]


# ---------------------------------------------------------------------------
# home-channel restrictions
# ---------------------------------------------------------------------------
HOME = "C0HOME"


def _home_tool(*, channel: str | None = None, thread: str | None = None):
    return make_use_slack(
        SlackSettings(bot_token="xoxb-test", home_channel=HOME),
        default_channel_id=channel,
        default_thread_ts=thread,
        home_channel=HOME,
    )


def test_home_channel_allows_thread_posts_and_reactions_in_the_home_channel(calls):
    tool = _home_tool()
    result = tool(action="chat.postMessage", channel_id=HOME, text="PR opened", thread_ts="1.2")
    assert result["status"] == "success"
    assert (
        tool(action="reactions.add", channel_id=HOME, timestamp="1.2", name="eyes")["status"]
        == "success"
    )
    assert [c["method"] for c in calls] == ["chat.postMessage", "reactions.add"]
    assert calls[0]["payload"]["thread_ts"] == "1.2"


def test_home_channel_top_level_posts_go_through_notify_team(calls):
    result = _home_tool()(action="chat.postMessage", channel_id=HOME, text="PR opened")
    assert result["status"] == "error" and "notify_team" in result["content"][0]["text"]
    assert calls == []


def test_a_home_thread_run_replies_in_its_own_thread_by_default(calls):
    tool = _home_tool(channel=HOME, thread="1712345678.000100")
    assert tool(action="chat.postMessage", channel_id=HOME, text="on it")["status"] == "success"
    assert calls[0]["payload"]["thread_ts"] == "1712345678.000100"


@pytest.mark.parametrize("method", ["reactions.remove", "conversations.history", "chat.delete"])
def test_home_channel_refuses_other_methods(calls, method):
    result = _home_tool()(action=method, channel_id=HOME, timestamp="1.2", name="eyes")
    text = result["content"][0]["text"]
    assert result["status"] == "error" and ("only post/react" in text or "not allowed" in text)
    assert calls == []


def test_home_channel_refuses_other_channels_on_a_non_slack_run(calls):
    result = _home_tool()(action="chat.postMessage", channel_id="C0OTHER", text="hi")
    assert result["status"] == "error" and "enabled home channel" in result["content"][0]["text"]
    assert calls == []


def test_home_channel_allows_the_runs_own_thread(calls):
    tool = _home_tool(channel="D0DM", thread="1712345678.000100")
    assert tool(action="chat.postMessage", channel_id="D0DM", text="done")["status"] == "success"
    assert calls[0]["payload"]["thread_ts"] == "1712345678.000100"
    assert (
        tool(action="reactions.add", channel_id="D0DM", timestamp="1712345678.000200", name="x")[
            "status"
        ]
        == "success"
    )


def test_enabled_home_channel_keeps_a_dm_run_in_its_own_thread(calls):
    tool = _home_tool(channel="D0DM", thread="1712345678.000100")
    result = tool(action="chat.postMessage", channel_id="D0DM", text="x", thread_ts="1.9")
    assert result["status"] == "error" and "own thread" in result["content"][0]["text"]
    assert calls == []


def test_without_a_home_channel_a_dm_run_may_remove_its_own_reactions(calls):
    tool = make_use_slack(SlackSettings(bot_token="xoxb-test"), default_channel_id="D0DM")
    assert tool(action="reactions.remove", channel_id="D0DM", timestamp="1.2", name="x")[
        "status"
    ] == "success"


# ---------------------------------------------------------------------------
# DM confinement with the home channel off (the default): a DM run reaches only its own DM thread
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("other", ["D0OTHER", "U0SOMEONE", "C0CHANNEL"])
def test_dm_run_refuses_other_destinations(calls, other):
    result = _threaded_tool()(action="chat.postMessage", channel_id=other, text="hi")
    assert result["status"] == "error" and "own DM" in result["content"][0]["text"]
    assert calls == []


def test_dm_run_refuses_a_foreign_thread(calls):
    result = _threaded_tool()(
        action="chat.postMessage", channel_id="D0123", text="x", thread_ts="1712345699.000900"
    )
    assert result["status"] == "error" and "own thread" in result["content"][0]["text"]
    assert calls == []


@pytest.mark.parametrize(
    "method", ["conversations.history", "conversations.open", "chat.delete", "users.list", "files.upload"]
)
def test_methods_outside_the_allow_list_are_refused(calls, method):
    result = _threaded_tool()(action=method, channel_id="D0123", text="x")
    assert result["status"] == "error" and "not allowed" in result["content"][0]["text"]
    assert calls == []


def test_dm_run_without_a_channel_fails_closed(calls):
    tool = make_use_slack(SlackSettings(bot_token="xoxb-test"))
    result = tool(action="chat.postMessage", channel_id="D0123", text="x")
    assert result["status"] == "error" and "no Slack DM" in result["content"][0]["text"]
    assert calls == []


def test_success_result_is_trimmed(monkeypatch):
    monkeypatch.setattr(
        slack_mod,
        "_slack_api",
        lambda m, p, t: {"ok": True, "channel": "D0123", "ts": "1.2", "message": {"text": "secret"}},
    )
    result = _tool()(action="chat.postMessage", channel_id="D0123", text="x")
    assert json.loads(result["content"][0]["text"]) == {"ok": True, "channel": "D0123", "ts": "1.2"}


# ---------------------------------------------------------------------------
# review follow-ups: id spelling, reactions, missing threads, home-mode DMs, scheduled DMs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("spelled", ["d0123", " D0123", "D0123 ", "D0123\n", "D01234", "<#D0123>"])
def test_dm_run_compares_the_channel_id_exactly(calls, spelled):
    result = _threaded_tool()(action="chat.postMessage", channel_id=spelled, text="hi")
    assert result["status"] == "error"
    assert calls == []


@pytest.mark.parametrize("other", ["D0OTHER", "C0CHANNEL", "U0SOMEONE"])
@pytest.mark.parametrize("method", ["reactions.add", "reactions.remove"])
def test_dm_run_refuses_reactions_outside_its_dm(calls, method, other):
    result = _threaded_tool()(action=method, channel_id=other, timestamp="1.2", name="eyes")
    assert result["status"] == "error" and "own DM" in result["content"][0]["text"]
    assert calls == []


def test_dm_run_with_no_recorded_thread_refuses_an_explicit_thread(calls):
    tool = make_use_slack(SlackSettings(bot_token="xoxb-test"), default_channel_id="D0123")
    result = tool(action="chat.postMessage", channel_id="D0123", text="x", thread_ts="1.9")
    assert result["status"] == "error" and "own thread" in result["content"][0]["text"]
    assert tool(action="chat.postMessage", channel_id="D0123", text="x")["status"] == "success"
    assert calls[0]["payload"] == {"channel": "D0123", "text": "x"}


@pytest.mark.parametrize("other", ["D0OTHER", "U0SOMEONE", HOME, "C0OTHER"])
def test_home_mode_personal_dm_run_stays_in_its_dm(calls, other):
    tool = make_use_slack(
        SlackSettings(bot_token="xoxb-test", home_channel=HOME),
        default_channel_id="D0DM",
        default_thread_ts="1712345678.000100",
        home_channel=HOME,
        private_dm=True,
    )
    result = tool(action="chat.postMessage", channel_id=other, text="hi", thread_ts="1.2")
    assert result["status"] == "error" and "own DM" in result["content"][0]["text"]
    assert tool(action="chat.postMessage", channel_id="D0DM", text="ok")["status"] == "success"
    assert [c["payload"]["channel"] for c in calls] == ["D0DM"]


def test_home_mode_non_dm_run_cannot_reach_a_dm(calls):
    tool = _home_tool(channel=HOME, thread="1712345678.000100")
    for other in ("D0OTHER", "U0SOMEONE"):
        result = tool(action="chat.postMessage", channel_id=other, text="hi")
        assert result["status"] == "error"
    assert calls == []


def _scheduled_state(**event: Any) -> SimpleNamespace:
    ctx = RuntimeContext(
        session_id="sched-abc", event={"triggerSource": "scheduled:t1", **event}
    )
    return SimpleNamespace(invocation_state={RUN_CONTEXT_STATE_KEY: ctx})


def test_scheduled_run_never_posts_to_a_dm_its_payload_names(calls):
    tool = make_use_slack(
        SlackSettings(bot_token="xoxb-test"),
        default_channel_id="D0DM",
        default_thread_ts="1712345678.000100",
        pin_destination=True,
    )
    state = _scheduled_state(slackChannelId="D0DM", slackThreadTs="1712345678.000100")
    for kwargs in (
        {"channel_id": "D0DM"},
        {"channel_id": "D0DM", "thread_ts": "1712345678.000100"},
        {"channel_id": "D0OTHER"},
        {"channel_id": "U0SOMEONE"},
    ):
        result = tool(action="chat.postMessage", text="x", tool_context=state, **kwargs)
        assert result["status"] == "error"
    assert calls == []


def test_scheduled_run_with_a_dm_payload_cannot_reach_it_in_home_mode(calls):
    tool = make_use_slack(
        SlackSettings(bot_token="xoxb-test", home_channel_enabled=True),
        default_channel_id=None,
        pin_destination=True,
        home_channel="C0HOME",
    )
    state = _scheduled_state(slackChannelId="D0DM", slackThreadTs="1712345678.000100")
    for channel in ("D0DM", "D0OTHER", "C0OTHER"):
        assert tool(action="chat.postMessage", channel_id=channel, text="x", tool_context=state)["status"] == "error"
    assert calls == []


def test_scheduled_run_without_a_stored_destination_fails_closed(calls):
    tool = make_use_slack(
        SlackSettings(bot_token="xoxb-test"), default_channel_id="D0DM", pin_destination=True
    )
    result = tool(
        action="chat.postMessage", channel_id="D0DM", text="x", tool_context=_scheduled_state()
    )
    assert result["status"] == "error"
    assert calls == []
