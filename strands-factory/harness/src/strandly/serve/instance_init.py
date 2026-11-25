"""Per-instance runtime state, resolved after the AgentCore Runtime V2 snapshot restore.

V2 starts every session from a snapshot of the process taken after startup, so anything computed at
import (config secret values, AWS clients and credentials, the random seed, monotonic baselines)
would be cloned into every instance. The entrypoint passes ``Config.load`` to ``build_app`` and
:class:`InstanceInit` runs it, with the client wiring, once per instance on the first request. On V1
this only moves the work from import to the first request.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from collections.abc import Callable
from typing import Any, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def reseed_process_randomness() -> None:
    """Re-seed the process-global ``random`` generator from ``os.urandom``.

    Python re-seeds ``random`` after ``os.fork`` but has no hook for a VM snapshot restore, so a
    clone keeps the parent's generator state.
    """
    random.seed()


class ReseedRandomnessMiddleware:
    """Pure ASGI middleware: re-seed ``random`` at the start of every HTTP request.

    ``build_app`` adds it LAST, so it is the outermost user middleware: Starlette's
    ``add_middleware`` inserts at the front, and OpenTelemetry's Starlette instrumentation adds its
    server-span middleware when the app is constructed, i.e. earlier, so inside this one. The
    reseed therefore runs before the request's server span draws its id (OTel and X-Ray id
    generators use ``random.getrandbits``) and before ``InstanceInit`` reads the config secret
    (botocore's retry jitter uses ``random`` too). Every request, not only the first, so the order
    does not depend on the platform never sending a request (a health ping, a warm-up invoke)
    before it takes the snapshot. ``random.seed()`` is one small ``os.urandom`` read.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket"):
            reseed_process_randomness()
        await self.app(scope, receive, send)


class InstanceInit(Generic[T]):
    """Run ``wire(load())`` once per process, lazily when ``source`` is a loader.

    Args:
        source: A loaded value (wired immediately — the test/local path, identical to eager
            construction) or a zero-argument loader (wired on the first :meth:`ensure_ready`).
        wire: Binds the loaded value into the caller's state (builds clients, configures metrics).
    """

    def __init__(self, source: T | Callable[[], T], wire: Callable[[T], None]) -> None:
        self._wire = wire
        self._lock = threading.Lock()
        self._ready = False
        self._loader: Callable[[], T] | None = None
        if callable(source):
            self._loader = source
        else:
            reseed_process_randomness()
            self._apply(source)

    @property
    def ready(self) -> bool:
        return self._ready

    def _apply(self, value: T) -> None:
        self._wire(value)
        self._ready = True

    def ensure_ready_sync(self) -> None:
        """Load + wire once (thread-safe). Re-raises a failed load; the next call retries."""
        if self._ready:
            return
        with self._lock:
            if self._ready or self._loader is None:
                return
            started = time.time()
            logger.info("instance init: start (loading config after snapshot restore)")
            try:
                # Before the loader: Config.load's Secrets Manager read retries with botocore's
                # jitter, which draws from ``random``. (The HTTP path has already reseeded in
                # ReseedRandomnessMiddleware; this covers every other caller.)
                reseed_process_randomness()
                self._apply(self._loader())
            except Exception:
                logger.exception("instance init: failed; the next request will retry")
                raise
            logger.info("instance init: done in %d ms", int((time.time() - started) * 1000))

    async def ensure_ready(self) -> None:
        """Async :meth:`ensure_ready_sync`: the blocking secret read runs off the event loop."""
        if self._ready:
            return
        await asyncio.to_thread(self.ensure_ready_sync)
