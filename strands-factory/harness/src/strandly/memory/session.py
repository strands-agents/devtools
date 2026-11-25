"""Short-term memory: the snapshot session manager and reading conversations back.

The SDK's ``SnapshotSessionManager`` over the session store (``memory/session_store.py``) saves
``snapshot_latest`` on every message and one checkpoint per invocation. Restore overwrites, never
replays, so rebuilding an agent over a session cannot duplicate history, and a failed restore stops
the build (:class:`FailClosedSnapshotSessionManager`) rather than serving an empty agent.
"""

from __future__ import annotations

from typing import Any

from strands.session import SnapshotSessionManager

from strandly.core.config import Config
from strandly.core.context import RuntimeContext

# The id helpers live in the strands-free ``core.session_ids`` so ``ops`` and the Lambdas can use them
# without importing this module; re-exported for ``memory.session`` callers.
from strandly.core.session_ids import runtime_session_id, sanitize_session_id
from strandly.core.session_store_reader import (
    Conversation,
    final_assistant_text,
    settled,
    store_session_id,
)
from strandly.memory.session_store import (
    FailClosedStorage,
    SessionStorageError,
    build_reader,
    storage_failure,
)

# Explicit re-exports (sanitize_session_id and runtime_session_id are re-export-only).
__all__ = [
    "FailClosedSnapshotSessionManager",
    "build_session_manager",
    "conversation_settled",
    "final_assistant_text",
    "read_conversation",
    "runtime_session_id",
    "sanitize_session_id",
    "session_id_for",
]

# --- the session manager ---------------------------------------------------------------------


def _checkpoint_every_invocation(**_: Any) -> bool:
    """Snapshot trigger: one immutable checkpoint per invocation (rewind / turn history)."""
    return True


class FailClosedSnapshotSessionManager(SnapshotSessionManager):
    """``SnapshotSessionManager`` whose restore fails CLOSED and keeps the build's system prompt.

    The SDK raises out of agent construction when a stored snapshot cannot be read or restored (a
    storage error, corrupt JSON, an unsupported ``schema_version``). That is the behaviour we want:
    the error is logged with ``SESSION_STORAGE_FAILURE``, counted, and re-raised as a
    :class:`~strandly.memory.session_store.SessionStorageError`, so the agent is never
    built, nothing is cached, the run fails with a clear error, and the next invocation retries the
    restore. An agent that starts empty over an existing session never exists, so it can never save
    over the stored snapshot.
    """

    def initialize(self, agent: Any, **kwargs: Any) -> None:
        # A snapshot carries the system prompt of the build that saved it; this build's prompt
        # (current code, current tools) wins.
        prompt = agent.system_prompt_content
        try:
            super().initialize(agent, **kwargs)
        except SessionStorageError:
            raise  # already logged + counted by the storage wrapper
        except Exception as e:
            raise storage_failure("restore", f"session {self.session_id}", self._backend(), e) from e
        agent.system_prompt = prompt

    def _backend(self) -> str:
        storage = self._raw_storage
        return str(getattr(storage, "backend", type(storage).__name__))


def session_id_for(ctx: RuntimeContext) -> str | None:
    """The store session id for an invocation, or ``None`` for a sessionless invoke.

    A sessionless invoke gets no persistence: one shared literal id would let unrelated runs
    restore and overwrite each other's history. Every surface that wants continuity passes an id
    (the CLI defaults one).
    """
    raw = ctx.session_id or ctx.session_key
    return store_session_id(raw) if raw else None


def build_session_manager(
    config: Config, ctx: RuntimeContext, storage: FailClosedStorage
) -> FailClosedSnapshotSessionManager | None:
    """The snapshot session manager over ``storage``, or ``None`` when the invoke has no session.

    ``storage`` is the per-build session store (``session_store.build_storage``) — the same instance
    ``build_agent`` passes agent-level so the context manager's stash is durable alongside it.
    """
    session_id = session_id_for(ctx)
    if session_id is None:
        return None
    return FailClosedSnapshotSessionManager(
        session_id,
        storage=storage,
        # Per message: lets a cross-instance poll see a
        # multi-hour run's progress, and loses at most one message on a microVM crash. The
        # invocation save still runs too (it captures the post-management history).
        save_latest_on="message",
        snapshot_trigger=_checkpoint_every_invocation,
    )


# --- reading back: the session store ------------------------------------------------------------


def read_conversation(config: Config, session_id: str) -> Conversation:
    """A session as ordered ``(role, text, has_tool_use)`` — the fire-and-forget read channel.

    The latest snapshot (one GET); ``[]`` when the store has none. Store errors propagate (callers
    degrade).
    """
    return build_reader(config).conversation(session_id) or []


def conversation_settled(conversation: Conversation) -> bool:
    """The tool_use-aware settle heuristic — see :func:`strandly.core.session_store_reader.settled`."""
    return settled(conversation)
