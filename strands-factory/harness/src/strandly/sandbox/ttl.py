"""Sliding session TTL for sandbox sessions, through EventBridge Scheduler.

Runtime-instance sessions have no TTL: ``MaxLifetime`` only stops them and their EBS volume keeps
billing, and nothing lists sessions. So every use of a session upserts a one-time schedule, named
deterministically per session so it slides, that calls ``DeleteCapacityProviderSession``
:data:`SANDBOX_SESSION_TTL_SECONDS` later through Scheduler's universal target (no Lambda). Upserts
are throttled per session, run on a background pool off the critical path, and fail soft (counted
as ``SandboxTtlUpsertFailed``). The client and pool are built lazily, so a runtime snapshot carries
neither. Standard library and boto3 only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as _wait_futures
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from strandly.core.constants import (
    SANDBOX_EPHEMERAL_SESSION_TTL_SECONDS,
    SANDBOX_SESSION_TTL_SECONDS,
    SANDBOX_TTL_BUMP_INTERVAL_SECONDS,
    SANDBOX_TTL_FAILURE_RETRY_SECONDS,
    SANDBOX_TTL_RETRY_ATTEMPTS,
    SANDBOX_TTL_RETRY_MAX_AGE_SECONDS,
    SANDBOX_TTL_SCHEDULE_HASH_CHARS,
    SANDBOX_TTL_SCHEDULE_PREFIX,
    SANDBOX_TTL_TARGET_ARN,
    SANDBOX_TTL_THROTTLE_MAX_SESSIONS,
)
from strandly.ops import metrics

logger = logging.getLogger(__name__)

#: Scheduler client timeouts: a Scheduler outage must not stall a wake for botocore's 60 s default.
_CONNECT_TIMEOUT_SECONDS = 5
_READ_TIMEOUT_SECONDS = 10

_clients: dict[str | None, Any] = {}
_clients_lock = threading.Lock()

_Key = tuple[str, str]
#: Per-process throttle state, all guarded by ``_bumped_lock``:
#: ``(capacity provider id, session id)`` -> monotonic time of this process's last successful bump
#: (``inf`` for an ephemeral session, whose fixed schedule is never moved), LRU-bounded;
_bumped: OrderedDict[_Key, float] = OrderedDict()
#: ... -> monotonic time of the last FAILED upsert (retried after the short backoff), LRU-bounded;
_failed: OrderedDict[_Key, float] = OrderedDict()
#: ... sessions with an upsert queued or running in the background (one at a time per session).
_inflight: set[_Key] = set()
_bumped_lock = threading.Lock()

#: The background pool :meth:`SessionTtl.bump_soon` hands upserts to. Built on first use. Its worker
#: threads are joined at interpreter exit, so a short CLI run's only upsert is not lost on the way
#: out (each upsert is bounded by the client timeouts above).
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_pending: set[Future[Any]] = set()
_EXECUTOR_WORKERS = 2


def _remember(table: OrderedDict[_Key, float], key: _Key, value: float) -> None:
    """Set ``table[key]`` as most recently used, evicting the oldest past the cap. Lock held."""
    table[key] = value
    table.move_to_end(key)
    while len(table) > SANDBOX_TTL_THROTTLE_MAX_SESSIONS:
        table.popitem(last=False)


def _submit_default(fn: Callable[[], Any]) -> Any:
    """Run ``fn`` on the shared background pool (built on first use)."""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=_EXECUTOR_WORKERS, thread_name_prefix="strandly-sandbox-ttl"
            )
        future = _executor.submit(fn)
        _pending.add(future)
    future.add_done_callback(_pending.discard)
    return future


def drain(timeout: float | None = None) -> bool:
    """Wait for every queued background upsert (tests, and an orderly shutdown). ``True`` if idle."""
    with _executor_lock:
        futures = list(_pending)
    if not futures:
        return True
    _, not_done = _wait_futures(futures, timeout=timeout)
    return not not_done


@dataclass(frozen=True)
class SessionTtlConfig:
    """Deploy wiring for the TTL (the Sandbox stack's outputs, via the agent runtime's env)."""

    capacity_provider_id: str
    schedule_group: str
    role_arn: str

    @classmethod
    def from_values(
        cls, capacity_provider_id: str | None, schedule_group: str | None, role_arn: str | None
    ) -> SessionTtlConfig | None:
        """All three set → a config; any missing → ``None`` (TTL off: nothing is scheduled)."""
        if capacity_provider_id and schedule_group and role_arn:
            return cls(capacity_provider_id, schedule_group, role_arn)
        return None


def schedule_name(capacity_provider_id: str, session_id: str) -> str:
    """The session's schedule name: deterministic, <= 64 chars of ``[0-9A-Za-z-_.]``."""
    # NUL-separated: ("ab", "c") and ("a", "bc") must not share a schedule.
    digest = hashlib.sha256(f"{capacity_provider_id}\0{session_id}".encode()).hexdigest()
    return SANDBOX_TTL_SCHEDULE_PREFIX + digest[:SANDBOX_TTL_SCHEDULE_HASH_CHARS]


def at_expression(when: datetime) -> str:
    """``at(yyyy-mm-ddThh:mm:ss)`` in UTC (Scheduler's one-time schedule syntax)."""
    utc = when.astimezone(timezone.utc) if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return f"at({utc.strftime('%Y-%m-%dT%H:%M:%S')})"


def target_input(capacity_provider_id: str, session_id: str) -> str:
    """The ``DeleteCapacityProviderSession`` request as universal-target JSON input.

    Universal targets take the request members in PascalCase (the AWS SDK model member names, e.g.
    Step Functions' ``stateMachineArn`` is passed as ``StateMachineArn``), so the API's
    ``capacityProviderId`` / ``sessionId`` URI parameters become ``CapacityProviderId`` /
    ``SessionId``.
    """
    return json.dumps(
        {"CapacityProviderId": capacity_provider_id, "SessionId": session_id},
        separators=(",", ":"),
    )


def schedule_request(
    config: SessionTtlConfig,
    session_id: str,
    now: datetime,
    ttl_seconds: int = SANDBOX_SESSION_TTL_SECONDS,
) -> dict[str, Any]:
    """Full ``CreateSchedule`` / ``UpdateSchedule`` kwargs for ``session_id`` (pure — unit-tested)."""
    expires = now + timedelta(seconds=ttl_seconds)
    return {
        "Name": schedule_name(config.capacity_provider_id, session_id),
        "GroupName": config.schedule_group,
        "ScheduleExpression": at_expression(expires),
        "ScheduleExpressionTimezone": "UTC",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "ActionAfterCompletion": "DELETE",
        "State": "ENABLED",
        "Description": f"Delete idle sandbox session {session_id}"[:512],
        "Target": {
            "Arn": SANDBOX_TTL_TARGET_ARN,
            "RoleArn": config.role_arn,
            "Input": target_input(config.capacity_provider_id, session_id),
            "RetryPolicy": {
                "MaximumRetryAttempts": SANDBOX_TTL_RETRY_ATTEMPTS,
                "MaximumEventAgeInSeconds": SANDBOX_TTL_RETRY_MAX_AGE_SECONDS,
            },
        },
    }


def _scheduler_client(region: str | None) -> Any:
    """The process's Scheduler client for ``region``, built on first use (never at import)."""
    with _clients_lock:
        client = _clients.get(region)
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.Session(region_name=region).client(
                "scheduler",
                config=Config(
                    connect_timeout=_CONNECT_TIMEOUT_SECONDS,
                    read_timeout=_READ_TIMEOUT_SECONDS,
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            )
            _clients[region] = client
        return client


def _error_code(error: BaseException) -> str:
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code") or "")
    return ""


def reset_throttle() -> None:
    """Forget every bump (and failure) this process made (tests)."""
    with _bumped_lock:
        _bumped.clear()
        _failed.clear()
        _inflight.clear()


class SessionTtl:
    """Upserts the sliding TTL schedule for sandbox sessions on one capacity provider."""

    def __init__(
        self,
        config: SessionTtlConfig,
        *,
        region: str | None = None,
        client: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        submit: Callable[[Callable[[], Any]], Any] | None = None,
    ) -> None:
        """``submit`` runs a background upsert (default: the shared thread pool; tests pass an
        inline runner to make :meth:`bump_soon` synchronous)."""
        self.config = config
        self._region = region
        self._client = client
        self._clock = clock
        self._now = now
        self._submit = submit or _submit_default

    def _due(self, key: _Key) -> bool:
        """Whether ``key`` needs an upsert now (throttle + failure backoff). Lock held."""
        now = self._clock()
        last = _bumped.get(key)
        if last is not None and now - last < SANDBOX_TTL_BUMP_INTERVAL_SECONDS:
            return False
        failed = _failed.get(key)
        return failed is None or now - failed >= SANDBOX_TTL_FAILURE_RETRY_SECONDS

    def bump_soon(self, session_id: str, *, ephemeral: bool = False) -> bool:
        """Queue :meth:`bump` in the background if it is due. Never raises, never blocks on AWS.

        This is what the sandbox calls on every wake and command: the check is a dict lookup under
        a lock, and the upsert itself (a synchronous boto call that can take seconds on a degraded
        Scheduler) runs on a worker thread. At most one upsert per session is queued at a time.
        Returns ``True`` when an upsert was queued.
        """
        key = (self.config.capacity_provider_id, session_id)
        with _bumped_lock:
            if key in _inflight or not self._due(key):
                return False
            _inflight.add(key)

        def run() -> None:
            try:
                self.bump(session_id, ephemeral=ephemeral)
            finally:
                with _bumped_lock:
                    _inflight.discard(key)

        try:
            self._submit(run)
        except Exception as e:  # noqa: BLE001 — e.g. the pool is shut down at interpreter exit
            with _bumped_lock:
                _inflight.discard(key)
            logger.warning("sandbox session TTL upsert not queued (%s) [session=%s]", e, session_id)
            return False
        return True

    def bump(self, session_id: str, *, ephemeral: bool = False) -> bool:
        """(Re)schedule ``session_id``'s deletion TTL from now, synchronously. Never raises.

        ``ephemeral`` (a sessionless invoke's throwaway session, which no later run reuses) uses
        :data:`SANDBOX_EPHEMERAL_SESSION_TTL_SECONDS` and is scheduled once: after a success this
        process never moves it again.

        Returns ``True`` when a schedule was created or updated, ``False`` when throttled (this
        process bumped the session recently, or failed to moments ago) or when the upsert failed
        (logged + counted; retried by the next use after :data:`SANDBOX_TTL_FAILURE_RETRY_SECONDS`).
        """
        key = (self.config.capacity_provider_id, session_id)
        with _bumped_lock:
            if not self._due(key):
                return False
        try:
            ttl = (
                SANDBOX_EPHEMERAL_SESSION_TTL_SECONDS if ephemeral else SANDBOX_SESSION_TTL_SECONDS
            )
            request = schedule_request(self.config, session_id, self._now(), ttl)
            client = self._client or _scheduler_client(self._region)
            try:
                client.create_schedule(**request)
            except Exception as e:  # noqa: BLE001 — only a conflict (exists) falls through
                if _error_code(e) != "ConflictException":
                    raise
                client.update_schedule(**request)
        except Exception as e:  # noqa: BLE001 — fail-soft: a TTL problem must never fail a wake
            with _bumped_lock:
                _remember(_failed, key, self._clock())
            logger.warning(
                "sandbox session TTL upsert failed (%s: %s) [session=%s, schedule=%s/%s]; "
                "retrying on the next use after %ss",
                _error_code(e) or type(e).__name__,
                str(e)[:200],
                session_id,
                self.config.schedule_group,
                schedule_name(self.config.capacity_provider_id, session_id),
                SANDBOX_TTL_FAILURE_RETRY_SECONDS,
            )
            metrics.emit(
                {metrics.SANDBOX_TTL_UPSERTS: 1, metrics.SANDBOX_TTL_UPSERT_FAILED: 1},
                surface=metrics.SURFACE_AGENTCORE,
            )
            return False
        with _bumped_lock:
            _failed.pop(key, None)
            _remember(_bumped, key, math.inf if ephemeral else self._clock())
        metrics.emit(
            {metrics.SANDBOX_TTL_UPSERTS: 1, metrics.SANDBOX_TTL_UPSERT_FAILED: 0},
            surface=metrics.SURFACE_AGENTCORE,
        )
        logger.info(
            "sandbox session TTL set: %s deleted after %s unless used again [session=%s]",
            request["Name"],
            request["ScheduleExpression"],
            session_id,
        )
        return True
