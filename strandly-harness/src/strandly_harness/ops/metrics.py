"""CloudWatch EMF metrics — operational telemetry from the harness's choke points.

This is the **operational** half of strandly's monitoring (the security half is the out-of-band
``ingress/audit.py``; the cost half is AWS-native Cost Anomaly Detection — neither is a self-emitted
metric). It emits a handful of operational metrics — invocations, failures, duration, token
throughput, poller health, ledger-write failures, stuck runs — from the points every deployed run
and every poll already pass through, so an alarm can finally *watch* the success rate / poller /
ledger we already compute but nobody was watching.

**How (no new dependency, no extra IAM).** It writes the CloudWatch *Embedded Metric Format* (EMF):
a single JSON line to stdout with an ``_aws`` header. Any CloudWatch Logs group (the AgentCore
runtime's, a Lambda's) auto-extracts the embedded metrics — so emitting is just ``print`` and needs
no ``cloudwatch:PutMetricData`` grant and no boto call on the hot path.

**Gated + fail-open.** Off unless ``STRANDLY_METRICS_NAMESPACE`` is set (so tests and local runs are
a pure no-op), and every emit is wrapped so a metrics bug can never disrupt or slow a run — exactly
like the run-ledger's fail-open writes.

**Dimensions.** Each metric is emitted under **two** dimension sets: the empty set ``[]`` (so the
metric rolls up to the namespace level, which is what alarms reference — and since the namespace is
already per-env, e.g. ``Strandly-dev``, that rollup is per-env) **and** ``[surface]`` (``agentcore``
/ ``poller`` / ``monitoring``) for drill-down. Alarms therefore reference only namespace + metric
name and don't depend on a dimension-value contract across the infra/runtime boundary.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
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
# Long-term knowledge-base writes (the ``add_memory`` tool). Its own surface so a write spike (the
# memory-poisoning signal) can be drilled into independently of the run/poller surfaces.
SURFACE_MEMORY = "memory"

# Metric names (one place, referenced by the infra alarms by string).
INVOCATIONS = "Invocations"
COMPLETED = "Completed"
FAILURES = "Failures"
DURATION_MS = "DurationMs"
TOKENS_TOTAL = "TokensTotal"
LEDGER_WRITE_FAILED = "LedgerWriteFailed"
POLL_SUCCESS = "PollSuccess"
POLL_ERROR = "PollError"
NOTIFICATIONS_FETCHED = "NotificationsFetched"
DISPATCHED = "Dispatched"
DISPATCH_FAILED = "DispatchFailed"
UNAUTHORIZED = "Unauthorized"
STUCK_RUNS = "StuckRuns"
# Long-term memory (``add_memory``) write outcomes — emitted once per add_memory call so a KB
# ingestion write (the memory-poisoning vector) is finally observable. ``MemoryWrite`` counts every
# call; ``MemoryWriteFailed`` counts the ones whose underlying write raised.
MEMORY_WRITE = "MemoryWrite"
MEMORY_WRITE_FAILED = "MemoryWriteFailed"

# Units (a subset of the CloudWatch unit enum we use).
COUNT = "Count"
MILLISECONDS = "Milliseconds"


def namespace() -> str | None:
    """The configured metric namespace, or ``None`` when metrics are disabled."""
    return os.environ.get(NAMESPACE_ENV) or None


def enabled() -> bool:
    """True iff a namespace is configured (metrics are emitted)."""
    return bool(namespace())


def build_emf(
    metrics: dict[str, Any],
    *,
    namespace: str,
    surface: str | None = None,
    env: str | None = None,
    timestamp_ms: int | None = None,
) -> dict[str, Any]:
    """Build one EMF document for ``metrics`` (pure — no I/O, fully unit-testable).

    ``metrics`` maps a metric name to either a numeric value (unit defaults to ``Count``) or a
    ``(value, unit)`` tuple. The document declares two dimension sets — ``[]`` (namespace-level
    rollup, what alarms read) and ``[surface]`` when a surface is given — and carries the dimension
    values + each metric value as top-level fields, per the EMF spec.
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

    # Dimension sets: always the empty rollup; add [surface] for drill-down when present.
    dimension_sets: list[list[str]] = [[]]
    dimension_fields: dict[str, str] = {}
    if surface:
        dimension_sets.append(["surface"])
        dimension_fields["surface"] = surface
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


def emit(metrics: dict[str, Any], *, surface: str | None = None) -> bool:
    """Emit ``metrics`` as one EMF line to stdout. No-op when disabled; fail-open always.

    Returns ``True`` if a line was written, ``False`` if metrics are disabled or the emit was
    swallowed by the fail-open guard. Never raises — a metrics error must not disrupt a run.
    """
    ns = namespace()
    if not ns or not metrics:
        return False
    try:
        doc = build_emf(metrics, namespace=ns, surface=surface, env=os.environ.get(ENV_ENV))
        sys.stdout.write(json.dumps(doc) + "\n")
        return True
    except Exception:  # noqa: BLE001 — fail-open: telemetry must never break a run
        logger.debug("metrics emit failed; continuing", exc_info=True)
        return False
