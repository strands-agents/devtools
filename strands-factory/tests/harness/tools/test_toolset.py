"""`build_tools` gating — which tools an invocation actually gets.

The Slack cases are the point of this file: without a home channel `use_slack` is a *reply path*
for the Slack DM ingress, so it must appear ONLY on runs that came from Slack; with a home channel
configured every run gets it (restricted inside the tool, see test_slack.py).
"""

from __future__ import annotations

from typing import Any

import pytest

from strandly.core.config import Config
from strandly.core.context import RuntimeContext
from strandly.tools.toolset import build_tools

SLACK_CFG = {"STRANDLY_SLACK_BOT_TOKEN": "xoxb-1"}


@pytest.fixture(autouse=True)
def _no_mcp(monkeypatch):
    """MCP clients would spawn/connect real servers — not what this file is about."""
    import strandly.mcp_clients as mcp_clients

    monkeypatch.setattr(mcp_clients, "build_mcp_clients", lambda config: [])


def _names(tools: list[Any]) -> set[str]:
    out: set[str] = set()
    for t in tools:
        name = t if isinstance(t, str) else getattr(t, "tool_name", None) or getattr(t, "__name__", "")
        out.add(str(name).rsplit(".", 1)[-1])
    return out


def _tools(values: dict[str, str], event: dict[str, Any] | None) -> set[str]:
    return _names(
        build_tools(
            Config(values=values),
            RuntimeContext(event=event),
            sandbox=None,
            spawn_depth=1,  # no subagent tool
        )
    )


def test_use_slack_present_only_on_a_slack_triggered_run():
    tools = _tools(SLACK_CFG, {"slackChannelId": "D123", "slackUserId": "U1"})
    assert "use_slack" in tools


@pytest.mark.parametrize(
    "event",
    [
        None,  # CLI / MCP / scheduled job — no invoke payload at all
        {},  # a payload with nothing Slack about it
        {"prompt": "review PR 1", "event_name": "issue_comment"},  # GitHub mention dispatch
        {"slackChannelId": ""},  # present but empty → not a reply target
        {"slackChannelId": "   "},  # whitespace only
        {"slackChannelId": None},
        {"slackChannelId": 123},  # non-string → not trusted as a channel
    ],
)
def test_use_slack_absent_when_the_run_is_not_from_slack(event):
    # The token IS configured in every one of these — availability alone must not inject the tool.
    assert "use_slack" not in _tools(SLACK_CFG, event)


def test_use_slack_absent_without_a_token_even_on_a_slack_run():
    assert "use_slack" not in _tools({}, {"slackChannelId": "D123"})


def test_slack_gate_does_not_affect_use_github():
    """`use_github` stays token-gated only — it is a general capability, not a reply path."""
    event = None
    with_token = _tools({"STRANDLY_GITHUB_TOKEN": "ghp_x"}, event)
    assert "use_github" in with_token
    assert "use_slack" not in with_token
    assert "use_github" not in _tools({}, event)


HOME_CFG = {**SLACK_CFG, "STRANDLY_SLACK_HOME_CHANNEL": "C0HOME", "STRANDLY_SLACK_HOME_CHANNEL_ENABLED": "true"}


@pytest.mark.parametrize("event", [None, {}, {"triggerSource": "scheduled:x"}])
def test_use_slack_on_every_run_with_a_home_channel(event):
    assert "use_slack" in _tools(HOME_CFG, event)


def test_home_channel_needs_the_bot_token():
    assert "use_slack" not in _tools({"STRANDLY_SLACK_HOME_CHANNEL": "C0HOME", "STRANDLY_SLACK_HOME_CHANNEL_ENABLED": "true"}, None)


@pytest.mark.parametrize("bad", ["", "D0123", "general", "c0home", "#strandly"])
def test_malformed_home_channel_is_off(bad):
    assert "use_slack" not in _tools({**SLACK_CFG, "STRANDLY_SLACK_HOME_CHANNEL": bad}, None)


def test_home_channel_tool_can_be_scoped_away():
    tools = build_tools(
        Config(values=HOME_CFG),
        RuntimeContext(event=None),
        sandbox=None,
        spawn_depth=1,
        allowed_tools=frozenset({"use_github"}),
    )
    assert "use_slack" not in _names(tools)


def _top_tools(values: dict[str, str], event: dict[str, Any] | None) -> set[str]:
    """The top agent's tools (depth 0), where ``notify_team`` lives."""
    return _names(build_tools(Config(values=values), RuntimeContext(event=event), sandbox=None))


@pytest.mark.parametrize(
    "event",
    [None, {"triggerSource": "scheduled:x"}, {"slackChannelId": "C0HOME", "slackUserId": "U1"}],
)
def test_notify_team_on_non_dm_runs_with_the_home_channel(event):
    assert "notify_team" in _top_tools(HOME_CFG, event)


@pytest.mark.parametrize(
    "values",
    [
        SLACK_CFG,
        {**SLACK_CFG, "STRANDLY_SLACK_HOME_CHANNEL": "C0HOME"},  # no operator switch
        {**SLACK_CFG, "STRANDLY_SLACK_HOME_CHANNEL_ENABLED": "true"},  # no channel id
        {"STRANDLY_SLACK_HOME_CHANNEL": "C0HOME", "STRANDLY_SLACK_HOME_CHANNEL_ENABLED": "true"},  # no token
    ],
)
def test_notify_team_is_off_unless_switch_channel_and_token_are_all_set(values):
    assert "notify_team" not in _top_tools(values, None)


@pytest.mark.parametrize("channel", ["D0DM", "D0SCHEDULED"])
def test_dm_runs_never_get_notify_team(channel):
    assert "notify_team" not in _top_tools(HOME_CFG, {"slackChannelId": channel, "slackUserId": "U1"})
    assert "notify_team" not in _top_tools(HOME_CFG, {"slackChannelId": channel})


def test_subagents_never_get_notify_team():
    assert "notify_team" not in _tools(HOME_CFG, None)
