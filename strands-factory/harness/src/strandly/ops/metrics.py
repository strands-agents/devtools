"""CloudWatch metrics from the harness's choke points.

Invocations, failures, duration, tokens, and poller and ledger health. Lambdas print Embedded Metric
Format lines, which their log groups extract. The agent runtime's stdout is not an extraction
contract, so there (``BEDROCK_AGENTCORE_RUNTIME=true`` or ``STRANDLY_METRICS_SINK=api``) the same
documents become ``PutMetricData`` calls through a buffered background emitter; a process uses one
path, never both. Off unless ``STRANDLY_METRICS_NAMESPACE`` is set, and every emit is fail-open.
"""

from __future__ import annotations

import atexit
import json
import logging
import math
import os
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# The env var that gates + names the metric namespace. Unset → metrics are a no-op. Set by the CDK
# on each Lambda (and folded into the config secret for the deployed runtime), e.g. "Strandly-dev".
NAMESPACE_ENV = "STRANDLY_METRICS_NAMESPACE"
# Optional dimension VALUE for the "env" we tag drill-down series with. Purely informational (the
# namespace already encodes env); kept low-cardinality.
ENV_ENV = "STRANDLY_ENV"

# Surfaces (the ``surface`` drill-down dimension value). Plain documented strings — alarms don't
# depend on them (they reference the namespace-level rollup), so there is no cross-venv sync needed.
SURFACE_AGENTCORE = "agentcore"
SURFACE_POLLER = "poller"
SURFACE_MONITORING = "monitoring"
# The Slack DM ingress Lambda (ops/lambdas/slack_ingress) — its dispatched/unauthorized/failed
# counts drill down separately from the GitHub poller.
SURFACE_SLACK = "slack"
# The short-term session store (memory/session_store.py — snapshot/stash I/O) and the transcript
# events (memory/transcript.py — TranscriptWriteFailed).
SURFACE_SESSION = "session"
# The scheduled-jobs invoker Lambda (ops/lambdas/scheduled).
SURFACE_SCHEDULED = "scheduled"

# Metric names (one place, referenced by the infra alarms by string).
INVOCATIONS = "Invocations"
COMPLETED = "Completed"
FAILURES = "Failures"
DURATION_MS = "DurationMs"
TOKENS_TOTAL = "TokensTotal"
# Tool calls inside a finished turn that returned an error result. Emitted (zero included) with
# every run's terminal metrics: a spike with Completed still green is the "turn finished but the
# task was starved" signature (e.g. an expired GitHub token failing every use_github call).
TOOL_ERRORS = "ToolErrors"
# Per-tool call/error rollup, dimensioned by ``[tool_name]`` (see ``emit_tool_call``). Distinct from
# the run-level ``ToolErrors`` above (which is a whole-turn aggregate with []/[surface] dims): these
# are emitted once per finished tool call so the dashboard can show a per-tool error rate the SDK's
# ``strands.tool.*`` metrics can't (those key on the per-call tool_use id → unsummable per tool).
TOOL_CALLS = "ToolCalls"
TOOL_CALL_ERRORS = "ToolCallErrors"
# The drill-down dimension name for the two metrics above. Kept in sync with the dashboard reader
# (dashboard/api/handler.py) and asserted by tests/test_infra_constants_sync.py.
TOOL_NAME_DIM = "tool_name"
# A run that ended because a newer message on its session superseded it, or a Stop. Not a failure.
CANCELLED = "Cancelled"
LEDGER_WRITE_FAILED = "LedgerWriteFailed"
# A run's final message carried ```work-product``` block(s) but no artifact was persisted: none
# parsed, none was about the run's own GitHub item, or the parsed block was unusable or failed to
# build/store. Emitted at most once per run (serve/agentcore_app.py).
WORK_PRODUCT_PARSE_FAILED = "WorkProductParseFailed"
POLL_SUCCESS = "PollSuccess"
POLL_ERROR = "PollError"
NOTIFICATIONS_FETCHED = "NotificationsFetched"
DISPATCHED = "Dispatched"
DISPATCH_FAILED = "DispatchFailed"
# A launch raised AFTER its request was sent (read timeout, connection dropped mid-response): the
# runtime may already have accepted the run, so the trigger keeps its dispatch intent (no rollback,
# no re-dispatch) rather than risk a duplicate run. ``runtime_client.launch_maybe_accepted``.
DISPATCH_MAYBE = "DispatchMaybe"
UNAUTHORIZED = "Unauthorized"
STUCK_RUNS = "StuckRuns"
REAPED_RUNS = "ReapedRuns"  # orphaned running rows the stuck-run reaper closed (per scan)
# The sandbox sessions' sliding TTL (sandbox/ttl.py): one EventBridge Scheduler upsert per used
# session per hour (throttled per process). A failed upsert never fails the wake, so the failure counter is
# the only signal that a session (= a retained 40 GiB EBS volume) may outlive its TTL.
SANDBOX_TTL_UPSERTS = "SandboxTtlUpserts"
SANDBOX_TTL_UPSERT_FAILED = "SandboxTtlUpsertFailed"
# The TTL is off on a runtime sandbox: 1-2 of its 3 deploy values set (or none). Once per process
# (sandbox/select.py); nothing then deletes sandbox sessions or their volumes.
SANDBOX_TTL_MISCONFIGURED = "SandboxTtlMisconfigured"
# Session store (memory/session_store.py): per-save snapshot size + latency (the per-message save
# cadence is gated on these) and I/O failures. A failure also fails the build/turn and logs the
# SESSION_STORAGE_FAILURE token; these counts are for dashboards and alarms.
SESSION_SNAPSHOT_BYTES = "SessionSnapshotBytes"
SESSION_WRITE_MS = "SessionWriteMs"
SESSION_WRITE_FAILED = "SessionWriteFailed"
SESSION_READ_FAILED = "SessionReadFailed"
# Transcript (memory/transcript.py): a message the recorder could not write to AgentCore Memory
# after its bounded retries. The turn goes on (the snapshot, not the transcript, is what the agent
# reloads); the same failure logs the SESSION_STORAGE_FAILURE token with op=transcript.
TRANSCRIPT_WRITE_FAILED = "TranscriptWriteFailed"

# Units (a subset of the CloudWatch unit enum we use).
COUNT = "Count"
MILLISECONDS = "Milliseconds"
BYTES = "Bytes"


def namespace() -> str | None:
    """The configured metric namespace, or ``None`` when metrics are disabled."""
    return os.environ.get(NAMESPACE_ENV) or None


def enabled() -> bool:
    """True iff a namespace is configured (metrics are emitted)."""
    return bool(namespace())


def configure(ns: str | None) -> bool:
    """Publish ``ns`` as this process's metric namespace, unless the env var is already set.

    The gate above reads **only the process env** — deliberately: ``ops/`` is a strands-free zone
    that knows nothing about ``core.config``, and every *Lambda* surface has the variable wired
    straight onto its function env by the CDK (ingress / slack / monitoring stacks). The **deployed
    AgentCore runtime is the one surface that doesn't**: its environment is a fixed set of deploy
    wiring (AgentRuntimeStack), so almost everything it knows arrives through the config secret — which
    ``Config.load`` merges into ``Config.values`` and **never** into ``os.environ``. That gap is why
    a namespace in the config secret was read by ``Config`` and then dropped on the floor, leaving
    every runtime-side emit (run metrics, ledger-write failures, KB writes) a silent no-op.

    This is the bridge, called once at runtime startup (``serve.agentcore_app.build_app``). An
    already-set env var always wins, mirroring ``Config.load``'s "process env beats the secret"
    precedence, so an explicit ``--env STRANDLY_METRICS_NAMESPACE=…`` on the runtime (or a local
    shell export) is never overridden. Returns True iff this call set the variable.
    """
    if not ns or os.environ.get(NAMESPACE_ENV):
        return False
    os.environ[NAMESPACE_ENV] = ns
    return True


def build_emf(
    metrics: dict[str, Any],
    *,
    namespace: str,
    surface: str | None = None,
    dimensions: dict[str, str] | None = None,
    rollup: bool = True,
    env: str | None = None,
    timestamp_ms: int | None = None,
) -> dict[str, Any]:
    """Build one EMF document for ``metrics`` (pure — no I/O, fully unit-testable).

    ``metrics`` maps a metric name to either a numeric value (unit defaults to ``Count``) or a
    ``(value, unit)`` tuple. The document declares the ``[]`` dimension set (namespace-level rollup,
    what alarms read) when ``rollup`` is true, plus ``[surface]`` when a surface is given, and — for
    each entry in ``dimensions`` — a single-key set ``[key]`` so the metric can be read grouped by
    that key (e.g. ``tool_name``). Every dimension value is also carried as a top-level field, per
    the EMF spec. Keep ``dimensions`` LOW-CARDINALITY (a handful of distinct values): a
    per-call-unique value (a tool_use id, a session id) would fan the namespace out into an
    unqueryable metric — the exact trap that makes the raw ``strands.tool.*`` metrics unreadable.

    Set ``rollup=False`` for a metric that is ONLY meaningful per-dimension: a ``GROUP BY <key>``
    read treats the empty-set datapoints (which carry the same value) as an unlabelled group equal
    to the grand total, so leaving the rollup on would double-count. Requires ``surface`` or
    ``dimensions`` so at least one set remains.
    """
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)

    definitions: list[dict[str, str]] = []
    values: dict[str, Any] = {}
    for name, raw in metrics.items():
        if isinstance(raw, tuple):
            value, unit = raw
        else:
            value, unit = raw, COUNT
        definitions.append({"Name": name, "Unit": unit})
        values[name] = value

    # Dimension sets: the empty rollup (alarms read it) unless suppressed; [surface] for drill-down.
    dimension_sets: list[list[str]] = [[]] if rollup else []
    dimension_fields: dict[str, str] = {}
    if surface:
        dimension_sets.append(["surface"])
        dimension_fields["surface"] = surface
    # Each extra dimension gets its OWN single-key set (so a reader can GROUP BY it) and a field.
    for key, val in (dimensions or {}).items():
        dimension_sets.append([key])
        dimension_fields[key] = val
    # Defensive: EMF needs at least one dimension set. rollup=False with no surface/dimensions would
    # leave none — fall back to the empty rollup rather than emit an invalid document.
    if not dimension_sets:
        dimension_sets = [[]]
    # ``env`` rides along as a plain field (not a dimension — the namespace already encodes env).
    if env:
        dimension_fields["env"] = env

    return {
        "_aws": {
            "Timestamp": ts,
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": dimension_sets,
                    "Metrics": definitions,
                }
            ],
        },
        **dimension_fields,
        **values,
    }


def emit(
    metrics: dict[str, Any],
    *,
    surface: str | None = None,
    dimensions: dict[str, str] | None = None,
    rollup: bool = True,
) -> bool:
    """Emit ``metrics`` as one EMF line to stdout. No-op when disabled; fail-open always.

    ``dimensions`` adds a low-cardinality drill-down dimension per key and ``rollup`` controls the
    namespace-level ``[]`` set (see :func:`build_emf`). Returns ``True`` if a line was written,
    ``False`` if metrics are disabled or the emit was swallowed by the fail-open guard. Never raises
    — a metrics error must not disrupt a run.
    """
    ns = namespace()
    if not ns or not metrics:
        return False
    try:
        doc = build_emf(
            metrics,
            namespace=ns,
            surface=surface,
            dimensions=dimensions,
            rollup=rollup,
            env=os.environ.get(ENV_ENV),
        )
        if direct_sink():
            return _emitter().put(emf_to_datums(doc))
        sys.stdout.write(json.dumps(doc) + "\n")
        return True
    except Exception:  # noqa: BLE001 — fail-open: telemetry must never break a run
        logger.debug("metrics emit failed; continuing", exc_info=True)
        return False


def emit_tool_call(tool_name: str, *, error: bool) -> bool:
    """Emit a per-``tool_name`` call/error rollup (one EMF line per finished tool call).

    :data:`TOOL_CALLS` counts every call and :data:`TOOL_CALL_ERRORS` counts the errored ones, both
    under a ``[tool_name]`` dimension ONLY (``rollup=False``) — ~one series per distinct tool, low
    cardinality, unlike the SDK's ``strands.tool.*`` metrics which key on the per-call ``tool_use``
    id and so can't be summed per tool over a window. The empty rollup is suppressed so a
    ``GROUP BY tool_name`` read doesn't double-count it as an unlabelled grand-total group. The
    dashboard reads these to show an accurate per-tool error rate.
    """
    return emit(
        {TOOL_CALLS: 1, TOOL_CALL_ERRORS: 1 if error else 0},
        dimensions={TOOL_NAME_DIM: tool_name},
        rollup=False,
    )


# --- Direct delivery (the deployed agent runtime) ---------------------------------------------------
#
# EMF extraction from an AgentCore runtime's stdout depends on the compute type: Runtime instances
# re-wrap every stdout line as ``{"log": "<the line>"}``, so the ``_aws`` header is not
# top-level, nothing is extracted and every alarm on the agent's metrics sees no data. The agent
# runtime therefore delivers its metrics itself, on any compute type, with
# ``cloudwatch:PutMetricData`` (IAM: ``StrandlyMetricsPut`` in infra/stacks/runtime_iam_stack.py,
# conditioned on this namespace). Why PutMetricData and not EMF via ``PutLogEvents``: no extra log
# group/stream to own, create and race on (sequence tokens, stream creation on cold start), the
# metric lands without depending on log extraction, and the grant
# is a single action pinned to one namespace. Lambdas keep stdout EMF (Lambda extracts it fine) —
# exactly one path per process, so nothing is double-counted.

#: Explicit override of the sink: ``api`` (PutMetricData) or ``emf`` (stdout). Unset → ``api`` iff
#: this process is the deployed AgentCore runtime (:data:`RUNTIME_MARKER_ENV`), else ``emf``.
SINK_ENV = "STRANDLY_METRICS_SINK"
SINK_API = "api"
SINK_EMF = "emf"
#: Set (to ``true``) on the deployed agent runtime by the CDK — mirrors
#: ``strandly.core.constants.AGENTCORE_RUNTIME_MARKER_ENV`` (``ops/`` stays import-light).
RUNTIME_MARKER_ENV = "BEDROCK_AGENTCORE_RUNTIME"

#: PutMetricData limits: ≤1000 datums and ≤1 MB per request. We stop a batch well under the byte cap.
PUT_MAX_DATUMS = 1000
PUT_MAX_BYTES = 900_000
#: Background flush cadence (seconds). Short enough that a turn's metrics land within the alarm
#: period, long enough to batch a busy turn's per-tool datums into a handful of calls.
FLUSH_INTERVAL_S = 15.0
#: Bounded queue: past this the OLDEST datums are dropped (counted) rather than growing unbounded
#: when CloudWatch is unreachable.
QUEUE_MAX_DATUMS = 20_000


def direct_sink() -> bool:
    """True when this process must deliver metrics with PutMetricData instead of stdout EMF."""
    sink = (os.environ.get(SINK_ENV) or "").strip().lower()
    if sink in (SINK_API, SINK_EMF):
        return sink == SINK_API
    return (os.environ.get(RUNTIME_MARKER_ENV) or "").strip().lower() in ("1", "true", "yes")


def emf_to_datums(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand one EMF document (:func:`build_emf`) into PutMetricData ``MetricData`` entries.

    One datum per (metric, dimension set) — exactly the series CloudWatch would have extracted
    from the EMF line, so the empty-set rollup the alarms read and the ``[surface]`` /
    ``[tool_name]`` drill-downs the dashboard reads keep their names, dimensions and units. The
    datums carry their namespace under ``_ns`` (stripped before the call). A NaN or ±Inf value is
    skipped: PutMetricData rejects the WHOLE request (every other datum in the batch with it) on a
    non-finite value. Pure; no I/O.
    """
    aws = doc["_aws"]
    ts = datetime.fromtimestamp(aws["Timestamp"] / 1000, tz=timezone.utc)
    out: list[dict[str, Any]] = []
    for directive in aws["CloudWatchMetrics"]:
        ns = directive["Namespace"]
        for definition in directive["Metrics"]:
            name = definition["Name"]
            value = doc.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if not math.isfinite(value):
                continue
            for dim_set in directive["Dimensions"]:
                out.append(
                    {
                        "_ns": ns,
                        "MetricName": name,
                        "Dimensions": [{"Name": k, "Value": str(doc[k])} for k in dim_set],
                        "Value": float(value),
                        "Unit": definition.get("Unit", COUNT),
                        "Timestamp": ts,
                    }
                )
    return out


def _datum_size(datum: dict[str, Any]) -> int:
    # Query-protocol encoding is ~ key + value per field; a generous JSON estimate keeps us safe.
    return len(json.dumps(datum, default=str)) + 64


class PutMetricDataEmitter:
    """Buffered, background-flushing ``cloudwatch:PutMetricData`` sender. Never blocks, never raises.

    ``put`` only appends to a bounded in-memory queue (oldest dropped + counted past
    :data:`QUEUE_MAX_DATUMS`). A daemon thread flushes every :data:`FLUSH_INTERVAL_S` (and as soon as
    a full batch is waiting); :meth:`flush` drains synchronously (used at process exit via
    ``atexit``). Each call carries ≤ :data:`PUT_MAX_DATUMS` datums / ≤ :data:`PUT_MAX_BYTES`, grouped
    by namespace. A failed call is logged (rate-limited) and counted in ``failed_calls`` /
    ``dropped_datums`` — the datums are dropped, not retried forever (the client already retries
    transient errors), so a CloudWatch outage can never back up into a run.
    """

    def __init__(
        self,
        client_factory: Callable[[], Any] | None = None,
        *,
        flush_interval_s: float = FLUSH_INTERVAL_S,
        max_queue: int = QUEUE_MAX_DATUMS,
        start_thread: bool = True,
    ) -> None:
        self._client_factory = client_factory or _default_cloudwatch_client
        self._client: Any = None
        self._queue: deque[dict[str, Any]] = deque()
        self._max_queue = max_queue
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._wake = threading.Event()
        self._interval = flush_interval_s
        self._last_warn = 0.0
        self.sent_datums = 0
        self.sent_calls = 0
        self.failed_calls = 0
        self.dropped_datums = 0
        self._thread: threading.Thread | None = None
        if start_thread:
            self._thread = threading.Thread(
                target=self._run, name="strandly-metrics-flush", daemon=True
            )
            self._thread.start()

    def put(self, datums: list[dict[str, Any]]) -> bool:
        """Enqueue ``datums``; True if anything was queued. O(len(datums)), no I/O."""
        if not datums:
            return False
        with self._lock:
            for d in datums:
                if len(self._queue) >= self._max_queue:
                    self._queue.popleft()
                    self.dropped_datums += 1
                self._queue.append(d)
            full = len(self._queue) >= PUT_MAX_DATUMS
        if full:
            self._wake.set()
        return True

    def request_flush(self) -> None:
        """Wake the background flusher now (instead of at its next tick). Never blocks, never raises.

        Without a flusher thread (``start_thread=False``) this is a no-op: the queue is still sent
        by the next :meth:`flush` (``atexit``)."""
        self._wake.set()

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def _take_batch(self) -> tuple[str, list[dict[str, Any]]] | None:
        """Pop the next batch: one namespace, ≤ PUT_MAX_DATUMS datums, ≤ PUT_MAX_BYTES."""
        with self._lock:
            if not self._queue:
                return None
            ns = self._queue[0]["_ns"]
            batch: list[dict[str, Any]] = []
            size = 0
            keep: deque[dict[str, Any]] = deque()
            while self._queue and len(batch) < PUT_MAX_DATUMS:
                d = self._queue.popleft()
                if d["_ns"] != ns:
                    keep.append(d)
                    continue
                datum = {k: v for k, v in d.items() if k != "_ns"}
                n = _datum_size(datum)
                if batch and size + n > PUT_MAX_BYTES:
                    keep.append(d)
                    break
                batch.append(datum)
                size += n
            # Put back what we skipped, ahead of the rest, preserving order.
            self._queue.extendleft(reversed(keep))
            return ns, batch

    def flush(self) -> int:
        """Send everything queued now (synchronously); returns datums sent. Never raises."""
        sent = 0
        with self._flush_lock:
            while True:
                nxt = self._take_batch()
                if nxt is None:
                    return sent
                ns, batch = nxt
                try:
                    if self._client is None:
                        self._client = self._client_factory()
                    self._client.put_metric_data(Namespace=ns, MetricData=batch)
                    self.sent_calls += 1
                    self.sent_datums += len(batch)
                    sent += len(batch)
                except Exception as e:  # noqa: BLE001 — fail-open: never break a run
                    self.failed_calls += 1
                    self.dropped_datums += len(batch)
                    now = time.monotonic()
                    if now - self._last_warn > 60:
                        self._last_warn = now
                        logger.warning(
                            "metrics: PutMetricData failed (%s datums dropped; %s failed calls so "
                            "far): %s",
                            len(batch),
                            self.failed_calls,
                            e,
                        )

    def _run(self) -> None:
        while True:
            self._wake.wait(self._interval)
            self._wake.clear()
            try:
                self.flush()
            except Exception:  # noqa: BLE001 — the flusher thread must never die
                logger.debug("metrics flush loop error", exc_info=True)


def _default_cloudwatch_client() -> Any:
    import boto3
    from botocore.config import Config

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    session = boto3.Session(region_name=region) if region else boto3.Session()
    return session.client(
        "cloudwatch",
        config=Config(
            connect_timeout=5,
            read_timeout=10,
            retries={"total_max_attempts": 3, "mode": "standard"},
        ),
    )


_EMITTER: PutMetricDataEmitter | None = None
_EMITTER_LOCK = threading.Lock()


def _emitter() -> PutMetricDataEmitter:
    """The process-wide emitter (lazily started on the first direct emit; flushed at exit)."""
    global _EMITTER
    if _EMITTER is None:
        with _EMITTER_LOCK:
            if _EMITTER is None:
                _EMITTER = PutMetricDataEmitter()
                atexit.register(_EMITTER.flush)
    return _EMITTER


def flush() -> int:
    """Flush the direct-delivery queue now (e.g. at the end of a turn). No-op off the runtime."""
    em = _EMITTER
    return em.flush() if em is not None else 0


def request_flush() -> None:
    """Ask the background flusher to send the queue NOW, without waiting for it (e.g. at the end
    of a run). Non-blocking and non-raising — safe anywhere, including a ``finally`` that must not
    be delayed or interrupted. No-op off the runtime (no emitter)."""
    em = _EMITTER
    if em is not None:
        em.request_flush()
