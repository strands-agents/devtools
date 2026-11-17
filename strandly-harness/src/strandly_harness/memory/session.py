"""Short-term memory: session ids, session managers, and reading conversations back.

- **Session ids:** :func:`sanitize_session_id` (filesystem/AgentCore-safe) and
  :func:`runtime_session_id` (AgentCore Runtime affinity key, padded to the minimum length).
- **Session manager** (per-conversation persistence): ``AgentCoreMemorySessionManager`` when an
  AgentCore Memory id is configured (``AGENTCORE_MEMORY_ID``), else a ``FileSessionManager`` on
  disk — see :func:`build_session_manager`. Hook-invoked methods are wrapped fail-soft
  (:func:`_resilient_session_manager`) so a persistence backend failure never kills a turn.
- **Reading back** (the fire-and-forget channel): :func:`read_events` / :func:`read_conversation`
  parse AgentCore Memory ``ListEvents`` output into ordered messages, with the tool_use-aware
  :func:`conversation_settled` heuristic for completion detection.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from strandly_harness.core.config import Config
from strandly_harness.core.constants import (
    MEMORY_MAX_EVENTS,
    SESSION_DIR,
)
from strandly_harness.core.context import RuntimeContext

# The id-normalization helpers live in the strands-free ``core`` zone so ``ops`` / the trigger-Lambda
# bundle can use them without importing this (strands-dependent) module. Re-exported here for the
# existing ``memory.session.{sanitize,runtime}_session_id`` call sites and back-compat.
from strandly_harness.core.session_ids import runtime_session_id, sanitize_session_id

# Explicit re-exports (sanitize_session_id is also used below; runtime_session_id is re-export-only).
__all__ = ["runtime_session_id", "sanitize_session_id"]

logger = logging.getLogger(__name__)


def _parse_session_message(raw: str) -> tuple[str, bool]:
    """Parse a Memory ``conversational.content.text`` into ``(text, has_tool_use)``.

    The Strands ``AgentCoreMemorySessionManager`` stores each message as a **JSON-encoded SDK
    ``SessionMessage``** in ``content.text`` — i.e. the text is double-wrapped::

        {"message": {"role": "assistant", "content": [{"text": "..."}, {"toolUse": {...}}]}, ...}

    Returns the concatenated text blocks plus whether the message carries a ``toolUse`` block. The
    ``toolUse`` flag is what distinguishes a mid-run *narration* ("Let me clone the repo…", emitted
    right before a tool call) from a terminal *answer* (text only, no tool call) — see
    :func:`conversation_settled`. Anything that isn't that shape (a plain string, a non-JSON value)
    is returned as ``(raw, False)``, so a reader over hand-written events still works.
    """
    try:
        content = json.loads(raw)["message"]["content"]
        text = "\n".join(b["text"] for b in content if isinstance(b, dict) and "text" in b)
        has_tool_use = any(isinstance(b, dict) and "toolUse" in b for b in content)
        return text, has_tool_use
    except (json.JSONDecodeError, KeyError, TypeError):
        return raw, False


def _unwrap_session_message(raw: str) -> str:
    """The plain text of a wrapped ``SessionMessage`` (see :func:`_parse_session_message`)."""
    return _parse_session_message(raw)[0]


def _events_to_messages(events: list[dict[str, Any]]) -> list[tuple[str, str, bool]]:
    """Parse AgentCore Memory ``ListEvents`` output into ordered ``(role, text, has_tool_use)``.

    ``events`` is the raw list from :meth:`MemoryClient.list_events` (``include_payload=True``).
    Each event carries a ``payload`` list of ``{"conversational": {"role", "content": {"text"}}}``
    items; text + a ``toolUse`` flag are parsed via :func:`_parse_session_message`. We sort by
    ``eventTimestamp`` defensively (the API returns chronological order, but we don't rely on it).
    """

    def _ts(ev: dict[str, Any]) -> Any:
        return ev.get("eventTimestamp") or 0

    out: list[tuple[str, str, bool]] = []
    for ev in sorted(events, key=_ts):
        for item in ev.get("payload", []) or []:
            conv = item.get("conversational") if isinstance(item, dict) else None
            if not conv:
                continue
            role = (conv.get("role") or "").lower()
            text, has_tool_use = _parse_session_message((conv.get("content") or {}).get("text", ""))
            out.append((role, text, has_tool_use))
    return out


def extract_conversation(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Parse ``ListEvents`` output into ordered ``(role, text)`` pairs (tool_use flag dropped)."""
    return [(role, text) for role, text, _ in _events_to_messages(events)]


def final_assistant_text(messages: list[tuple[str, str]]) -> str | None:
    """The most recent non-empty assistant message text, or ``None``."""
    for role, text in reversed(messages):
        if role == "assistant" and text.strip():
            return text
    return None


def conversation_settled(events: list[dict[str, Any]]) -> bool:
    """Tool_use-aware settle heuristic for fire-and-forget completion (no extra store).

    A run is "settled" only when its **last** conversational message (chronologically) is an
    assistant *answer*: non-empty text **and no** ``toolUse`` block. This is what the previous
    "any assistant message after the last user" rule got wrong — an agent narrates *before* each
    tool call ("Let me clone the repo…"), and that narration is itself an assistant message, so the
    old rule flipped to "completed" on the **first** narration and a recycled-instance poll returned
    intermediate narration as the result. A narration carries a ``toolUse`` block (a tool call is
    pending) → not settled; a tool result is a non-assistant message → not settled; only a terminal
    text-only assistant message counts. (The in-instance status sentinel is still preferred when
    reachable; this is the durable, cross-instance fallback.)

    **Known narrow window (goal loop):** with the actor-critic goal loop enabled, the critic runs
    *after* the actor's final text-only answer — between "actor produced text" and "critic says
    RETRY, resume prompt appended", the conversation *looks* settled. A cross-instance poll landing
    in that window (sentinel unreachable → this fallback) can return a premature ``completed`` with
    an answer the critic is about to reject. Same bug class as the narration case above, one level
    up; accepted for now because the window is seconds wide, the sentinel wins whenever session
    affinity holds, and a subsequent poll self-corrects once the critic's resume lands.
    """
    messages = _events_to_messages(events)
    if not messages:
        return False
    role, text, has_tool_use = messages[-1]
    return role == "assistant" and bool(text.strip()) and not has_tool_use


def read_events(
    config: Config, session_id: str, *, actor_id: str | None = None
) -> list[dict[str, Any]]:
    """Read the raw AgentCore Memory ``ListEvents`` output for a fire-and-forget session.

    Uses the data plane under the run's ``memory_id`` + ``actor_id`` + the *sanitized* session id
    (the same id the session manager wrote under). Returns ``[]`` if Memory isn't configured;
    network/SDK errors propagate to the caller to handle.

    ``list_events`` returns events **oldest-first** and truncates to ``max_results`` (default
    **100**), so we must request a high ceiling (:data:`MEMORY_MAX_EVENTS`) — otherwise the read
    keeps only the earliest 100 events and the FINAL assistant message of any longer run (routine
    for a minutes-to-hours task) is silently dropped, making the poll return a stale/partial result.
    """
    if not config.use_agentcore_session:
        return []
    from bedrock_agentcore.memory import MemoryClient

    client = MemoryClient(region_name=config.aws_region)
    return client.list_events(
        memory_id=config.memory_id,
        actor_id=actor_id or config.actor_id,
        session_id=sanitize_session_id(session_id),
        max_results=MEMORY_MAX_EVENTS,
        include_payload=True,
    )


def read_conversation(
    config: Config, session_id: str, *, actor_id: str | None = None
) -> list[tuple[str, str]]:
    """Read a Memory session back as ordered ``(role, text)`` pairs (the fire-and-forget channel)."""
    return extract_conversation(read_events(config, session_id, actor_id=actor_id))


def _session_id(ctx: RuntimeContext) -> str:
    if ctx.session_id:
        return sanitize_session_id(ctx.session_id)
    if ctx.session_key:
        return sanitize_session_id(ctx.session_key)
    return "session"

def build_session_manager(config: Config, ctx: RuntimeContext) -> Any | None:
    """AgentCore Memory session when configured (short-term), else a file session."""
    session_id = _session_id(ctx)

    if config.use_agentcore_session:
        from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AgentCoreMemorySessionManager,
        )

        actor_id = config.actor_id
        mem_config = AgentCoreMemoryConfig(
            memory_id=config.memory_id, session_id=session_id, actor_id=actor_id
        )
        return _resilient_session_manager(
            AgentCoreMemorySessionManager(mem_config, region_name=config.aws_region)
        )

    from strands.session.file_session_manager import FileSessionManager

    return FileSessionManager(session_id=session_id, storage_dir=SESSION_DIR)


# Methods the SDK invokes from agent-lifecycle hooks (MessageAdded/AfterInvocation/
# AgentInitialized). A raise in any of these propagates out and kills the turn — but short-term
# session persistence is a convenience, not load-bearing for producing an answer. We wrap them so a
# backend failure (e.g. an AgentCore Memory data-plane 403 in a region where it isn't serving)
# degrades to "no persistence this turn" with a warning, instead of crashing the run.
_GUARDED_SESSION_METHODS = (
    "append_message",
    "sync_agent",
    "initialize",
    "redact_latest_message",
    "sync_multi_agent",
    "initialize_multi_agent",
)


def _resilient_session_manager(manager: Any) -> Any:
    """Wrap a session manager so its hook-invoked methods log-and-continue instead of raising."""

    def guard(method: Any) -> Any:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            try:
                return method(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 — persistence is best-effort; never fail the turn
                logger.warning(
                    "session persistence failed in %s (%s); continuing without it for this turn",
                    getattr(method, "__name__", "session method"),
                    e,
                )
                return None

        return wrapped

    for name in _GUARDED_SESSION_METHODS:
        original = getattr(manager, name, None)
        if callable(original):
            setattr(manager, name, guard(original))
    return manager
