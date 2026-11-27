"""OpenTelemetry environment sanitation.

AWS runtimes often set ``OTEL_PROPAGATORS=xray``, which OpenTelemetry resolves to an entry point at
import time; without the X-Ray propagator installed that import crashes the process. Strands sets the
W3C propagators itself, so :func:`sanitize_otel_env` removes ``xray`` (keeping other propagators,
``tracecontext,baggage`` if none remain). It must run before the first ``strands`` or
``opentelemetry`` import, so the package ``__init__`` calls it.
"""

from __future__ import annotations

import os

# Propagator names that need the optional AWS X-Ray package; we drop them to avoid the import crash.
_XRAY_NAMES = {"xray", "aws_xray", "awsxray"}
_DEFAULT = "tracecontext,baggage"


def sanitize_otel_env(env: dict[str, str] | None = None) -> None:
    """Strip an X-Ray propagator from ``OTEL_PROPAGATORS`` in place (defaults to ``os.environ``).

    No-op when the variable is unset or contains no X-Ray entry. When removing X-Ray empties the
    list, falls back to ``tracecontext,baggage`` so propagation still works.
    """
    target = os.environ if env is None else env
    raw = target.get("OTEL_PROPAGATORS")
    if not raw:
        return
    kept = [p.strip() for p in raw.split(",") if p.strip() and p.strip().lower() not in _XRAY_NAMES]
    new_value = ",".join(kept) if kept else _DEFAULT
    if new_value != raw:
        target["OTEL_PROPAGATORS"] = new_value


# ---------------------------------------------------------------------------------------------------
# CloudWatch OTLP log batch sizing
# ---------------------------------------------------------------------------------------------------

#: Byte budget per OTLP logs request. CloudWatch rejects anything over 1 MiB (``400 Upload too
#: large``). ADOT packs batches up to its *estimate* of 1 MiB, but the estimate omits protobuf
#: framing, so real requests land just over: of 17,911 rejected uploads in prod (Aug–Oct 2026) the
#: median was ~1.05 MB and 99.4% were within 10% of the limit. A 25% margin covers that overshoot.
CW_LOG_REQUEST_BUDGET_BYTES = 768 * 1024

#: How deep ADOT's size estimator walks a log body. Its default (3) stops at ``input.messages[i]``,
#: so the actual text inside ``content``/``parts`` lists counts as zero — the heavy part of a GenAI
#: log record. Walking further makes the estimate track the real payload.
CW_LOG_ESTIMATE_DEPTH = 12

_CW_PROCESSOR_MODULE = (
    "amazon.opentelemetry.distro.exporter.otlp.aws.logs._aws_cw_otlp_batch_log_record_processor"
)
_PATCHED_FLAG = "_strandly_budget_patched"
_ORIGINAL_ESTIMATE = "_strandly_original_estimate_log_size"


def cap_cloudwatch_log_batches(
    budget_bytes: int = CW_LOG_REQUEST_BUDGET_BYTES, depth: int = CW_LOG_ESTIMATE_DEPTH
) -> bool:
    """Keep ADOT's CloudWatch OTLP log batches under the 1 MiB request limit. Returns whether it applied.

    Patches ``AwsCloudWatchOtlpBatchLogRecordProcessor`` (the processor ADOT installs when
    ``AGENT_OBSERVABILITY_ENABLED=true``) at the **class** level: it lowers the per-request byte
    budget and deepens the size estimate. Both are read on every export, so this works even though
    ``opentelemetry-instrument`` built the processor before our code ran. Telemetry is best-effort:
    if ADOT isn't installed or its private internals moved, this logs nothing and returns ``False``
    rather than ever breaking the agent. Idempotent.
    """
    try:
        import importlib

        cls = importlib.import_module(_CW_PROCESSOR_MODULE).AwsCloudWatchOtlpBatchLogRecordProcessor
        original = cls._estimate_log_size
        if not isinstance(cls._MAX_LOG_REQUEST_BYTE_SIZE, int):
            return False
    except Exception:  # noqa: BLE001 — ADOT absent or changed shape (ImportError, AttributeError, …): skip the patch
        return False

    cls._MAX_LOG_REQUEST_BYTE_SIZE = budget_bytes
    if not getattr(cls, _PATCHED_FLAG, False):

        def _estimate_log_size(self: object, log: object, depth_: int = depth) -> int:
            return int(original(self, log, max(depth_, depth)))

        setattr(cls, _ORIGINAL_ESTIMATE, original)
        cls._estimate_log_size = _estimate_log_size
        setattr(cls, _PATCHED_FLAG, True)
    return True
