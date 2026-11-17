"""Per-session agent cache for long-lived serving processes.

A deployed AgentCore Runtime is a **long-lived process** that serves many invocations. If each
invocation rebuilt an ``Agent`` over the *same* persistent session manager, the SDK would replay
the restored history *and* re-append it — collapsing/duplicating the conversation. So within one
process we keep **one live agent per session id** and reuse it across invokes; only its in-memory
state advances, and the session manager persists deltas exactly once.

Concurrency: each session gets an :class:`asyncio.Lock`. Concurrent invokes on the *same* session
serialize (a single agent's message list is not safe to mutate from two turns at once); invokes on
*different* sessions run in parallel. Sessionless invokes (no id — one-shot/interactive) are never
cached and always build fresh.

This cache is only for the streaming/interactive path within a process. The fire-and-forget
deployed path builds its own agent per background task (see ``serve/agentcore_app.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from strandly_harness.core.context import RuntimeContext


class AgentCache:
    """Holds one live agent per session id, with a per-session lock."""

    def __init__(self) -> None:
        self._agents: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Guards the maps themselves while we look up / create per-session entries.
        self._guard = asyncio.Lock()

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    async def get_or_build(self, key: str, build: Callable[[], Awaitable[Any]]) -> Any:
        """Return the cached agent for ``key``, building (and caching) it once if absent."""
        async with self._guard:
            agent = self._agents.get(key)
        if agent is not None:
            return agent
        agent = await build()
        async with self._guard:
            # Double-check: another coroutine may have built it while we awaited.
            existing = self._agents.get(key)
            if existing is not None:
                return existing
            self._agents[key] = agent
        return agent

    def lock_for_session(self, key: str) -> Awaitable[asyncio.Lock]:
        """Public accessor for a session's lock (callers hold it for the duration of a turn)."""
        return self._lock_for(key)


# One process-wide cache for the streaming serving path.
_CACHE = AgentCache()


def session_key(ctx: RuntimeContext) -> str | None:
    """The cache key for a context, or ``None`` when the invoke is sessionless (never cached)."""
    return ctx.session_id or ctx.session_key or None


def get_cache() -> AgentCache:
    return _CACHE
