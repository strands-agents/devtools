"""Opt-in task-specific durable results; never infer a job's result from a session tail."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import Any

from strandly.core.config import Config
from strandly.core.session_ids import sanitize_session_id
from strandly.memory.session_store import build_storage

logger = logging.getLogger(__name__)
CANCEL_POLL_SECONDS = 1.0


def key(payload: dict[str, Any], *, cancel: bool = False) -> str:
    result_id = payload.get("resultId")
    session_id = payload.get("sessionId")
    if (
        not isinstance(result_id, str)
        or not re.fullmatch(r"[0-9a-f]{32}", result_id)
        or not isinstance(session_id, str)
        or not session_id
        or sanitize_session_id(session_id) != session_id
    ):
        raise ValueError("resultId requires a 32-character hex ID and a canonical sessionId")
    suffix = "cancel" if cancel else "result"
    return f"session/{session_id}/runs/{result_id}/{suffix}.json"


async def read(config: Config, payload: dict[str, Any]) -> dict[str, Any]:
    raw = await build_storage(config).read(key(payload))
    return json.loads(raw) if raw is not None else {"status": "unknown"}


async def write(config: Config, payload: dict[str, Any], result: dict[str, Any]) -> None:
    # The receipt contains only the final answer, never the internal event stream or exceptions.
    doc: dict[str, Any] = {"status": result["status"]}
    if result["status"] == "completed":
        doc["result"] = result.get("lastTurn", result.get("result", ""))
    await build_storage(config).write(key(payload), json.dumps(doc).encode())


async def request_cancel(config: Config, payload: dict[str, Any]) -> None:
    await build_storage(config).write(key(payload, cancel=True), b"true")


async def cancel_requested(config: Config, payload: dict[str, Any]) -> bool:
    return await build_storage(config).read(key(payload, cancel=True)) is not None


async def persist_terminal(config: Config, payload: dict[str, Any], result: dict[str, Any]) -> None:
    for attempt in range(3):
        try:
            await write(config, payload, result)
            return
        except Exception:  # noqa: BLE001 — bounded storage retry; final error propagates
            if attempt == 2:
                raise
            logger.warning("retrying terminal run receipt (attempt=%s)", attempt + 1)
            await asyncio.sleep(0.1 * (attempt + 1))


async def watch_cancel(config: Config, payload: dict[str, Any], signal: threading.Event) -> None:
    try:
        while not signal.is_set():
            if await cancel_requested(config, payload):
                signal.set()
                return
            await asyncio.sleep(CANCEL_POLL_SECONDS)
    except Exception:  # noqa: BLE001 — fail closed if cancellation intent cannot be observed
        logger.exception("run cancellation observation failed; stopping run")
        signal.set()
