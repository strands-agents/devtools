"""Memory actors, namespaces and the AgentCore session-id mapping (``memory/scopes.py``)."""

from __future__ import annotations

import re

import botocore.session
import pytest

from strandly.core.constants import (
    MEMORY_FACTS_NAMESPACE_TEMPLATE,
    MEMORY_PREFERENCES_NAMESPACE_TEMPLATE,
)
from strandly.core.context import RuntimeContext
from strandly.memory.scopes import (
    TEAM_ACTOR,
    memory_actor,
    memory_session_id,
    personal_actor,
)

_MODEL = botocore.session.get_session().get_service_model("bedrock-agentcore")


def _api_pattern(shape: str) -> re.Pattern[str]:
    return re.compile(f"^(?:{_MODEL.shape_for(shape).metadata['pattern']})$")


def facts_namespace(actor: str) -> str:
    return MEMORY_FACTS_NAMESPACE_TEMPLATE.replace("{actorId}", actor)


def preferences_namespace(actor: str) -> str:
    return MEMORY_PREFERENCES_NAMESPACE_TEMPLATE.replace("{actorId}", actor)


def _dm(user: str = "U0123ABC") -> RuntimeContext:
    return RuntimeContext(event={"slackChannelId": "D123", "slackUserId": user, "triggerSource": f"slack:{user}"})


def test_a_slack_dm_is_its_person_and_everything_else_is_the_team():
    assert memory_actor(_dm()) == "slack-user-U0123ABC"
    assert personal_actor(_dm()) == "slack-user-U0123ABC"
    team_runs = [
        None,
        RuntimeContext(),  # CLI / portal
        RuntimeContext(event={"event_name": "issue_comment"}),  # GitHub
        RuntimeContext(event={"slackChannelId": "C123", "slackUserId": "U0123ABC"}),  # channel
        RuntimeContext(event={"slackChannelId": "G123", "slackUserId": "U0123ABC"}),  # group DM
        RuntimeContext(event={"slackChannelId": "D123"}),  # DM without a user id
        RuntimeContext(event={"slackChannelId": "D123", "slackUserId": "nope"}),  # junk id
        RuntimeContext(event={"slackChannelId": "D123", "slackUserId": "U0123ABC"}),  # no ingress
        RuntimeContext(  # another surface naming a DM
            event={"slackChannelId": "D123", "slackUserId": "U0123ABC", "triggerSource": "acp:prompt"}
        ),
        RuntimeContext(  # the trigger names a different Slack user
            event={"slackChannelId": "D123", "slackUserId": "U0123ABC", "triggerSource": "slack:U0OTHER"}
        ),
        RuntimeContext(  # a scheduled job created in a DM notifies that DM
            event={"slackChannelId": "D123", "triggerSource": "scheduled:daily"}, session_id="sched-daily"
        ),
        RuntimeContext(
            event={"slackChannelId": "D123", "slackUserId": "U0123ABC", "triggerSource": "scheduled:daily"}
        ),
    ]
    for ctx in team_runs:
        assert memory_actor(ctx) == TEAM_ACTOR == "team"
        assert personal_actor(ctx) is None


def test_two_people_never_share_a_scope():
    a, b = memory_actor(_dm("U0AAA")), memory_actor(_dm("U0BBB"))
    assert a != b
    assert facts_namespace(a) != facts_namespace(b)
    assert preferences_namespace(a) != preferences_namespace(b)
    assert TEAM_ACTOR not in (a, b)


def test_every_actor_and_namespace_passes_the_api_patterns():
    actor_ok, namespace_ok = _api_pattern("ActorId"), _api_pattern("Namespace")
    for actor in (TEAM_ACTOR, memory_actor(_dm("U0123ABC")), memory_actor(_dm("W" + "Z" * 31))):
        assert actor_ok.match(actor), actor
        assert namespace_ok.match(facts_namespace(actor))
        assert namespace_ok.match(preferences_namespace(actor))
    assert facts_namespace("team") == "/facts/team/"
    assert preferences_namespace("slack-user-U1") == "/preferences/slack-user-U1/"


@pytest.mark.parametrize(
    "raw",
    [
        "gh-strands-agents-sdk-python-123",
        "slack-T01-D02-1712345678.123456",
        "portal-" + "x" * 200,
        "mcp-a.b.c",
        "..",
        "",
        "-leading-dash",
        "unicode-ü-ß",
        "sched-daily-2025-01-01",
    ],
)
def test_memory_session_ids_always_pass_the_api_pattern(raw):
    sid = memory_session_id(raw)
    assert _api_pattern("SessionId").match(sid), sid
    assert 1 <= len(sid) <= 100
    assert memory_session_id(raw) == sid  # deterministic


def test_an_id_that_already_fits_is_kept_verbatim():
    assert memory_session_id("gh-strands-agents-sdk-python-123") == "gh-strands-agents-sdk-python-123"


def test_ids_that_would_sanitize_alike_never_collide():
    # A '.' is not allowed in an AgentCore session id; a naive replace would merge these.
    ids = ["slack-T-D-1.2", "slack-T-D-1-2", "slack-T-D-1_2", "slack-T-D-12"]
    assert len({memory_session_id(i) for i in ids}) == len(ids)
    # Two long ids sharing their first 100 characters stay apart too.
    long_a, long_b = "gh-" + "a" * 120 + "1", "gh-" + "a" * 120 + "2"
    assert memory_session_id(long_a) != memory_session_id(long_b)
