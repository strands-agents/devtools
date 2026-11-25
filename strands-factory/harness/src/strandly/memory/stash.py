"""The context stash as ``retrieve_context`` reads it: JSON tool output with its real lines.

A tool that returns a dict (``shell``: ``{"output", "error", "exit_code"}``) lands as one text
block of JSON, so its stashed original is a single line and ``line_range``/``pattern`` cannot
slice it. :class:`ReadableStash` wraps the agent's storage and, on read only, renders such a block
with each multi-line string's lines as real lines. Stored objects are never rewritten, so every
existing reference reads, and slices, the same way.
"""

from __future__ import annotations

import builtins
import json
from typing import Any

from strandly.core.constants import SESSION_STORE_CONTEXT_PREFIX

_STASH_KEY_PREFIX = f"{SESSION_STORE_CONTEXT_PREFIX}/"


def readable_text(text: str) -> str | None:
    """``text`` re-rendered with real lines when it is a JSON object holding multi-line strings.

    Multi-line fields come first, each as ``<key>:`` then its lines (so line N of a lone field such
    as ``output`` is line N + 1), then every other field as ``<key>: <value>``. ``None`` when the
    text is anything else.
    """
    if not text.lstrip().startswith("{"):
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    multi = {k: v for k, v in doc.items() if isinstance(v, str) and "\n" in v}
    if not multi:
        return None
    lines: list[str] = []
    for key, value in multi.items():
        lines += [f"{key}:", value.removesuffix("\n")]
    for key, value in doc.items():
        if key not in multi:
            lines.append(f"{key}: {value if isinstance(value, str) else json.dumps(value)}")
    return "\n".join(lines)


def readable_block(data: bytes) -> bytes:
    """A stashed block's bytes, with a JSON-text block rendered by :func:`readable_text`."""
    try:
        block = json.loads(data)
    except ValueError:
        return data
    if not (isinstance(block, dict) and set(block) == {"text"} and isinstance(block["text"], str)):
        return data
    rendered = readable_text(block["text"])
    return data if rendered is None else json.dumps({"text": rendered}).encode("utf-8")


class ReadableStash:
    """A ``strands.storage.Storage`` wrapper that renders stash reads with :func:`readable_block`.

    Defines no ``namespace()``, so the SDK wraps it in its own namespaced view and every stash key
    reaches it fully prefixed. Ephemeral exactly when the wrapped storage is, so the context
    manager's durability check sees through it.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        ephemeral = getattr(inner, "_ephemeral", None)
        if ephemeral is not None:
            self._ephemeral = ephemeral

    @property
    def inner(self) -> Any:
        return self._inner

    async def write(self, key: str, data: bytes) -> None:
        await self._inner.write(key, data)

    async def read(self, key: str) -> bytes | None:
        data: bytes | None = await self._inner.read(key)
        if data is None or not key.lstrip("/").startswith(_STASH_KEY_PREFIX):
            return data
        return readable_block(data)

    async def delete(self, key: str) -> None:
        await self._inner.delete(key)

    async def list(self, query: str = "") -> builtins.list[str]:
        keys: builtins.list[str] = await self._inner.list(query)
        return keys

    async def search(self, query: Any) -> builtins.list[Any]:
        results: builtins.list[Any] = await self._inner.search(query)
        return results
