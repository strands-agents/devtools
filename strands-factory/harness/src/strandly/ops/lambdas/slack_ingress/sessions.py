"""Slack session ids: one conversation per DM thread (and per home-channel thread).

The session key is the thread root (``thread_ts`` for a reply, the message's own ``ts`` otherwise),
so a thread maps to one agent session, Memory thread and sandbox workspace; continuity across
threads comes from personal long-term memory. Ids carry a ``slack-`` prefix that cannot collide with
the ``gh-`` scheme and must be valid for both AgentCore Runtime and Memory, which rejects dots, so
the ``ts`` dot becomes a hyphen.
"""

from __future__ import annotations

import re

#: Characters AgentCore Memory rejects in a ``sessionId`` (it allows only ``[A-Za-z0-9_-]``).
_MEMORY_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def thread_root_ts(message_ts: str, thread_ts: str | None) -> str:
    """The ``ts`` of the thread a DM belongs to: its ``thread_ts``, or its own ``ts`` if top-level."""
    return thread_ts or message_ts


def session_id_for_dm(team_id: str, channel_id: str, root_ts: str) -> str:
    """``slack-<team_id>-<channel_id>-<root_ts>`` — the session id for one DM thread.

    The ``ts`` dot becomes ``-`` (see module docstring): ``slack-T1-D1-1791234567-123456``.
    """
    return _MEMORY_UNSAFE.sub("-", f"slack-{team_id}-{channel_id}-{root_ts}").strip("-")


def session_id_for_channel(channel_id: str, root_ts: str) -> str:
    """``slack-ch-<channel_id>-<root_ts>`` — the session id for one home-channel thread."""
    return _MEMORY_UNSAFE.sub("-", f"slack-ch-{channel_id}-{root_ts}").strip("-")
