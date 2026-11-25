"""The session store: one ``Storage`` per agent build, S3 when deployed, local files otherwise.

The same instance backs the snapshot session manager (``session/<sid>/``) and the context
manager's stash of offloaded originals (``context/<sid>/``, read by ``retrieve_context``), so both
survive restarts. ``STRANDLY_SESSION_BUCKET`` selects S3; a deployed runtime without it fails instead
of writing local files. Missing keys mean no data; every other storage error fails closed.
"""

from __future__ import annotations

import builtins
import logging
import os
import time
from functools import lru_cache
from typing import Any

from strandly.core.config import Config
from strandly.core.constants import (
    AGENTCORE_RUNTIME_MARKERS,
    LOCAL_SESSION_STORE_DIR,
    SESSION_STORE_MAX_ATTEMPTS,
    SESSION_STORE_SESSION_PREFIX,
)
from strandly.ops import metrics

logger = logging.getLogger(__name__)

#: The exact token on every session-storage failure log line (an alarm's metric filter keys on it).
SESSION_STORAGE_FAILURE = "SESSION_STORAGE_FAILURE"

_SESSION_KEY_PREFIX = f"{SESSION_STORE_SESSION_PREFIX}/"
_READ_OPS = frozenset({"read", "list", "search", "restore", "seed", "configure"})


class SessionStorageError(RuntimeError):
    """A session-store operation failed for a reason other than not-found; the turn must stop."""


def _is_session_key(key: str) -> bool:
    return key.lstrip("/").startswith(_SESSION_KEY_PREFIX)


def aws_error_code(error: BaseException) -> str:
    """The AWS error code behind ``error`` (walking ``__cause__``), else its type name."""
    seen: BaseException | None = error
    while seen is not None:
        response = getattr(seen, "response", None)
        if isinstance(response, dict):
            code = (response.get("Error") or {}).get("Code")
            if code:
                return str(code)
        seen = seen.__cause__
    return type(error).__name__


def storage_failure(op: str, key: str, backend: str, error: BaseException) -> SessionStorageError:
    """Log + count one session-storage failure and return the error to raise (fail closed)."""
    code = aws_error_code(error)
    logger.error(
        "%s op=%s backend=%s key=%s code=%s: %s",
        SESSION_STORAGE_FAILURE, op, backend, key, code, error,
    )
    metric = metrics.SESSION_READ_FAILED if op in _READ_OPS else metrics.SESSION_WRITE_FAILED
    metrics.emit({metric: 1}, surface=metrics.SURFACE_SESSION)
    return SessionStorageError(
        f"{SESSION_STORAGE_FAILURE}: session store {op} of {key!r} failed on {backend} ({code})"
    )


class FailClosedStorage:
    """A ``strands.storage.Storage`` wrapper: not-found passes through, every other error raises.

    Deliberately defines no ``namespace()``/``for_sandbox()``: the SDK then wraps it in its own
    namespaced view, so every key — fully prefixed — still flows through this wrapper. Not
    ephemeral, so the context manager treats the stash as durable.
    """

    def __init__(self, inner: Any, *, backend: str) -> None:
        self._inner = inner
        self.backend = backend

    @property
    def inner(self) -> Any:
        return self._inner

    def _failed(self, op: str, key: str, error: BaseException) -> SessionStorageError:
        return storage_failure(op, key, self.backend, error)

    async def write(self, key: str, data: bytes) -> None:
        started = time.monotonic()
        try:
            await self._inner.write(key, data)
        except Exception as e:
            raise self._failed("write", key, e) from e
        if _is_session_key(key):
            metrics.emit(
                {
                    metrics.SESSION_SNAPSHOT_BYTES: (len(data), metrics.BYTES),
                    metrics.SESSION_WRITE_MS: (
                        round((time.monotonic() - started) * 1000, 1),
                        metrics.MILLISECONDS,
                    ),
                },
                surface=metrics.SURFACE_SESSION,
            )

    async def read(self, key: str) -> bytes | None:
        try:
            data: bytes | None = await self._inner.read(key)  # None only for a missing key
        except Exception as e:
            raise self._failed("read", key, e) from e
        return data

    async def delete(self, key: str) -> None:
        try:
            await self._inner.delete(key)
        except Exception as e:
            raise self._failed("delete", key, e) from e

    async def list(self, query: str = "") -> builtins.list[str]:
        try:
            keys: builtins.list[str] = await self._inner.list(query)
        except Exception as e:
            raise self._failed("list", query, e) from e
        return keys

    async def search(self, query: Any) -> builtins.list[Any]:
        try:
            results: builtins.list[Any] = await self._inner.search(query)
        except Exception as e:
            raise self._failed("search", "<search>", e) from e
        return results


@lru_cache(maxsize=8)
def _s3_storage(bucket: str, profile: str | None, region: str | None) -> Any:
    """One ``S3Storage`` (and so one lazily-built S3 client) per bucket/credentials, per process."""
    from botocore.config import Config as BotocoreConfig
    from strands.storage import S3Storage

    from strandly.core.config import _session

    client_config = BotocoreConfig(
        retries={"max_attempts": SESSION_STORE_MAX_ATTEMPTS, "mode": "adaptive"}
    )
    if profile or region:
        return S3Storage(
            bucket, boto_session=_session(profile, region), boto_client_config=client_config
        )
    return S3Storage(bucket, boto_client_config=client_config)


def deployed_runtime_marker() -> str | None:
    """The env var proving this process is a deployed AgentCore Runtime, if any (else local)."""
    return next((name for name in AGENTCORE_RUNTIME_MARKERS if os.environ.get(name)), None)


def build_storage(config: Config) -> FailClosedStorage:
    """A fresh :class:`FailClosedStorage` for one agent build (S3 when a bucket is configured).

    Raises :class:`SessionStorageError` on a deployed runtime with no bucket: a session kept on the
    microVM's disk vanishes at the next cold start while the dashboard reads an empty bucket.
    """
    bucket = config.session_bucket
    if bucket:
        inner = _s3_storage(bucket, config.get("AWS_PROFILE"), config.aws_region)
        return FailClosedStorage(inner, backend=f"s3://{bucket}")
    marker = deployed_runtime_marker()
    if marker:
        raise storage_failure(
            "configure",
            "STRANDLY_SESSION_BUCKET",
            "local",
            RuntimeError(
                f"deployed runtime ({marker} is set) has no STRANDLY_SESSION_BUCKET; refusing to "
                "keep the session on ephemeral disk"
            ),
        )
    from strands.storage import LocalFileStorage

    # Read through the module attribute so the test suite can point it at a tmp dir.
    import strandly.memory.session_store as this

    return FailClosedStorage(LocalFileStorage(this.LOCAL_SESSION_STORE_DIR), backend="local")


def build_reader(config: Config) -> Any:
    """A strands-free :class:`~strandly.core.session_store_reader.SessionStoreReader`
    over the same backend :func:`build_storage` writes (bucket, else the local store dir)."""
    import strandly.memory.session_store as this
    from strandly.core.session_store_reader import SessionStoreReader

    bucket = config.session_bucket
    if bucket:
        return SessionStoreReader(bucket, boto_session=config.boto_session())
    return SessionStoreReader(local_dir=this.LOCAL_SESSION_STORE_DIR)


__all__ = [
    "LOCAL_SESSION_STORE_DIR",
    "SESSION_STORAGE_FAILURE",
    "FailClosedStorage",
    "SessionStorageError",
    "aws_error_code",
    "build_reader",
    "build_storage",
    "deployed_runtime_marker",
    "storage_failure",
]
