"""Tests for OTEL guards: env sanitation (stray xray propagator) and CloudWatch OTLP log batch sizing."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from strandly import otel_guard
from strandly.otel_guard import sanitize_otel_env


def test_drops_xray_keeps_others():
    env = {"OTEL_PROPAGATORS": "xray,tracecontext,baggage"}
    sanitize_otel_env(env)
    assert env["OTEL_PROPAGATORS"] == "tracecontext,baggage"


def test_xray_only_falls_back_to_default():
    env = {"OTEL_PROPAGATORS": "xray"}
    sanitize_otel_env(env)
    assert env["OTEL_PROPAGATORS"] == "tracecontext,baggage"


def test_case_and_alias_insensitive():
    env = {"OTEL_PROPAGATORS": "AWS_XRAY,b3"}
    sanitize_otel_env(env)
    assert env["OTEL_PROPAGATORS"] == "b3"


def test_no_xray_is_left_untouched():
    env = {"OTEL_PROPAGATORS": "tracecontext,baggage"}
    sanitize_otel_env(env)
    assert env["OTEL_PROPAGATORS"] == "tracecontext,baggage"


def test_unset_is_noop():
    env: dict[str, str] = {}
    sanitize_otel_env(env)
    assert "OTEL_PROPAGATORS" not in env


def test_sampler_survives_sanitation():
    env = {
        "OTEL_PROPAGATORS": "baggage,xray,tracecontext",
        "OTEL_TRACES_SAMPLER": "traceidratio",
        "OTEL_TRACES_SAMPLER_ARG": "0.5",
    }
    sanitize_otel_env(env)
    assert env == {
        "OTEL_PROPAGATORS": "baggage,tracecontext",
        "OTEL_TRACES_SAMPLER": "traceidratio",
        "OTEL_TRACES_SAMPLER_ARG": "0.5",
    }


_SAMPLER_PROBE = """
import os
import strandly  # the early sanitation, as main.py runs it
from amazon.opentelemetry.distro.aws_opentelemetry_configurator import _custom_import_sampler
from opentelemetry.sdk._configuration import _get_sampler
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, set_span_in_context

# An ingress Lambda's forwarded header: a remote parent with Sampled=0.
parent = set_span_in_context(NonRecordingSpan(SpanContext(
    trace_id=0x5F0000000000000000000000000000AB, span_id=0xAB, is_remote=True,
    trace_flags=TraceFlags(TraceFlags.DEFAULT),
)))
sampler = _custom_import_sampler(_get_sampler(), Resource.create({}))
result = sampler.should_sample(parent, 0x5F0000000000000000000000000000AB, "invocation")
print(os.environ.get("OTEL_TRACES_SAMPLER"), result.decision.is_sampled())
"""


@pytest.mark.parametrize(
    "sampler, expected",
    [("always_on", "always_on True"), (None, "None False")],
)
def test_adot_samples_under_an_unsampled_caller_only_with_the_runtime_sampler(sampler, expected):
    """ADOT reads ``OTEL_TRACES_SAMPLER`` after the package's early sanitation; its default,
    ``parentbased_always_on``, follows the caller's Sampled=0."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    env["OTEL_PROPAGATORS"] = "baggage,xray,tracecontext"
    if sampler:
        env["OTEL_TRACES_SAMPLER"] = sampler
    out = subprocess.run(
        [sys.executable, "-c", _SAMPLER_PROBE], env=env, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip().splitlines()[-1] == expected


# ---------------------------------------------------------------------------------------------------
# cap_cloudwatch_log_batches — exercised against the real ADOT processor and the real OTLP encoder.
# ---------------------------------------------------------------------------------------------------

_CW_LIMIT = 1024 * 1024  # CloudWatch OTLP logs request limit


def _adot_processor_cls():
    mod = pytest.importorskip(otel_guard._CW_PROCESSOR_MODULE)
    return mod.AwsCloudWatchOtlpBatchLogRecordProcessor


@pytest.fixture
def restore_adot_processor():
    """Patches are class-level; put the class back exactly as found so tests stay independent."""
    cls = _adot_processor_cls()
    keys = (
        "_MAX_LOG_REQUEST_BYTE_SIZE",
        "_estimate_log_size",
        otel_guard._PATCHED_FLAG,
        otel_guard._ORIGINAL_ESTIMATE,
    )
    saved = {k: cls.__dict__[k] for k in keys if k in cls.__dict__}
    # Start from ADOT's pristine defaults even if build_app() already patched the class this session.
    cls._estimate_log_size = saved.get(otel_guard._ORIGINAL_ESTIMATE, saved["_estimate_log_size"])
    cls._MAX_LOG_REQUEST_BYTE_SIZE = _CW_LIMIT
    for k in (otel_guard._PATCHED_FLAG, otel_guard._ORIGINAL_ESTIMATE):
        if k in cls.__dict__:
            delattr(cls, k)
    yield cls
    for k in keys:
        if k in saved:
            setattr(cls, k, saved[k])
        elif k in cls.__dict__:
            delattr(cls, k)


def _genai_record(text_bytes: int):
    """A log record shaped like strands' gen_ai_latest_experimental events (see prod otel-rt-logs):
    the bulky text sits at depth 5 (body → input → messages → [msg] → parts → [part] → content)."""
    from opentelemetry._logs import LogRecord
    from opentelemetry.sdk._logs import ReadableLogRecord
    from opentelemetry.sdk.resources import Resource

    part = {"type": "text", "content": "x" * text_bytes}
    body = {
        "input": {"messages": [{"role": "user", "parts": [part]}]},
        "output": {
            "messages": [{"role": "assistant", "parts": [{"type": "text", "content": "ok"}]}]
        },
    }
    rec = LogRecord(
        body=body,
        attributes={"session.id": "slack-T0-D0", "event.name": "strands.telemetry.tracer"},
    )
    return ReadableLogRecord(
        rec, resource=Resource.create({"service.name": "strandly_prod.DEFAULT"})
    )


class _CapturingExporter:
    def __init__(self) -> None:
        self.batches: list[list] = []

    def export(self, batch):
        from opentelemetry.sdk._logs.export import LogRecordExportResult

        self.batches.append(list(batch))
        return LogRecordExportResult.SUCCESS

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _encoded_sizes(cls, records) -> list[int]:
    """Run records through the processor's size-aware batching; return each request's real wire size."""
    from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs

    exporter = _CapturingExporter()
    proc = cls(exporter, schedule_delay_millis=60_000, max_export_batch_size=512)
    try:
        for r in records:
            proc._batch_processor._queue.appendleft(r)
        proc.force_flush()
    finally:
        proc.shutdown()
    assert sum(len(b) for b in exporter.batches) == len(records)
    return [len(encode_logs(b).SerializeToString()) for b in exporter.batches]


def test_unpatched_adot_batches_overflow_cloudwatch_limit(restore_adot_processor):
    """Reproduces prod's `400 Upload too large`: deep GenAI text is invisible to the depth-3 estimate."""
    sizes = _encoded_sizes(restore_adot_processor, [_genai_record(300_000) for _ in range(6)])
    assert max(sizes) > _CW_LIMIT


def test_cap_keeps_every_request_under_cloudwatch_limit(restore_adot_processor):
    assert otel_guard.cap_cloudwatch_log_batches() is True
    records = [
        _genai_record(n) for n in (300_000, 250_000, 5_000, 400_000, 120_000, 60_000, 700_000, 10)
    ]
    sizes = _encoded_sizes(restore_adot_processor, records)
    assert len(sizes) > 1  # it still batches, it just batches honestly
    assert max(sizes) < _CW_LIMIT


def test_cap_still_batches_small_records_together(restore_adot_processor):
    otel_guard.cap_cloudwatch_log_batches()
    sizes = _encoded_sizes(restore_adot_processor, [_genai_record(1_000) for _ in range(50)])
    assert len(sizes) == 1


def test_cap_is_idempotent(restore_adot_processor):
    cls = restore_adot_processor
    assert otel_guard.cap_cloudwatch_log_batches() is True
    patched = cls._estimate_log_size
    assert otel_guard.cap_cloudwatch_log_batches() is True
    assert cls._estimate_log_size is patched  # not wrapped twice
    assert cls._MAX_LOG_REQUEST_BYTE_SIZE == otel_guard.CW_LOG_REQUEST_BUDGET_BYTES


def test_cap_is_a_noop_without_adot(monkeypatch):
    import importlib

    real = importlib.import_module

    def fake(name, *a, **kw):
        if name == otel_guard._CW_PROCESSOR_MODULE:
            raise ImportError(name)
        return real(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", fake)
    assert otel_guard.cap_cloudwatch_log_batches() is False


def test_fixture_resets_even_if_already_patched(restore_adot_processor):
    otel_guard.cap_cloudwatch_log_batches()  # simulate build_app() having run earlier
    cls = restore_adot_processor
    assert cls._MAX_LOG_REQUEST_BYTE_SIZE == otel_guard.CW_LOG_REQUEST_BUDGET_BYTES
