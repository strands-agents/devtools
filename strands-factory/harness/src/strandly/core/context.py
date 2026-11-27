"""Per-invocation runtime context shared across the harness."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from strandly.core.constants import DIRECT_INVOKE_KEY

# Payload key the Slack DM ingress (``ops/lambdas/slack_ingress``) rides along on every dispatch it
# makes. Its presence is the ONLY marker that an invocation was triggered from Slack — no other
# surface (GitHub mention poller, scheduler, CLI, MCP) sets it.
SLACK_CHANNEL_KEY = "slackChannelId"
# The Slack user id (``U…``/``W…``) the DM came from, also set by the Slack ingress on every
# dispatch. Unlike the channel id this is NOT a capability gate — it is the per-person identity the
# long-term-memory *personal scope* is derived from (``memory/scopes.py``).
SLACK_USER_KEY = "slackUserId"
# The thread root ``ts`` the Slack ingress dispatched this run for (every DM conversation is a
# thread — see ``ops.lambdas.slack_ingress.sessions``). ``use_slack`` defaults ``thread_ts`` to it
# so a reply posted without one still lands in the thread instead of orphaning at the DM root.
SLACK_THREAD_KEY = "slackThreadTs"

# How the run started (``slack:<user>``, ``github:@<login>``, ``portal:chat``,
# ``github-actions:<owner>/<repo>/<workflow>``, ``scheduled:<job>[:manual]``), set by every
# ingress. Read by the ledger and by the ``schedule`` tool's authorization (``tools/schedule.py``).
TRIGGER_SOURCE_KEY = "triggerSource"
# The immutable Cognito subject and issuer verified by the dashboard API.
PORTAL_USER_KEY = "portalUser"
PORTAL_ISSUER_KEY = "portalIssuer"
# The person of a portal, CLI or ACP run: the enabled people-directory row the dashboard API found
# bound to the verified Cognito ``sub`` it also sends as ``portalUser`` (``dashboard/api/handler.py``),
# or, on a trusted direct invoke (``DIRECT_INVOKE_KEY``), the row the invoke named.
# ``personGithub`` is that row's login, absent when the row has none.
PERSON_GITHUB_KEY = "personGithub"
PERSON_ID_KEY = "personId"
PERSON_NAME_KEY = "personName"
PERSON_ROLE_KEY = "personRole"
PERSON_SLACK_USER_KEY = "personSlackUserId"
# The trigger sources whose payloads may carry a person: the dashboard API's chat and ACP launches.
PERSON_TRIGGER_PREFIXES = ("portal:", "acp:")
# The GitHub issue/PR a mention-poller run was dispatched for (``https://github.com/o/r/pull/1``).
GITHUB_URL_KEY = "githubUrl"
# Stable account identity from the authenticated GitHub API mention item, not its login.
GITHUB_USER_ID_KEY = "githubUserId"
GITHUB_ISSUER_KEY = "githubIssuer"
# The workflow run the GitHub Actions ingress verified from its OIDC token (repository, workflow,
# run, actor), with ``triggerSource`` ``github-actions:<owner>/<repo>/<workflow file>``.
GITHUB_ACTIONS_KEY = "githubActions"
GITHUB_ACTIONS_ISSUER = "https://token.actions.githubusercontent.com"
# Invocation-state key ``run_turn`` stamps with the CURRENT turn's ``RuntimeContext``: the agent is
# cached per session, so a tool that depends on who triggered the turn reads it from here.
RUN_CONTEXT_STATE_KEY = "strandly.run_context"

# Slack channel-id prefix for an IM (a 1:1 direct message). Channel ids are typed by their first
# letter — ``D`` is a DM, ``C``/``G`` are (multi-person) channels and group DMs. This matters because
# a Slack session id is scoped to one thread *within* a channel (``slack-<team>-<channel>-<root_ts>``,
# see ``ops.lambdas.slack_ingress.sessions``): only in a ``D`` channel is every session's thread
# guaranteed to be one person, which is the precondition for deriving a per-person memory namespace.
SLACK_DM_CHANNEL_PREFIX = "D"

# The shape of a Slack user id: ``U``/``W`` (``B`` for a bot) + uppercase alphanumerics. Validated
# because this value becomes a **memory namespace** (a filter value stamped on KB writes and used in
# the retrieval filter): a junk/attacker-shaped value must never reach that filter or silently
# create a garbage namespace. Slack ids are case-sensitive opaque ids, so no case folding.
_SLACK_USER_ID_RE = re.compile(r"^[UWB][A-Z0-9]{1,31}$")

# Mirrors ``strandly.ops.people`` (login, person_id slug, one-line field cap).
_GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_PERSON_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# A Cognito ``sub`` (mirrors the dashboard API's ``_PORTAL_USER_RE``).
_PORTAL_USER_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_LINE_BREAKING = re.compile(r"[\s\x00-\x1f\x7f\u2028\u2029\u0085]+")
_PERSON_FIELD_MAX = 80

# The shape of a Slack message ``ts``: epoch seconds, a dot, microseconds (``1791234567.123456``).
_SLACK_TS_RE = re.compile(r"^\d{9,12}\.\d{1,9}$")

# Payload key a calling application (the maintainer portal today) may set to describe the ambient
# UI state of the human behind the invocation — most usefully *what they are currently viewing*
# (the issue/PR open in a triage or review pane), so "what do you think about this one?" resolves
# without the URL being pasted. Purely additive: nothing is gated on it (like ``slackUserId`` is
# for memory namespacing, this is for visibility only). Caller-supplied text that lands in the
# prompt, so it is validated here (dict-shaped or dropped) and size-clamped where it is rendered
# (``plugins/caller_context.py``).
INVOCATION_CONTEXT_KEY = "invocationContext"

# Rendering budget for one caller-context block, in characters of serialized JSON. The context is
# ambient state ("issue X is on screen"), not content — a few KB is generous, and clamping keeps a
# misbehaving (or malicious) caller from spending the prompt on it.
MAX_INVOCATION_CONTEXT_CHARS = 4000


def invocation_context_of(payload: Any) -> dict[str, Any] | None:
    """The caller-supplied invocation context carried by an invoke payload, or ``None``.

    Accepts only a non-empty ``dict`` under :data:`INVOCATION_CONTEXT_KEY` — anything else
    (absent, ``None``, a string, a list, ``{}``) yields ``None``, so downstream renderers never
    see a junk shape. Read by :attr:`RuntimeContext.invocation_context` (the current turn's
    payload).
    """
    if not isinstance(payload, dict):
        return None
    context = payload.get(INVOCATION_CONTEXT_KEY)
    if not isinstance(context, dict) or not context:
        return None
    return context


def _one_line(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = _LINE_BREAKING.sub(" ", value).strip()
    return text[:_PERSON_FIELD_MAX]


@dataclass(frozen=True)
class RunPerson:
    """The people-directory row bound to a portal, CLI or ACP run's signed-in user. It selects the
    person's memory, shared with their Slack DMs (``memory/scopes.py``)."""

    github: str
    person_id: str = ""
    name: str = ""
    role: str = ""
    slack_user_id: str = ""

    @property
    def known(self) -> bool:
        """Whether the run has a directory row (always, for a :func:`person_of` result)."""
        return bool(self.person_id)

    def introduction(self) -> str:
        """The directory's one-line introduction."""
        from strandly.ops.people import Person

        return Person(self.person_id, self.name, self.role, self.github, self.slack_user_id).introduction()


def is_direct_invoke_payload(payload: Any) -> bool:
    """Whether the dashboard API marked this run as a trusted direct invoke (exactly ``true``)."""
    return isinstance(payload, dict) and payload.get(DIRECT_INVOKE_KEY) is True


def person_of(payload: Any) -> RunPerson | None:
    """The run's person from a portal/ACP payload's ``person*`` fields, or ``None``.

    Only the dashboard API's sources (:data:`PERSON_TRIGGER_PREFIXES`) may carry one, and only
    next to the verified ``portalUser`` the person is bound to, or with ``directInvoke: true``
    (:data:`DIRECT_INVOKE_KEY`), which the API sets only for a direct Lambda invoke that named the
    person. Every field is validated or flattened: these values reach the prompt, the schedule owner
    and the memory actor. A malformed ``personId`` means no person; a malformed login or Slack id
    drops only that field.
    """
    if not isinstance(payload, dict):
        return None
    source = payload.get(TRIGGER_SOURCE_KEY)
    if not isinstance(source, str) or not source.strip().startswith(PERSON_TRIGGER_PREFIXES):
        return None
    portal_user = payload.get(PORTAL_USER_KEY)
    verified = isinstance(portal_user, str) and bool(_PORTAL_USER_RE.fullmatch(portal_user))
    if not verified and not is_direct_invoke_payload(payload):
        return None
    person_id = payload.get(PERSON_ID_KEY)
    if not isinstance(person_id, str) or not _PERSON_ID_RE.match(person_id.strip()):
        return None
    github = payload.get(PERSON_GITHUB_KEY)
    github = github.strip().lower() if isinstance(github, str) and _GITHUB_LOGIN_RE.match(github.strip()) else ""
    slack = payload.get(PERSON_SLACK_USER_KEY)
    slack = slack.strip() if isinstance(slack, str) else ""
    return RunPerson(
        github,
        person_id.strip(),
        _one_line(payload.get(PERSON_NAME_KEY)),
        _one_line(payload.get(PERSON_ROLE_KEY)),
        slack if _SLACK_USER_ID_RE.match(slack) else "",
    )


@dataclass
class RuntimeContext:
    """Everything an invocation needs that is not in the static config.

    Built fresh per request/turn by a serving adapter and threaded into ``build_agent``.
    """

    cwd: str = field(default_factory=os.getcwd)
    session_id: str | None = None
    session_key: str | None = None  # derive a deterministic id when session_id is unset
    event: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    now: datetime | None = None  # clock override for deterministic tests
    parent_session_id: str | None = None  # a subagent's: the delegating top-level run's session
    # The run's model string and effort, set by a serving adapter only after the harness model
    # resolution builds the caller's choice; ``None`` is config.model.
    model: str | None = None
    effort: str | None = None

    def timestamp(self) -> datetime:
        return self.now or datetime.now(timezone.utc)

    @property
    def slack_channel_id(self) -> str | None:
        """The Slack DM channel this invocation came from, or ``None`` if it isn't from Slack.

        The Slack ingress dispatches with ``slackChannelId`` in the invoke payload (which the
        serving adapter hands over as :attr:`event`), so this doubles as "was this run triggered
        from Slack?" — which is what gates the ``use_slack`` tool in ``build_tools``.
        """
        event = self.event if isinstance(self.event, dict) else None
        channel = (event or {}).get(SLACK_CHANNEL_KEY)
        return channel.strip() if isinstance(channel, str) and channel.strip() else None

    @property
    def slack_user_id(self) -> str | None:
        """The Slack user who sent this DM, or ``None`` when the run isn't a (valid) Slack one.

        Read from the ingress payload's ``slackUserId`` (see :data:`SLACK_USER_KEY`) and validated
        against :data:`_SLACK_USER_ID_RE` — an absent, empty, non-string or wrong-shaped value
        yields ``None``. This is the identity the agent's **personal long-term-memory namespace** is
        derived from (``memory.scopes.personal_actor``), which is why it is validated
        rather than passed through: the value ends up as a Bedrock retrieval-filter value stamped on
        every write. It deliberately gates nothing else — ``use_slack`` still keys off
        :attr:`slack_channel_id`.
        """
        event = self.event if isinstance(self.event, dict) else None
        user = (event or {}).get(SLACK_USER_KEY)
        if not isinstance(user, str):
            return None
        user = user.strip()
        return user if _SLACK_USER_ID_RE.match(user) else None

    @property
    def slack_thread_ts(self) -> str | None:
        """The Slack thread root this run answers in, or ``None`` when absent / not ts-shaped.

        Read from the ingress payload's ``slackThreadTs`` (see :data:`SLACK_THREAD_KEY`). It gives
        ``use_slack`` a default ``thread_ts`` for ``chat.postMessage`` and is the only thread a DM
        run may post in (``None`` means only the DM root).
        """
        event = self.event if isinstance(self.event, dict) else None
        ts = (event or {}).get(SLACK_THREAD_KEY)
        if not isinstance(ts, str):
            return None
        ts = ts.strip()
        return ts if _SLACK_TS_RE.match(ts) else None

    @property
    def is_slack_dm(self) -> bool:
        """Whether this run came from a Slack **1:1 DM** (as opposed to any multi-person surface).

        True when the channel this invocation carries is an IM channel (``D…``, see
        :data:`SLACK_DM_CHANNEL_PREFIX`) *and* a valid user id came along with it. The ingress also
        dispatches for the team's home channel (``C…``/``G…``), where "the channel" is not "the
        person": anything derived per-person from a Slack session (the long-term-memory personal
        namespace) switches itself off there rather than attribute a second user's turn to the
        first user's identity. Fails closed.
        """
        channel = self.slack_channel_id
        return bool(
            channel and channel.startswith(SLACK_DM_CHANNEL_PREFIX) and self.slack_user_id
        )

    @property
    def person(self) -> RunPerson | None:
        """The bound person of a portal, CLI or ACP run (see :func:`person_of`)."""
        return person_of(self.event)

    @property
    def invocation_context(self) -> dict[str, Any] | None:
        """Ambient UI state the caller sent with this invocation, or ``None`` (see
        :func:`invocation_context_of`). Read from the invoke payload's ``invocationContext`` key;
        injected per turn into the latest user message by the ``CallerContext`` plugin. Gates
        nothing — deliberately, per the same rule as :attr:`slack_user_id`'s docstring: additive
        context must never become an implicit capability switch."""
        return invocation_context_of(self.event)

