"""Run one turn: the helper every serving surface shares.

Streams one turn of normalized ``HarnessEvent``s. A session invoke reuses the cached agent for that
session under its lock (:mod:`strandly.serve.cache`); a sessionless invoke builds a fresh agent.
A turn takes an optional per-run ``cancel_signal``; a cached agent skips the model call of a turn
already cancelled when it starts and reports ``skipped`` in the caller's ``outcome``.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from typing import Any

from strands.hooks import BeforeModelCallEvent, HookOrder

from strandly.core.agent import build_agent
from strandly.core.config import Config
from strandly.core.context import RUN_CONTEXT_STATE_KEY, RuntimeContext
from strandly.core.events import HarnessEvent, translate
from strandly.plugins.caller_context import CALLER_CONTEXT_STATE_KEY
from strandly.plugins.goal import has_a_pending_cancel, has_background_work_outstanding
from strandly.serve.cache import agent_cache_key, get_cache, session_key
from strandly.tools.subagent import RUN_CANCEL_SIGNAL_STATE_KEY

# Stand-in assistant turn inserted when the previous turn left history in a shape that would make
# the next ask the SECOND consecutive user message. Bedrock Converse requires strict
# user/assistant alternation and rejects that; the SDK normalizes blank text and tool names, never
# roles. Two shapes get there, and both are interruptions:
#
# 1. history ends on a *user* message — a cancel landing between a model's ``tool_use``
#    and the tool call, where the SDK appends a synthetic ``toolResult`` user message ("Tool
#    execution cancelled") and stops;
# 2. history ends on an *assistant* message carrying an **unanswered ``toolUse``** — the assistant
#    message is persisted the moment the model returns it, so an instance recycled (or a process
#    killed) *during* tool execution restores exactly this. ``Agent._convert_prompt_to_messages``
#    then appends its own synthetic tool-result **user** message before our ask, which is what
#    produces the duplicate role. Worse than case 1: the shape is in *durable* history, so every
#    later turn on that session would fail identically — a permanently wedged session.
#
# So we close the interrupted turn explicitly, which is also the honest transcript (Claude Code
# writes an "[Request interrupted by user]" marker for the same reason) and gives the model the
# context for why its previous turn has no reply.
TURN_INTERRUPTED_MARKER = (
    "[This turn was interrupted before I finished — it was cancelled or cut off while work was in "
    "flight.]"
)

# The assistant text recorded for a turn whose run was cancelled before its model call.
SUPERSEDED_MARKER = "[cancelled before it started]"

# Invocation-state key for the caller's per-run outcome dict (see ``run_turn``'s ``outcome``).
RUN_OUTCOME_STATE_KEY = "strandly.run_outcome"


def _skip_model_call_when_cancelled(event: BeforeModelCallEvent) -> None:
    """No model request for a turn that is already cancelled; its user message is still recorded."""
    if has_a_pending_cancel(event.agent):
        event.cancel = SUPERSEDED_MARKER
        outcome = event.invocation_state.get(RUN_OUTCOME_STATE_KEY)
        if isinstance(outcome, dict):
            outcome["skipped"] = True


async def _build_session_agent(config: Config, ctx: RuntimeContext, *, model: Any | None) -> Any:
    agent = await build_agent(config, ctx, model=model)
    # First, so the other before-model hooks see the cancel and can skip their work.
    agent.add_hook(_skip_model_call_when_cancelled, BeforeModelCallEvent, order=HookOrder.SDK_FIRST)
    return agent


async def run_turn(
    config: Config,
    user_input: str,
    ctx: RuntimeContext,
    *,
    model: Any | None = None,
    cancel_signal: threading.Event | None = None,
    outcome: dict[str, Any] | None = None,
) -> AsyncIterator[HarnessEvent]:
    """Stream one normalized turn, reusing the per-session agent when the invoke has a session.

    ``cancel_signal``: this turn's own cancel event (``stream_async(cancel_signal=...)``). Setting
    it cancels this turn only; it never carries over to the agent's next turn.

    ``outcome``: a caller-owned dict this turn writes to; ``skipped`` is set when a cancel already
    set at the model call stopped the turn before its first model request, ``abandoned`` when the
    cancel ended the turn while background work (a subagent, a background command) was outstanding.
    """
    key = session_key(ctx)
    # The CURRENT payload's caller-supplied UI context, stamped into this call's invocation
    # state for the ``CallerContext`` plugin. Per call (not per agent) on purpose: the agent below
    # may be cached across requests while the user navigates between views, so the context must
    # ride the request. The whole turn context rides too (``RUN_CONTEXT_STATE_KEY``) for tools that
    # authorize on who triggered this turn.
    caller_context = ctx.invocation_context
    invocation_state: dict[str, Any] = {RUN_CONTEXT_STATE_KEY: ctx}
    if caller_context is not None:
        invocation_state[CALLER_CONTEXT_STATE_KEY] = caller_context
    if outcome is not None:
        invocation_state[RUN_OUTCOME_STATE_KEY] = outcome
    if cancel_signal is not None:
        # Outlives the SDK's per-invocation signal, so a queued subagent call still sees the cancel.
        invocation_state[RUN_CANCEL_SIGNAL_STATE_KEY] = cancel_signal
    if key is None:
        # Sessionless: a fresh agent, never cached — nothing to clear.
        agent = await build_agent(config, ctx, model=model)
        async for ev in translate(
            agent.stream_async(
                user_input, invocation_state=invocation_state, cancel_signal=cancel_signal
            )
        ):
            yield ev
        _note_abandoned_background_work(agent, cancel_signal, outcome)
        return

    cache = get_cache()
    lock = await cache.lock_for_session(key)
    # Serialize turns on the same SESSION — one agent's messages can't be mutated concurrently (the
    # SDK raises ConcurrencyException), and two agents on one session id must not interleave writes
    # to the same session manager; different sessions don't contend. A run superseded by a newer
    # message releases this lock as soon as its turn stops. The agent is *cached* under a narrower
    # key — the session plus the per-person identity baked into the build — so a different identity
    # on the same session gets its own agent instead of inheriting the first one's personal memory
    # store (see ``agent_cache_key``, which documents the trade-off). The lock stays on the bare
    # session key: it is about the session, not the person.
    async with lock:
        agent = await cache.get_or_build(
            agent_cache_key(ctx) or key,
            lambda: _build_session_agent(config, ctx, model=model),
            session=key,
        )
        # The SDK waits for a running tool batch before it honours a cancel; a sandbox that can
        # (``RuntimeSandbox``) stops waiting on its commands once this turn's signal is set.
        bind = getattr(getattr(agent, "sandbox", None), "bind_cancel_signal", None)
        if not callable(bind) or cancel_signal is None:
            bind = None
        if bind is not None:
            bind(cancel_signal)
        try:
            async for ev in translate(
                agent.stream_async(
                    _turn_input(agent, user_input),
                    invocation_state=invocation_state,
                    cancel_signal=cancel_signal,
                )
            ):
                yield ev
            _note_abandoned_background_work(agent, cancel_signal, outcome)
        finally:
            if bind is not None:
                bind(None)


def _note_abandoned_background_work(
    agent: Any, cancel_signal: threading.Event | None, outcome: dict[str, Any] | None
) -> None:
    """Set ``outcome["abandoned"]`` when the cancel ended the turn with background work outstanding.

    The SDK waits for background tasks after the model's last answer; a cancel ends that wait and
    the turn, but the turn still reports that answer's ``end_turn``.
    """
    if (
        outcome is not None
        and cancel_signal is not None
        and cancel_signal.is_set()
        and has_background_work_outstanding(agent)
    ):
        outcome["abandoned"] = True


def _needs_interrupted_marker(last: Any) -> bool:
    """True when appending a user ask after ``last`` would duplicate the ``user`` role.

    Either ``last`` already *is* a user message, or it is an assistant message with an unanswered
    ``toolUse`` — in which case the SDK inserts a synthetic tool-result **user** message ahead of our
    ask (``Agent._convert_prompt_to_messages``), which duplicates the role just the same. See
    :data:`TURN_INTERRUPTED_MARKER`.
    """
    if not isinstance(last, dict):
        return False
    if last.get("role") == "user":
        return True
    content = last.get("content")
    return last.get("role") == "assistant" and isinstance(content, list) and any(
        isinstance(block, dict) and "toolUse" in block for block in content
    )


def _turn_input(agent: Any, user_input: str) -> Any:
    """The turn's input, kept valid for a provider that requires strict role alternation.

    Normally just ``user_input``. When the agent's history ends in a shape that would make this ask
    the second consecutive ``user`` message (see :func:`_needs_interrupted_marker`), the input becomes
    an explicit ``[assistant marker, user ask]`` pair so the conversation still alternates. Passing
    ``Messages`` is a documented ``AgentInput`` shape and goes through the agent's own append path, so
    the marker is persisted by the session manager exactly like any other message.
    """
    messages = getattr(agent, "messages", None) or []
    if not user_input or not messages or not _needs_interrupted_marker(messages[-1]):
        return user_input
    return [
        {"role": "assistant", "content": [{"text": TURN_INTERRUPTED_MARKER}]},
        {"role": "user", "content": [{"text": user_input}]},
    ]
