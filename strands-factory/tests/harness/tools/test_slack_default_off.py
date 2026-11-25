"""The operator switch is independent of credentials and destination configuration."""
from __future__ import annotations

import urllib.error

import pytest

from strandly.core.config import Config
from strandly.core.context import RuntimeContext
from strandly.ops.lambdas import slack_alarms
from strandly.ops.slack import slack_error_message
from strandly.tools import slack, toolset

FLAG = "STRANDLY_SLACK_HOME_CHANNEL_ENABLED"
TOKEN = "STRANDLY_SLACK_BOT_TOKEN"
CHANNEL = "STRANDLY_SLACK_HOME_CHANNEL"


@pytest.mark.parametrize("flag", [None, "", "false", "0", "yes", "invalid"])
@pytest.mark.parametrize("token", ["", "xoxb-test"])
@pytest.mark.parametrize("channel", ["", "C0HOME", "G0HOME", "D0DM", "#name"])
def test_disabled_matrix_preserves_only_dm_reply_tools(monkeypatch, flag, token, channel):
    values = {TOKEN: token, CHANNEL: channel}
    if flag is not None:
        values[FLAG] = flag
    cfg = Config(values=values)
    assert cfg.slack_home_channel is None
    assert cfg.slack_enabled is bool(token)
    monkeypatch.setattr("strandly.mcp_clients.build_mcp_clients", lambda _: [])
    for target in [None, "D0DM", "C0HOME", "C0OTHER"]:
        tools = toolset.build_tools(cfg, RuntimeContext(event={"slackChannelId": target}), sandbox=None, spawn_depth=1)
        names = {getattr(t, "tool_name", None) for t in tools}
        assert ("use_slack" in names) is (bool(token) and target == "D0DM")


@pytest.mark.parametrize("flag", [None, "false", "invalid"])
@pytest.mark.parametrize("token", ["", "xoxb-test"])
@pytest.mark.parametrize("channel", ["", "C0HOME"])
def test_disabled_notifier_never_reads_secret_or_posts(monkeypatch, flag, token, channel):
    if flag is None:
        monkeypatch.delenv(FLAG, raising=False)
    else:
        monkeypatch.setenv(FLAG, flag)
    def forbidden(*a, **kw):
        pytest.fail("disabled notifier performed I/O")
    monkeypatch.setattr(slack_alarms, "load_secrets", forbidden)
    monkeypatch.setattr(slack_alarms, "_slack_post", forbidden)
    monkeypatch.setenv(TOKEN, token)
    monkeypatch.setenv(CHANNEL, channel)
    assert slack_alarms.lambda_handler({})["status"] == "disabled"


def test_missing_scope_sanitized_and_no_tool_retry(monkeypatch):
    calls = []
    def denied(*a):
        calls.append(a)
        return {"ok": False, "error": "missing_scope", "needed": "chat:write,xoxb-secret,C0SECRET", "provided": "secret"}
    monkeypatch.setattr(slack, "_slack_api", denied)
    tool = slack.make_use_slack(Config(values={TOKEN: "test"}).slack, home_channel="C0HOME")
    for _ in range(3):
        result = tool(action="chat.postMessage", channel_id="C0HOME", text="hi", thread_ts="1.2")
        assert result["status"] == "error"
        message = result["content"][0]["text"]
        assert "missing_scope" in message and "chat:write" in message and "reinstall" in message
        assert "secret" not in message and "C0SECRET" not in message
    assert len(calls) == 1


def test_missing_scope_notifier_stops_batch_and_reports_failure(monkeypatch, caplog):
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setattr(slack_alarms, "load_secrets", lambda: {"bot_token": "test", "home_channel": "C0HOME"})
    calls = []
    def denied(*a):
        calls.append(a)
        return {"ok": False, "error": "missing_scope", "needed": "chat:write,xoxb-secret"}
    monkeypatch.setattr(slack_alarms, "_slack_post", denied)
    event = {"Records": [{"EventSource": "aws:sns", "Sns": {"Message": "report"}}] * 3}
    result = slack_alarms.lambda_handler(event)
    assert result == {"status": "failed", "posted": 0, "failed": 1, "skipped": 2}
    assert len(calls) == 1
    assert "missing_scope" in caplog.text and "chat:write" in caplog.text and "secret" not in caplog.text


@pytest.mark.parametrize("code,retry", [(400, False), (401, False), (403, False), (404, False), (429, True), (500, True), (503, True)])
def test_alarm_http_retry_classification(monkeypatch, caplog, code, retry):
    def fail(*a):
        raise urllib.error.HTTPError("https://slack.com/api", code, "xoxb-secret", {}, None)
    monkeypatch.setattr(slack_alarms, "_slack_post", fail)
    if retry:
        with pytest.raises(slack_alarms.TransientSlackError, match=str(code)):
            slack_alarms.post("test", "C0HOME", "test")
    else:
        assert slack_alarms.post("test", "C0HOME", "test") is None
    assert "xoxb-secret" not in caplog.text


def test_untrusted_error_fields_not_echoed():
    assert "C0SECRET" not in slack_error_message({"error": "C0SECRET"})


@pytest.mark.parametrize("destination", ["C0OTHER", "D0OTHER"])
def test_shared_run_cannot_post_elsewhere(monkeypatch, destination):
    monkeypatch.setattr(slack, "_slack_api", lambda *a: pytest.fail("widened destination"))
    tool = slack.make_use_slack(Config(values={TOKEN: "test"}).slack, home_channel="C0HOME", default_channel_id="C0HOME")
    assert tool(action="chat.postMessage", channel_id=destination, text="hi")["status"] == "error"


def test_enabled_personal_dm_cannot_send_memory_to_home(monkeypatch):
    calls = []
    monkeypatch.setattr(slack, "_slack_api", lambda *a: calls.append(a) or {"ok": True})
    tool = slack.make_use_slack(Config(values={TOKEN: "test"}).slack, home_channel="C0HOME",
                                default_channel_id="D0DM", private_dm=True)
    assert tool(action="chat.postMessage", channel_id="C0HOME", text="personal")["status"] == "error"
    assert not calls
    assert tool(action="reactions.remove", channel_id="D0DM", name="eyes", timestamp="1.2")["status"] == "success"


@pytest.mark.parametrize("home", [None, "C0HOME"])
def test_cached_scheduled_context_cannot_authorize_another_channel(monkeypatch, home):
    from types import SimpleNamespace

    from strandly.core.context import RUN_CONTEXT_STATE_KEY

    monkeypatch.setattr(slack, "_slack_api", lambda *a: pytest.fail("unapproved shared destination"))
    tool = slack.make_use_slack(Config(values={TOKEN: "test"}).slack,
                               default_channel_id="D0OLD", home_channel=home)
    current = RuntimeContext(session_id="sched-job", event={
        "triggerSource": "scheduled:job", "slackChannelId": "C0OTHER", "slackThreadTs": "1.2",
    })
    state = SimpleNamespace(invocation_state={RUN_CONTEXT_STATE_KEY: current})
    assert tool(action="chat.postMessage", channel_id="C0OTHER", text="no", tool_context=state)["status"] == "error"


def test_scheduled_home_is_the_only_additional_destination(monkeypatch):
    calls = []
    monkeypatch.setattr(slack, "_slack_api", lambda *a: calls.append(a) or {"ok": True})
    tool = slack.make_use_slack(Config(values={TOKEN: "test"}).slack,
                               default_channel_id="D0ORIGIN", default_thread_ts="1.2",
                               pin_destination=True, home_channel="C0HOME")
    # A scheduled run never posts to a DM, even the one it was built with.
    for destination, ok in [("C0HOME", True), ("D0ORIGIN", False), ("D0OTHER", False), ("C0OTHER", False)]:
        thread = "1.3" if destination == "C0HOME" else ""
        result = tool(action="chat.postMessage", channel_id=destination, text="summary", thread_ts=thread)
        assert (result["status"] == "success") is ok
    assert len(calls) == 1
    # A top-level home post is a notification, which only notify_team makes.
    result = tool(action="chat.postMessage", channel_id="C0HOME", text="summary")
    assert result["status"] == "error" and "notify_team" in result["content"][0]["text"]
    assert len(calls) == 1


@pytest.mark.parametrize("channel", ["", "D0DM", "#not-an-id"])
def test_enabled_missing_or_invalid_home_does_not_broaden_dm_destinations(monkeypatch, channel):
    calls = []
    monkeypatch.setattr(slack, "_slack_api", lambda *a: calls.append(a) or {"ok": True})
    config = Config(values={TOKEN: "test", FLAG: "true", CHANNEL: channel})
    assert config.slack_home_channel is None
    tool = slack.make_use_slack(config.slack, default_channel_id="D0DM", home_channel=config.slack_home_channel)
    for target in ["C0HOME", "C0OTHER", "D0OTHER"]:
        assert tool(action="chat.postMessage", channel_id=target, text="no")["status"] == "error"
    assert calls == []
    assert tool(action="reactions.remove", channel_id="D0DM", timestamp="1.2", name="eyes")["status"] == "success"


@pytest.mark.parametrize(
    ("session", "event"),
    [
        ("sched-daily", {"triggerSource": "scheduled:daily", "slackChannelId": "D0DM", "slackThreadTs": "1.2"}),
        ("sched-daily", {"slackChannelId": "D0DM", "slackUserId": "U0BOB"}),
        ("s1", {"triggerSource": "scheduled:daily", "slackChannelId": "D0DM"}),
    ],
)
def test_a_scheduled_run_never_gets_a_dm_to_reply_in(monkeypatch, session, event):
    from types import SimpleNamespace

    from strandly.core.context import RUN_CONTEXT_STATE_KEY

    monkeypatch.setattr("strandly.mcp_clients.build_mcp_clients", lambda _: [])
    calls = []
    monkeypatch.setattr(slack, "_slack_api", lambda *a: calls.append(a) or {"ok": True})
    ctx = RuntimeContext(event=event, session_id=session)
    # Home channel off: the DM payload gives the scheduled run no use_slack at all.
    off = toolset.build_tools(Config(values={TOKEN: "test"}), ctx, sandbox=None, spawn_depth=1)
    assert "use_slack" not in {getattr(t, "tool_name", None) for t in off}
    # Home channel on: use_slack exists for the home channel, never for the DM.
    on = toolset.build_tools(
        Config(values={TOKEN: "test", FLAG: "true", CHANNEL: "C0HOME"}), ctx, sandbox=None, spawn_depth=1
    )
    tool = next(t for t in on if getattr(t, "tool_name", None) == "use_slack")
    state = SimpleNamespace(invocation_state={RUN_CONTEXT_STATE_KEY: ctx})
    for kwargs in ({}, {"tool_context": state}):
        assert tool(action="chat.postMessage", channel_id="D0DM", text="x", **kwargs)["status"] == "error"
    assert calls == []
