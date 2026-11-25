"""`RuntimeContext` — the per-invocation facts the harness gates on."""

from __future__ import annotations

import pytest

from strandly.core.context import RuntimeContext


def test_slack_channel_id_from_ingress_payload():
    ctx = RuntimeContext(
        event={
            "prompt": "hi",
            "slackTeamId": "T1",
            "slackChannelId": "D123",
            "slackUserId": "U1",
        }
    )
    assert ctx.slack_channel_id == "D123"
    # Surrounding whitespace is not part of the channel id.
    assert RuntimeContext(event={"slackChannelId": " D123 "}).slack_channel_id == "D123"


@pytest.mark.parametrize(
    "event",
    [
        None,
        {},
        {"prompt": "x", "event_name": "issue_comment"},  # a GitHub dispatch
        {"slackChannelId": ""},
        {"slackChannelId": "  "},
        {"slackChannelId": None},
        {"slackChannelId": 123},  # non-string is not a channel id
        {"slackChannelId": ["D123"]},
        "not-a-dict",  # a serving adapter handing over something unexpected must not raise
    ],
)
def test_slack_channel_id_is_none_when_not_a_slack_run(event):
    assert RuntimeContext(event=event).slack_channel_id is None


def test_slack_user_id_from_ingress_payload():
    # The per-person identity the long-term-memory personal namespace is derived from.
    ctx = RuntimeContext(event={"slackChannelId": "D123", "slackUserId": "U0123ABC"})
    assert ctx.slack_user_id == "U0123ABC"
    # Enterprise-grid ids start with W; surrounding whitespace is not part of the id.
    assert RuntimeContext(event={"slackUserId": " W0123ABC "}).slack_user_id == "W0123ABC"


@pytest.mark.parametrize(
    "event",
    [
        None,
        {},
        {"prompt": "x", "event_name": "issue_comment"},  # a GitHub dispatch — no Slack user
        {"slackUserId": ""},
        {"slackUserId": "   "},
        {"slackUserId": None},
        {"slackUserId": 123},  # non-string
        {"slackUserId": ["U1"]},
        {"slackUserId": "u0123abc"},  # lowercase is not a Slack id (they are case-sensitive)
        {"slackUserId": "X0123ABC"},  # wrong prefix
        {"slackUserId": "U"},  # prefix only
        {"slackUserId": "U0123-ABC"},  # punctuation
        {"slackUserId": "U" * 40},  # implausibly long
        # Adversarial: the value becomes a Bedrock retrieval-filter value stamped on every KB
        # write, so anything that isn't a plain Slack id must be rejected outright.
        {"slackUserId": "U1 OR namespace = shared"},
        {"slackUserId": "../shared"},
        {"slackUserId": "U1\nU2"},
        "not-a-dict",
    ],
)
def test_slack_user_id_is_none_unless_a_real_slack_id(event):
    assert RuntimeContext(event=event).slack_user_id is None


def test_slack_user_id_does_not_gate_use_slack():
    # The two Slack fields are independent: the reply-path gate is the channel id, the memory
    # namespace is the user id. A payload with only one must not imply the other.
    assert RuntimeContext(event={"slackUserId": "U0123ABC"}).slack_channel_id is None
    assert RuntimeContext(event={"slackChannelId": "D123"}).slack_user_id is None


# --- is_slack_dm: "is this run 1:1 with one person?" ---------------------------------------------
#
# The precondition for deriving anything per-person from a Slack run. A Slack session id is
# CHANNEL-scoped (``slack-<team>-<channel>``) and the serving process caches one agent per session,
# so only for an IM channel (``D…``) is "one session" also "one person". The ingress dispatches for
# ``channel_type == "im"`` only today; this is the guard for the day a channel/thread surface lands.


@pytest.mark.parametrize(
    "event",
    [
        {"slackChannelId": "D123", "slackUserId": "U0123ABC"},
        {"slackChannelId": " D123 ", "slackUserId": " U0123ABC "},  # ingress whitespace
        {"slackChannelId": "D0000000000", "slackUserId": "W0123ABC"},  # W = Enterprise Grid user
    ],
)
def test_is_slack_dm_true_for_an_im_channel_with_a_valid_user(event):
    assert RuntimeContext(event=event).is_slack_dm is True


@pytest.mark.parametrize(
    "event",
    [
        {"slackChannelId": "C123", "slackUserId": "U0123ABC"},  # public/private channel
        {"slackChannelId": "G123", "slackUserId": "U0123ABC"},  # group DM / legacy private
        {"slackChannelId": "d123", "slackUserId": "U0123ABC"},  # ids are case-sensitive
        {"slackChannelId": "D123"},  # DM, but no user id came along
        {"slackChannelId": "D123", "slackUserId": "nope"},  # …or a junk one
        {"slackUserId": "U0123ABC"},  # a user id with no channel is not a DM
        {"event_name": "issue_comment"},  # not Slack at all
        {},
        None,
        "not-a-dict",
    ],
)
def test_is_slack_dm_false_for_everything_else(event):
    assert RuntimeContext(event=event).is_slack_dm is False


def test_invocation_context_from_invoke_payload():
    # The maintainer portal stamping "what the user is viewing" onto a chat turn.
    context = {"surface": "maintainer-portal", "viewing": {"type": "issue", "number": 123}}
    ctx = RuntimeContext(event={"prompt": "hi", "invocationContext": context})
    assert ctx.invocation_context == context


@pytest.mark.parametrize(
    "event",
    [
        None,
        {},
        {"prompt": "x"},  # absent
        {"invocationContext": None},
        {"invocationContext": "viewing issue 5"},  # non-dict shapes are rejected wholesale
        {"invocationContext": 42},
        {"invocationContext": ["o/r#5"]},
        {"invocationContext": {}},  # empty dict says nothing — treated as absent
        "not-a-dict",  # a serving adapter handing over something unexpected must not raise
    ],
)
def test_invocation_context_is_none_for_junk_shapes(event):
    assert RuntimeContext(event=event).invocation_context is None


def test_slack_thread_ts_from_ingress_payload():
    ctx = RuntimeContext(event={"slackChannelId": "D123", "slackThreadTs": " 1791234567.123456 "})
    assert ctx.slack_thread_ts == "1791234567.123456"


@pytest.mark.parametrize(
    "event",
    [
        None,
        {},
        {"slackThreadTs": ""},
        {"slackThreadTs": 1791234567.123456},
        {"slackThreadTs": "1791234567-123456"},  # the Memory-safe session form, not a Slack ts
        {"slackThreadTs": "not-a-ts"},
    ],
)
def test_slack_thread_ts_is_none_unless_ts_shaped(event):
    assert RuntimeContext(event=event).slack_thread_ts is None
