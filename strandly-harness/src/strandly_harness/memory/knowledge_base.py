"""Long-term memory (cross-conversation): a ``MemoryManager`` over a writable Bedrock KB.

Enabled only when ``STRANDLY_KB_ID`` + ``STRANDLY_KB_DATA_SOURCE_ID`` are set. It gives the agent
``search_memory`` + ``add_memory`` tools and injects relevant memories before each turn — so the
agent can record (and later recall) code facts, procedures, preferences, and past mistakes across
sessions. Every ``add_memory`` write is instrumented (structured log + EMF metric) so memory
poisoning can't go unobserved.
"""

from __future__ import annotations

import functools
import logging
from typing import Any

from strandly_harness.core.config import Config
from strandly_harness.core.context import RuntimeContext
from strandly_harness.ops import metrics

logger = logging.getLogger(__name__)

def build_memory_manager(config: Config, ctx: RuntimeContext | None = None) -> Any | None:
    """Long-term memory plugin (``MemoryManager``) over a writable Bedrock KB, or ``None``.

    Returns ``None`` unless both ``STRANDLY_KB_ID`` and ``STRANDLY_KB_DATA_SOURCE_ID`` are set —
    long-term memory is a gated capability like the others. When on, the agent gets ``search_memory``
    + ``add_memory`` tools and relevant memories are injected before each model call.

    The returned manager's ``add_memory`` tool is wrapped (:func:`_instrument_add_memory`) so every
    long-term KB write emits a structured log line + an EMF metric — the one ingestion path that was
    previously neither logged nor metered (so memory poisoning would go unobserved). ``ctx`` is
    threaded only to tag those records with the session id.
    """
    if not config.use_long_term_memory:
        return None

    from strands.memory import MemoryManager
    from strands.vended_memory_stores.bedrock_knowledge_base import BedrockKnowledgeBaseStore

    # Build the KB clients in the harness's region. Without this the store falls back to boto3's
    # ambient region (often AWS_DEFAULT_REGION), which can point at a different region than the KB
    # lives in — yielding a confusing ResourceNotFoundException on every search/add.
    session = config.boto_session()
    import boto3

    session = session or boto3.Session(region_name=config.aws_region)
    kb_config: dict[str, Any] = {
        "knowledge_base_id": config.kb_id,
        "data_source_type": "CUSTOM",
        "data_source_id": config.kb_data_source_id,
        "runtime_client": session.client("bedrock-agent-runtime"),
        "agent_client": session.client("bedrock-agent"),
    }
    store = BedrockKnowledgeBaseStore(config=kb_config, name="strandly-memory", writable=True)
    # add_tool_config=True exposes add_memory (default is read-only); injection on by default.
    manager = MemoryManager(stores=[store], add_tool_config=True)
    return _instrument_add_memory(manager, config, ctx)


# How many characters of a memory entry we surface as a preview in the structured write log. A short
# prefix is enough to tell *what kind* of fact was written (and to eyeball obvious poisoning) without
# dumping full — possibly secret/PII — memory content into CloudWatch logs.
MEMORY_WRITE_PREVIEW_CHARS = 80


def _memory_write_preview(entries: Any) -> str:
    """A short, single-line, length-capped preview of the first entry — never the full content.

    We log a preview (not the whole entry) so a write is *observable* — you can tell roughly what
    kind of fact landed and spot blatant poisoning — without persisting full, possibly secret/PII,
    memory content into the logs.
    """
    if not entries:
        return ""
    first = entries[0] if isinstance(entries, (list, tuple)) else entries
    text = " ".join(str(first).split())  # collapse whitespace/newlines to a single line
    if len(text) > MEMORY_WRITE_PREVIEW_CHARS:
        return text[:MEMORY_WRITE_PREVIEW_CHARS] + "…"
    return text


def _record_memory_write(
    *,
    success: bool,
    entries: Any,
    actor_id: str | None,
    session_id: str | None,
    error: BaseException | None = None,
) -> None:
    """Log + emit-metric for one ``add_memory`` call. Fully fail-soft.

    Records, at INFO on success / WARNING on failure, *that* a long-term memory write happened — its
    entry count, total content length, a short non-sensitive preview, and the actor/session id — and
    emits ``MemoryWrite`` / ``MemoryWriteFailed`` (a no-op when metrics are disabled). Never raises
    and never logs full entry content, so observability can neither break the write nor leak secrets.
    """
    try:
        items = list(entries) if isinstance(entries, (list, tuple)) else ([entries] if entries else [])
        count = len(items)
        total_len = sum(len(str(item)) for item in items)
        preview = _memory_write_preview(items)
        if success:
            logger.info(
                "add_memory write ok: entries=%d total_len=%d actor_id=%s session_id=%s preview=%r",
                count,
                total_len,
                actor_id,
                session_id,
                preview,
            )
            metrics.emit({metrics.MEMORY_WRITE: 1}, surface=metrics.SURFACE_MEMORY)
        else:
            logger.warning(
                "add_memory write FAILED: entries=%d total_len=%d actor_id=%s session_id=%s "
                "preview=%r cause=%s",
                count,
                total_len,
                actor_id,
                session_id,
                preview,
                error,
            )
            metrics.emit({metrics.MEMORY_WRITE_FAILED: 1}, surface=metrics.SURFACE_MEMORY)
    except Exception:  # noqa: BLE001 — observability must never disrupt (or alter) the write
        logger.debug("add_memory instrumentation failed; continuing", exc_info=True)


def _instrument_add_memory(
    manager: Any, config: Config, ctx: RuntimeContext | None = None
) -> Any:
    """Wrap the manager's ``add_memory`` tool with structured logging + an EMF metric.

    Long-term KB writes were the one harness ingestion path with no log and no metric — so memory
    poisoning would go unobserved (the poller, runs, write-audit and stuck-run detector all already
    emit). We replace the SDK ``add_memory`` tool's underlying coroutine with a thin wrapper that,
    once per call, records the outcome via :func:`_record_memory_write` and emits the metric.

    Fail-soft on two axes: (1) the instrumentation itself can never raise or change the write's
    result (logging/metrics are swallowed); (2) a genuine write failure is logged + metered and then
    **re-raised unchanged** — we deliberately do not mask it, since silently swallowing a failed
    write would make the agent believe a fact was stored when it wasn't. If the add tool is absent
    (disabled) or the SDK shape changes, we return the manager untouched.
    """
    add_tool = next(
        (t for t in getattr(manager, "tools", []) if getattr(t, "tool_name", None) == "add_memory"),
        None,
    )
    if add_tool is None or not hasattr(add_tool, "_tool_func"):
        return manager  # nothing to wrap — fail-soft

    original = add_tool._tool_func
    actor_id = getattr(config, "actor_id", None)
    session_id = ctx.session_id if ctx is not None else None

    @functools.wraps(original)
    async def instrumented(*args: Any, **kwargs: Any) -> Any:
        entries = kwargs.get("entries")
        if entries is None and args:
            entries = args[0]
        try:
            result = await original(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — observe then re-raise unchanged
            _record_memory_write(
                success=False, entries=entries, actor_id=actor_id, session_id=session_id, error=exc
            )
            raise
        _record_memory_write(
            success=True, entries=entries, actor_id=actor_id, session_id=session_id
        )
        return result

    add_tool._tool_func = instrumented
    return manager

