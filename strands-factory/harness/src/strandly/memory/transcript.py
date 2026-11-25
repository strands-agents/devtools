"""The verbatim transcript: one AgentCore Memory event per message.

The snapshot holds the managed history (summarized turns, truncated tool results); the dashboard and
reviewers need what was actually said. :class:`TranscriptRecorder` sends one ``CreateEvent`` per
``MessageAddedEvent`` to ``(memory_id, actor, session)``. Text-only user and assistant messages become
``conversational`` items, which the Memory's strategies extract from (reasoning rides along as
``blob`` items, never extracted); every other message is a ``json`` item sent with
``extractionMode=SKIP``. The SDK's ``ContextInjector`` writes ``injected`` blocks into the latest
user message after it was recorded, so after each model call those blocks are recorded too, as a
``context`` event the dashboard attaches to that message (metadata ``tid``, its tracking id). An
event too large for ``CreateEvent`` is stored whole in the session store and recorded as a preview
(see ``TRANSCRIPT_EVENT_OFFLOAD_BYTES``). Writes are retried within a budget and counted as
``TranscriptWriteFailed``; the turn never fails on them.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from strands.hooks import AfterModelCallEvent, HookOrder, MessageAddedEvent
from strands.plugins import Plugin
from strands.types.session import encode_bytes_values

from strandly.core.constants import (
    TRANSCRIPT_EVENT_MAX_ITEMS,
    TRANSCRIPT_EVENT_OFFLOAD_BYTES,
    TRANSCRIPT_ITEM_MAX_CHARS,
    TRANSCRIPT_PREVIEW_CLIP_CHARS,
    TRANSCRIPT_STRING_CLIP_CHARS,
    TRANSCRIPT_WRITE_ATTEMPTS,
    TRANSCRIPT_WRITE_BUDGET_SECONDS,
)
from strandly.core.session_ids import transcript_payload_key
from strandly.core.session_store_reader import actor_marker_key
from strandly.memory.scopes import memory_actor, memory_session_id
from strandly.memory.session_store import (
    SESSION_STORAGE_FAILURE,
    SessionStorageError,
    aws_error_code,
)
from strandly.ops import metrics

if TYPE_CHECKING:
    from strandly.core.config import Config
    from strandly.core.context import RuntimeContext

logger = logging.getLogger(__name__)

KIND_TEXT = "text"
KIND_MESSAGE = "message"
KIND_CONTEXT = "context"
_ROLES = {"user": "USER", "assistant": "ASSISTANT"}
_TRUNCATED = "[transcript: {n} more characters not recorded]"

# Errors worth another attempt: throttling, server-side and transport trouble. A validation or
# access error fails the same way every time, so it is reported at once.
_RETRYABLE_CODES = frozenset(
    {
        "ThrottlingException",
        "ThrottledException",
        "TooManyRequestsException",
        "ServiceQuotaExceededException",
        "InternalServerException",
        "InternalFailure",
        "ServiceUnavailableException",
        "ServiceUnavailable",
        "RequestTimeout",
        "RequestTimeoutException",
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "EndpointConnectionError",
        "ConnectionClosedError",
    }
)
_BACKOFF_SECONDS = (0.2, 0.6, 1.2)


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _cut(text: str) -> list[str]:
    """``text`` as at most TRANSCRIPT_EVENT_MAX_ITEMS item-sized strings, marked if it was cut."""
    budget = TRANSCRIPT_ITEM_MAX_CHARS * TRANSCRIPT_EVENT_MAX_ITEMS
    if len(text) > budget:
        keep = budget - 64  # room for the marker, which is always shorter than 64 characters
        text = text[:keep] + "\n" + _TRUNCATED.format(n=len(text) - keep)
    return _chunks(text, TRANSCRIPT_ITEM_MAX_CHARS)


def _is_text_block(block: Any) -> bool:
    return isinstance(block, dict) and set(block) == {"text"} and isinstance(block["text"], str)


def _is_reasoning_block(block: Any) -> bool:
    return isinstance(block, dict) and set(block) == {"reasoningContent"}


def _is_conversation(message: dict[str, Any]) -> bool:
    """User/assistant text, optionally with reasoning blocks, and some non-empty text."""
    content = message.get("content")
    if message.get("role") not in _ROLES or not isinstance(content, list) or not content:
        return False
    if not all(_is_text_block(b) or _is_reasoning_block(b) for b in content):
        return False
    return any(_is_text_block(b) and b["text"] for b in content)


def _conversation_text(message: dict[str, Any]) -> str | None:
    """The text a conversational item records, or ``None`` when the message is not conversation."""
    if not _is_conversation(message):
        return None
    texts = [b["text"] for b in message["content"] if _is_text_block(b)]
    return "\n".join(texts) if any(texts) else None


def _clip_strings(value: Any, limit: int = TRANSCRIPT_STRING_CLIP_CHARS) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + " " + _TRUNCATED.format(n=len(value) - limit)
    if isinstance(value, dict):
        return {k: _clip_strings(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip_strings(v, limit) for v in value]
    return value


def _message_items(message: dict[str, Any], member: str) -> list[dict[str, Any]]:
    """The message, verbatim, as ``json`` or ``blob`` payload items."""
    encoded = encode_bytes_values(dict(message))
    doc: dict[str, Any] = {"message": encoded}
    raw = json.dumps(doc, ensure_ascii=False)
    budget = TRANSCRIPT_ITEM_MAX_CHARS * TRANSCRIPT_EVENT_MAX_ITEMS
    if len(raw) > budget:
        doc = {"message": _clip_strings(encoded), "truncated": len(raw)}
        raw = json.dumps(doc, ensure_ascii=False)
    if len(raw) > budget:
        # Still too big (a very large number of blocks): keep the role and say so.
        doc = {
            "message": {
                "role": message.get("role"),
                "content": [{"text": _TRUNCATED.format(n=len(raw))}],
            },
            "truncated": len(raw),
        }
        raw = json.dumps(doc, ensure_ascii=False)
    if len(raw) <= TRANSCRIPT_ITEM_MAX_CHARS:
        docs = [doc]
    else:
        parts = _chunks(raw, TRANSCRIPT_ITEM_MAX_CHARS)
        docs = [{"part": i, "parts": len(parts), "data": chunk} for i, chunk in enumerate(parts)]
    # A json item wraps its value in {"content": …}; a blob item is the value itself.
    if member == "json":
        return [{"json": {"content": d}} for d in docs]
    return [{"blob": d} for d in docs]


def transcript_event(message: dict[str, Any], *, seq: str, kind: str | None = None) -> dict[str, Any]:
    """The ``CreateEvent`` fields (``payload``, ``metadata``, ``extractionMode``) for one message.

    ``kind`` overrides the derived ``kind`` metadata (``context`` for recorded injected blocks).
    """
    role = str(message.get("role") or "")
    text = _conversation_text(message)
    if text is not None:
        blocks = message["content"]
        payload = [
            {"conversational": {"content": {"text": chunk}, "role": _ROLES[role]}}
            for chunk in _cut(text)
        ]
        if not all(_is_text_block(b) for b in blocks):
            payload += _message_items(message, "blob")
        derived, skip = KIND_TEXT, False
    else:
        payload, derived, skip = _message_items(message, "json"), KIND_MESSAGE, True
    metadata: dict[str, Any] = {
        "seq": {"stringValue": seq},
        "kind": {"stringValue": kind or derived},
        "role": {"stringValue": role[:64] or "unknown"},
    }
    tid = message.get("tracking_id")
    if isinstance(tid, str) and 0 < len(tid) <= 128:
        metadata["tid"] = {"stringValue": tid}
    fields: dict[str, Any] = {"payload": payload, "metadata": metadata}
    if skip:
        fields["extractionMode"] = "SKIP"
    return fields


def payload_bytes(payload: Any) -> int:
    """An upper bound on the payload's size as ``CreateEvent`` measures it (ASCII-escaped JSON)."""
    return len(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"))


def preview_event(
    message: dict[str, Any], *, seq: str, kind: str | None, stored_bytes: int | None
) -> dict[str, Any]:
    """A stand-in for an event too large for ``CreateEvent``: ``message`` with clipped strings.

    ``stored_bytes`` is the full payload's size when it was stored at
    :func:`~strandly.core.session_ids.transcript_payload_key`, recorded as metadata ``offloaded``.
    """
    clipped = _clip_strings(encode_bytes_values(dict(message)), TRANSCRIPT_PREVIEW_CLIP_CHARS)
    fields = transcript_event(clipped, seq=seq, kind=kind)
    if payload_bytes(fields["payload"]) > TRANSCRIPT_EVENT_OFFLOAD_BYTES:
        # Still too big (a very large number of blocks): keep the role and say so.
        stub: dict[str, Any] = {
            "role": message.get("role"),
            "content": [{"text": _TRUNCATED.format(n=payload_bytes(message["content"]))}],
        }
        if message.get("tracking_id"):
            stub["tracking_id"] = message["tracking_id"]
        fields = transcript_event(stub, seq=seq, kind=kind or KIND_MESSAGE)
        fields["extractionMode"] = "SKIP"
    if stored_bytes is not None:
        fields["metadata"]["offloaded"] = {"stringValue": str(stored_bytes)}
    return fields


def _item_doc(item: Any) -> Any:
    if not isinstance(item, dict):
        return None
    if "blob" in item:
        return item["blob"]
    return (item.get("json") or {}).get("content")


def event_message(event: dict[str, Any]) -> dict[str, Any] | None:
    """The message a transcript event carries (the inverse of :func:`transcript_event`).

    The verbatim message (``json`` or ``blob`` items) wins over the conversational text.
    """
    payload = event.get("payload") or []
    parts: dict[int, str] = {}
    for item in payload:
        doc = _item_doc(item)
        if isinstance(doc, dict) and isinstance(doc.get("message"), dict):
            return dict(doc["message"])
        if isinstance(doc, dict) and isinstance(doc.get("data"), str):
            parts[int(doc.get("part", len(parts)))] = doc["data"]
    if parts:
        try:
            whole = json.loads("".join(parts[i] for i in sorted(parts)))
        except ValueError:
            return None
        message = whole.get("message") if isinstance(whole, dict) else None
        return dict(message) if isinstance(message, dict) else None
    texts: list[str] = []
    role = ""
    for item in payload:
        conv = item.get("conversational") if isinstance(item, dict) else None
        if isinstance(conv, dict):
            role = role or str(conv.get("role") or "").lower()
            texts.append(str((conv.get("content") or {}).get("text") or ""))
    if not texts:
        return None
    return {"role": role, "content": [{"text": "".join(texts)}]}


def _retryable(error: BaseException) -> bool:
    return aws_error_code(error) in _RETRYABLE_CODES


@lru_cache(maxsize=8)
def _client(profile: str | None, region: str | None) -> Any:
    """One ``bedrock-agentcore`` client per process: short timeouts, no SDK retries (ours bound it)."""
    from botocore.config import Config as BotocoreConfig

    from strandly.core.config import _session

    client_config = BotocoreConfig(
        connect_timeout=1, read_timeout=3, retries={"max_attempts": 1, "mode": "standard"}
    )
    return _session(profile, region).client("bedrock-agentcore", config=client_config)


class TranscriptRecorder(Plugin):
    """Write every message added to the agent as one AgentCore Memory event (see module doc)."""

    name = "strandly:transcript"

    def __init__(
        self,
        client: Any,
        *,
        memory_id: str,
        actor_id: str,
        session_id: str,
        storage: Any | None = None,
    ) -> None:
        super().__init__()
        self._client = client
        self._memory_id = memory_id
        self._actor_id = actor_id
        self._store_session_id = session_id
        self._session_id = memory_session_id(session_id)
        self._storage = storage
        self._counter = itertools.count(1)
        self._marked = storage is None
        # (tracking id, source) of each injected block already recorded by this recorder.
        self._injected: set[tuple[str, str]] = set()

    @property
    def actor_id(self) -> str:
        return self._actor_id

    @property
    def memory_session_id(self) -> str:
        return self._session_id

    def init_agent(self, agent: Any) -> None:
        agent.add_hook(self._on_message_added, MessageAddedEvent, order=HookOrder.SDK_FIRST)
        agent.add_hook(self._on_model_called, AfterModelCallEvent)

    def next_seq(self) -> str:
        return f"{time.time_ns():020d}-{next(self._counter):06d}"

    async def _on_message_added(self, event: MessageAddedEvent) -> None:
        await self._record(dict(event.message))

    async def _on_model_called(self, event: AfterModelCallEvent) -> None:
        """Record the ``injected`` blocks the latest user message gained since it was recorded.

        They are written as model input, after the message was added, and never change after;
        retries and later calls on the same message find nothing new.
        """
        message = next((m for m in reversed(event.agent.messages) if m.get("role") == "user"), None)
        if message is None:
            return
        tid = str(message.get("tracking_id") or "")
        fresh = []
        for block in message.get("content") or []:
            injected = block.get("injected") if isinstance(block, dict) else None
            if not isinstance(injected, dict):
                continue
            key = (tid or str(injected.get("text")), str(injected.get("source")))
            if key not in self._injected:
                self._injected.add(key)
                fresh.append(block)
        if fresh:
            await self._record(dict(message), injected=fresh)

    async def _record(self, message: dict[str, Any], *, injected: list[Any] | None = None) -> None:
        landed = datetime.now(timezone.utc)
        seq = self.next_seq()
        await self._mark()
        kind = KIND_CONTEXT if injected is not None else None
        try:
            if injected is not None:
                message = {**message, "content": injected}
            fields = transcript_event(message, seq=seq, kind=kind)
            size = payload_bytes(fields["payload"])
            if size > TRANSCRIPT_EVENT_OFFLOAD_BYTES:
                stored = await self._offload(fields["payload"], seq)
                fields = preview_event(message, seq=seq, kind=kind, stored_bytes=size if stored else None)
        except Exception as e:  # noqa: BLE001 — a record that cannot be built is reported, not raised
            self._failed(e, seq)
            return
        request = {
            "memoryId": self._memory_id,
            "actorId": self._actor_id,
            "sessionId": self._session_id,
            "eventTimestamp": landed,
            **fields,
        }
        await self._send(request, seq)

    async def _offload(self, payload: list[Any], seq: str) -> bool:
        """Store an oversized event's full payload in the session store; whether it was stored."""
        if self._storage is None:
            logger.warning("transcript event seq=%s too large and no session store: preview only", seq)
            return False
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        key = transcript_payload_key(self._store_session_id, seq)
        try:
            await self._storage.write(key, body)
        except SessionStorageError:
            return False  # already logged + counted by the store; the preview still records it
        logger.info("transcript event seq=%s offloaded: bytes=%d key=%s", seq, len(body), key)
        return True

    async def _mark(self) -> None:
        """Persist which actor this session's events are under (once per recorder)."""
        if self._marked or self._storage is None:
            return
        body = json.dumps({"actor": self._actor_id, "sessionId": self._session_id}).encode()
        try:
            await self._storage.write(actor_marker_key(self._store_session_id), body)
        except SessionStorageError:
            return  # already logged + counted by the store; retried on the next message
        self._marked = True

    async def _send(self, request: dict[str, Any], seq: str) -> None:
        deadline = time.monotonic() + TRANSCRIPT_WRITE_BUDGET_SECONDS
        for attempt in range(TRANSCRIPT_WRITE_ATTEMPTS):
            try:
                await asyncio.to_thread(self._client.create_event, **request)
                return
            except Exception as e:  # noqa: BLE001 — every failure ends in _failed, never in the turn
                last = attempt == TRANSCRIPT_WRITE_ATTEMPTS - 1
                pause = _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]
                if last or not _retryable(e) or time.monotonic() + pause >= deadline:
                    self._failed(e, seq)
                    return
                await asyncio.sleep(pause)

    def _failed(self, error: BaseException, seq: str) -> None:
        logger.error(
            "%s op=transcript backend=agentcore-memory:%s key=%s/%s seq=%s code=%s: %s",
            SESSION_STORAGE_FAILURE,
            self._memory_id,
            self._actor_id,
            self._session_id,
            seq,
            aws_error_code(error),
            error,
        )
        metrics.emit({metrics.TRANSCRIPT_WRITE_FAILED: 1}, surface=metrics.SURFACE_SESSION)


def build_transcript_recorder(
    config: Config, ctx: RuntimeContext, session_id: str, storage: Any | None
) -> TranscriptRecorder | None:
    """The recorder for a top-level run, or ``None`` without a Memory (``AGENTCORE_MEMORY_ID``)."""
    memory_id = config.memory_id
    if not memory_id:
        return None
    client = _client(config.get("AWS_PROFILE"), config.aws_region)
    return TranscriptRecorder(
        client,
        memory_id=memory_id,
        actor_id=memory_actor(ctx),
        session_id=session_id,
        storage=storage,
    )
