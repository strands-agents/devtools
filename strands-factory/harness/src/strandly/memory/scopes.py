"""Which actor a conversation's memory belongs to.

The transcript is written under ``(memory_id, actor, session)`` and extraction writes long-term
records under that actor's namespaces, so this one decision controls who can recall what. A Slack DM
that the Slack ingress dispatched (a ``slack:`` trigger) belongs to ``slack-user-<UID>`` and recalls
that person's scope plus the team's. A portal, CLI or ACP run whose advisory person the people
directory knows shares that person's memory: ``slack-user-<UID>`` when the person has a Slack id
(the same memory as their DMs), else ``person-<person_id>``. Personal memory is a convenience
layer, not a private store: the person is a caller claim. Every other run (GitHub, Slack channels,
the portal or CLI without a known person, scheduled jobs) is the ``team`` actor. Personal content
never reaches team memory.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

from strandly.core.constants import (
    MEMORY_PERSON_ACTOR_PREFIX,
    MEMORY_PERSONAL_ACTOR_PREFIX,
    MEMORY_TEAM_ACTOR,
)
from strandly.core.context import TRIGGER_SOURCE_KEY
from strandly.core.session_ids import (
    SESSION_ID_MAX_LEN as SESSION_ID_MAX_LEN,
)
from strandly.core.session_ids import (
    memory_session_id as memory_session_id,
)
from strandly.core.session_store_reader import store_session_id as store_session_id

if TYPE_CHECKING:
    from strandly.core.context import RuntimeContext

TEAM_ACTOR = MEMORY_TEAM_ACTOR
PERSONAL_ACTOR_PREFIX = MEMORY_PERSONAL_ACTOR_PREFIX
PERSON_ACTOR_PREFIX = MEMORY_PERSON_ACTOR_PREFIX
# The trigger source the Slack ingress stamps on a DM it verified (``slack:<UID>``).
SLACK_TRIGGER_PREFIX = "slack:"

# What an AgentCore Memory actor id may hold of a person_id slug (no dots).
_ACTOR_SAFE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def person_actor_id(person_id: str) -> str:
    """``person-<person_id>``; a slug with dots gets them replaced plus a short digest of the
    original, so two slugs never share an actor."""
    if _ACTOR_SAFE.match(person_id):
        return f"{PERSON_ACTOR_PREFIX}{person_id}"
    digest = hashlib.sha256(person_id.encode()).hexdigest()[:8]
    return f"{PERSON_ACTOR_PREFIX}{person_id.replace('.', '-')}-{digest}"


def personal_actor(ctx: RuntimeContext | None) -> str | None:
    """The run's personal actor, or ``None`` (the run has no personal scope).

    ``slack-user-<UID>`` for a Slack DM the Slack ingress dispatched, or for a portal/CLI/ACP run
    whose known person has a Slack id; ``person-<person_id>`` for a known person without one.
    """
    if ctx is None:
        return None
    if ctx.is_slack_dm:
        # The Slack ingress verified this user id and stamps it as ``slack:<UID>``; Slack keys from
        # any other surface (a scheduled job notifying a DM, the portal, ACP) select no Slack actor.
        source = (ctx.event or {}).get(TRIGGER_SOURCE_KEY)
        if source == f"{SLACK_TRIGGER_PREFIX}{ctx.slack_user_id}":
            return f"{PERSONAL_ACTOR_PREFIX}{ctx.slack_user_id}"
    person = ctx.person
    if person is None or not person.known:
        return None
    if person.slack_user_id:
        return f"{PERSONAL_ACTOR_PREFIX}{person.slack_user_id}"
    return person_actor_id(person.person_id)


def memory_actor(ctx: RuntimeContext | None) -> str:
    """The AgentCore Memory actor a run's transcript (and so its extracted memory) belongs to."""
    return personal_actor(ctx) or TEAM_ACTOR
