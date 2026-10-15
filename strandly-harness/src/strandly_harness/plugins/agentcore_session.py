"""Cross-invocation persistence for the AgentCore sandbox's managed session.

The :class:`~strandly_harness.sandbox.agentcore.AgentCoreSandbox` *lazily* starts a managed
Code Interpreter session on first use and stops it on ``close()``. On its own, that
session lives and dies within a single process: the next invocation cold-starts a brand
new session, losing the warm kernel/filesystem and paying start-up latency again.

This plugin closes that gap by treating ``agent.state`` as the durable carrier (the
session manager already persists ``agent.state`` to its backend across invocations):

1. **Restore** — on :class:`AgentInitializedEvent` (fired *after* the session manager has
   already rehydrated ``agent.state``), read the saved session id and hand it to the
   sandbox via :meth:`AgentCoreSandbox.adopt_session`, so the next invoke reattaches to
   the same session instead of cold-starting.
2. **Record** — after sandbox activity (:class:`AfterToolCallEvent`) and at the end of the
   turn (:class:`AfterInvocationEvent`), capture the now-live session id into
   ``agent.state`` so the session manager serializes it for the next invocation.

It is intentionally a hooks-only plugin (no tools): it just wires sandbox lifecycle to
the agent's persisted state. It is a no-op unless the agent's sandbox is an
``AgentCoreSandbox`` that *owns* its session (lazy-start mode); attached, caller-owned
sessions (``session_id`` set in config) are never recorded, because the harness does not
manage their lifecycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from strands.hooks import AfterInvocationEvent, AfterToolCallEvent, AgentInitializedEvent
from strands.plugins import Plugin, hook

if TYPE_CHECKING:
    from strandly_harness.sandbox.agentcore import AgentCoreSandbox

#: ``agent.state`` key under which the live managed session id is persisted.
SESSION_STATE_KEY = "agentcore_session_id"


def _owned_agentcore_sandbox(agent: object) -> AgentCoreSandbox | None:
    """Return the agent's sandbox iff it is an AgentCoreSandbox that owns its session."""
    # Imported lazily so this module (and the harness's default plugin wiring) never
    # requires the optional `bedrock-agentcore` extra unless an AgentCore sandbox is in use.
    from strandly_harness.sandbox.agentcore import AgentCoreSandbox

    sandbox = getattr(agent, "sandbox", None)
    if isinstance(sandbox, AgentCoreSandbox) and sandbox.owns_session:
        return sandbox
    return None


class AgentCoreSessionPlugin(Plugin):
    """Persist + restore the AgentCore managed session id across invocations via ``agent.state``."""

    name = "meta-harness-agentcore-session"

    @hook  # type: ignore[call-overload]  # SDK hook() overloads don't model bound (self, event) methods
    def restore(self, event: AgentInitializedEvent) -> None:
        """Reattach to a previously-persisted session id, if one is in ``agent.state``.

        Fires after the session manager has rehydrated ``agent.state``, so a session id
        saved on a prior invocation is available here. Adoption is best-effort: the
        sandbox treats the id as unverified and transparently cold-starts a fresh session
        on first use if it has since expired.
        """
        agent = event.agent
        sandbox = _owned_agentcore_sandbox(agent)
        if sandbox is None:
            return
        saved = agent.state.get(SESSION_STATE_KEY)
        adopted = False
        if isinstance(saved, str) and saved:
            adopted = sandbox.adopt_session(saved)
        # Warm up (start session + bootstrap git in the background) ONLY when we didn't adopt a
        # prior session — so the ~30-60s bootstrap overlaps the agent's first non-sandbox work
        # instead of blocking the first sandbox tool call. Must come AFTER adoption: warming first
        # would start a fresh session and make adopt_session a no-op, defeating session reuse (and
        # losing the prior session's filesystem). No-op when adopted or already live.
        if not adopted:
            sandbox.warm_up()

    @hook  # type: ignore[call-overload]
    def record_after_tool(self, event: AfterToolCallEvent) -> None:
        """Capture the live session id after a (possibly session-starting) tool call."""
        self._record(event.agent)

    @hook  # type: ignore[call-overload]
    def record_after_invocation(self, event: AfterInvocationEvent) -> None:
        """Capture the live session id at the end of the turn (catch-all)."""
        self._record(event.agent)

    @staticmethod
    def _record(agent: object) -> None:
        """Sync the sandbox's current session id into ``agent.state`` (idempotent).

        Only writes when the value actually changes, so we don't needlessly bump the
        state version (which would force the session manager to re-serialize every turn).
        Clears the key if a recorded session has since been closed.
        """
        sandbox = _owned_agentcore_sandbox(agent)
        if sandbox is None:
            return
        state = agent.state  # type: ignore[attr-defined]
        current = sandbox.session_id
        saved = state.get(SESSION_STATE_KEY)
        if current:
            if current != saved:
                state.set(SESSION_STATE_KEY, current)
        elif saved is not None:
            # Session was closed/never started — drop the stale id so the next
            # invocation cold-starts cleanly instead of adopting a dead session.
            state.delete(SESSION_STATE_KEY)
