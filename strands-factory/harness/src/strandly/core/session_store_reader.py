"""Read a session's messages back out of the session store, without the Strands SDK.

The SDK's ``SnapshotSessionManager`` writes one whole-agent snapshot per session
(``memory/session.py``); this module reads it with only the standard library and boto3, for the
runtime's poll fallback and the strands-free Lambdas.
"""

from __future__ import annotations

import builtins
import logging
from pathlib import Path
from typing import Any

from strandly.core.constants import (  # noqa: F401 — re-exported for callers
    LOCAL_SESSION_STORE_DIR,
    SESSION_STORE_ACTOR_MARKER,
    SESSION_STORE_AGENT_ID,
    SESSION_STORE_LATEST_SNAPSHOT,
    SESSION_STORE_SESSION_PREFIX,
)
from strandly.core.session_ids import (
    actor_marker_key as actor_marker_key,
)
from strandly.core.session_ids import sanitize_session_id as sanitize_session_id
from strandly.core.session_ids import (
    snapshot_key as snapshot_key,
)
from strandly.core.session_ids import (
    snapshot_messages,
)
from strandly.core.session_ids import (
    store_session_id as store_session_id,
)

logger = logging.getLogger(__name__)

# A conversation as the poll heuristic sees it: ordered ``(role, text, has_tool_use)``.
Conversation = list[tuple[str, str, bool]]


def message_text(message: Any) -> tuple[str, str, bool]:
    """``(role, text, has_tool_use)`` for one SDK message dict (``{"role", "content": [...]}``).

    Text blocks are joined; a ``toolUse`` block flags a pending tool call — the distinction the
    settle heuristic needs between a mid-run *narration* and a terminal *answer*. Anything that
    isn't that shape yields ``("", "", False)``.
    """
    if not isinstance(message, dict):
        return "", "", False
    role = str(message.get("role") or "").lower()
    content = message.get("content")
    if not isinstance(content, list):
        return role, "", False
    text = "\n".join(
        str(b["text"]) for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)
    )
    has_tool_use = any(isinstance(b, dict) and "toolUse" in b for b in content)
    return role, text, has_tool_use


def conversation_from_messages(messages: list[Any]) -> Conversation:
    """SDK messages → the ordered ``(role, text, has_tool_use)`` view (see :func:`message_text`)."""
    return [message_text(m) for m in messages]


def settled(conversation: Conversation) -> bool:
    """Tool_use-aware settle heuristic for fire-and-forget completion.

    A run is "settled" only when its **last** message is an assistant *answer*: non-empty text and
    **no** ``toolUse`` block. An agent narrates before each tool call ("Let me clone the repo…") —
    that narration is an assistant message carrying a ``toolUse``, so it is not settled; a tool
    result is a user-role message, so not settled either. The in-instance status sentinel is
    preferred when reachable; this is the durable, cross-instance fallback.

    **Known narrow window (goal loop):** the critic runs *after* the actor's final text-only answer,
    so between "actor answered" and "critic says RETRY" the conversation looks settled. Accepted —
    the window is seconds wide, the sentinel wins whenever session affinity holds, and a later poll
    self-corrects once the critic's resume lands.
    """
    if not conversation:
        return False
    role, text, has_tool_use = conversation[-1]
    return role == "assistant" and bool(text.strip()) and not has_tool_use


def final_assistant_text(conversation: list[tuple[Any, ...]]) -> str | None:
    """The most recent non-empty assistant text, or ``None`` (accepts 2- or 3-tuples)."""
    for entry in reversed(conversation):
        role, text = entry[0], entry[1]
        if role == "assistant" and text.strip():
            return str(text)
    return None


class SessionStoreReader:
    """Read snapshots and transcripts from the S3 session bucket, or a local store directory.

    ``bucket`` selects S3 (``client`` injectable for tests; else a boto3 client from ``region`` /
    ``boto_session``); with no bucket it reads ``local_dir`` (default ``LOCAL_SESSION_STORE_DIR``),
    the same root ``LocalFileStorage`` writes. Missing data is ``None``; any other failure raises
    so the caller decides how to degrade (every caller today falls back, never fails the request).
    """

    def __init__(
        self,
        bucket: str | None = None,
        *,
        region: str | None = None,
        boto_session: Any | None = None,
        client: Any | None = None,
        local_dir: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._region = region
        self._boto_session = boto_session
        self._client = client
        self._local_dir = Path(local_dir or LOCAL_SESSION_STORE_DIR)

    # -- raw access --------------------------------------------------------------------------

    def _s3(self) -> Any:
        if self._client is None:
            if self._boto_session is not None:
                self._client = self._boto_session.client("s3")
            else:
                import boto3

                self._client = boto3.client("s3", region_name=self._region)
        return self._client

    def read(self, key: str) -> bytes | None:
        """The bytes stored under ``key``, or ``None`` when absent."""
        if not self._bucket:
            path = self._local_dir / key
            return path.read_bytes() if path.is_file() else None
        client = self._s3()
        try:
            resp = client.get_object(Bucket=self._bucket, Key=key)
        except Exception as e:  # noqa: BLE001 — only "missing" is mapped; everything else re-raises
            code = getattr(e, "response", {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise
        body: bytes = resp["Body"].read()
        return body

    def snapshot_messages(self, session_id: str) -> builtins.list[dict[str, Any]] | None:
        """The latest snapshot's ``data.messages`` (SDK message dicts), or ``None`` if no snapshot."""
        raw = self.read(snapshot_key(session_id))
        if raw is None:
            return None
        return [m for m in snapshot_messages(raw) if isinstance(m, dict)]

    def conversation(self, session_id: str) -> Conversation | None:
        """The session as ``(role, text, has_tool_use)`` from its latest snapshot, or ``None``."""
        messages = self.snapshot_messages(session_id)
        return None if messages is None else conversation_from_messages(messages)
