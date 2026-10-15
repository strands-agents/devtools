"""Shared run-one-turn helper used by every serving surface.

Streams one normalized ``HarnessEvent`` turn. For a **session** invoke we reuse one live agent per
session from the process-wide :mod:`agent cache <strandly_harness.serve.cache>` (held under
its per-session lock for the turn), so a long-lived serving process doesn't rebuild an agent over
the same persistent session manager and collapse/duplicate history. A **sessionless** invoke
(one-shot CLI, a stateless ask) builds a fresh agent each time and isn't cached.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from strandly_harness.core.agent import build_agent
from strandly_harness.core.config import Config
from strandly_harness.core.context import RuntimeContext
from strandly_harness.core.events import HarnessEvent, translate
from strandly_harness.serve.cache import get_cache, session_key


async def run_turn(
    config: Config,
    user_input: str,
    ctx: RuntimeContext,
    *,
    model: Any | None = None,
    hitl: bool = False,
) -> AsyncIterator[HarnessEvent]:
    """Stream one normalized turn, reusing the per-session agent when the invoke has a session."""
    key = session_key(ctx)
    if key is None:
        # Sessionless: a fresh agent, never cached.
        agent = await build_agent(config, ctx, hitl=hitl, model=model)
        async for ev in translate(agent.stream_async(user_input)):
            yield ev
        return

    cache = get_cache()
    lock = await cache.lock_for_session(key)
    # Serialize turns on the same session (one agent's messages can't be mutated concurrently);
    # different sessions don't contend.
    async with lock:
        agent = await cache.get_or_build(
            key, lambda: build_agent(config, ctx, hitl=hitl, model=model)
        )
        async for ev in translate(agent.stream_async(user_input)):
            yield ev
