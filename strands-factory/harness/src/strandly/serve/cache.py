"""Per-session agent cache for long-lived serving processes.

A deployed runtime keeps one live agent per session id, so warm state (sandbox session, MCP
clients, context manager) carries across invokes; a rebuild is only slower, because restore
overwrites. Each session has an :class:`asyncio.Lock`: invokes on one session serialize, different
sessions run in parallel. Sessionless invokes are never cached.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from strandly.core.context import RuntimeContext


class AgentCache:
    """Holds one live agent per session id, with a per-session lock."""

    def __init__(self) -> None:
        self._agents: dict[str, Any] = {}
        # The cache key each session's live agent is under (see ``get_or_build``'s ``session``).
        self._session_keys: dict[str, str] = {}
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

    async def get_or_build(
        self, key: str, build: Callable[[], Awaitable[Any]], *, session: str | None = None
    ) -> Any:
        """Return the cached agent for ``key``, building (and caching) it once if absent.

        With ``session`` (the bare session key, whose lock the caller holds) a session keeps one
        live agent: a different ``key`` on it drops the previous agent, so the new build restores
        the latest stored session instead of two agents overwriting each other's snapshots.
        """
        async with self._guard:
            if session is not None:
                previous = self._session_keys.get(session)
                if previous is not None and previous != key:
                    self._agents.pop(previous, None)
                self._session_keys[session] = key
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
    """The session this invoke belongs to, or ``None`` when it is sessionless (never cached).

    This is the *conversation* identity — what turns serialize on, and what the persistent session
    manager writes under. For the key an agent is **cached** under, see :func:`agent_cache_key`.
    """
    return ctx.session_id or ctx.session_key or None


def agent_cache_key(ctx: RuntimeContext) -> str | None:
    """The key a built agent may be reused under: the session plus the identity baked into it.

    A built agent keeps the long-term-memory personal scope of the invoke that built it
    (``memory.scopes.personal_actor``). Keying on the session alone would hand a second identity on
    the same ``slack-*`` session id the first caller's personal memories for the life of the
    process, so the personal namespace is part of the key. Its system prompt also introduces the
    run's advisory person (``RuntimeContext.person``), known to the directory or not, so a digest
    of that introduction is too, as is a model the caller chose for the run. Runs with none of them
    keep the bare key.

    A shared portal session can see several people's turns. The per-session lock stays on the bare
    :func:`session_key`, and :meth:`AgentCache.get_or_build` keeps one live agent per session, so a
    turn under another identity rebuilds from the stored session: slower, never a lost turn and
    never a leaked memory.
    """
    key = session_key(ctx)
    if key is None:
        return None
    # Imported here (not at module import) to keep this module free of the memory/strands import
    # chain — it is imported by every serving surface.
    from strandly.memory.scopes import personal_actor

    parts = [key]
    namespace = personal_actor(ctx)
    if namespace:
        parts.append(namespace)
    person = ctx.person
    if person is not None:
        digest = hashlib.sha256(person.introduction().encode()).hexdigest()[:16]
        parts.append(f"intro-{digest}")
    # A turn on another model or effort rebuilds from the stored session, like another identity.
    if ctx.model is not None:
        parts.append(f"model-{ctx.model}@{ctx.effort or 'auto'}")
    return "#".join(parts)


def get_cache() -> AgentCache:
    return _CACHE
