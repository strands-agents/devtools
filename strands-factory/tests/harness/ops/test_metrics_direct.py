"""Direct metric delivery (PutMetricData) for the deployed agent runtime.

On AgentCore Runtime *instances* the agent's stdout is re-wrapped as ``{"log": "<line>"}``, so the
EMF lines :func:`metrics.emit` used to print were never extracted and every alarm on an agent-side
metric went dark. The agent runtime now delivers its metrics with a buffered, background-flushing
``cloudwatch:PutMetricData`` emitter; Lambdas keep stdout EMF. These tests pin: the sink selection
(no double counting), the EMF → datum expansion (same namespace / names / dimension sets / units the
alarms and the dashboard read), batching limits, flush, fail-open and the bounded queue — plus a
guard that every harness metric an alarm in ``infra/stacks/monitoring_stack.py`` watches is
emitted by the harness and reaches CloudWatch through the agent-runtime path.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from strandly.ops import metrics

_ROOT = Path(__file__).resolve().parents[3]
_MONITORING = _ROOT / "infra" / "stacks" / "monitoring_stack.py"
NS = "Strandly-test"


class FakeCloudWatch:
    def __init__(self, fail: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def put_metric_data(self, **kw: Any) -> dict[str, Any]:
        if self.fail:
            self.fail -= 1
            raise RuntimeError("AccessDenied")
        self.calls.append(kw)
        return {}


@pytest.fixture
def fake_cw(monkeypatch):
    """The agent-runtime environment with a fake CloudWatch behind a thread-less emitter."""
    cw = FakeCloudWatch()
    em = metrics.PutMetricDataEmitter(lambda: cw, start_thread=False)
    monkeypatch.setattr(metrics, "_EMITTER", em)
    monkeypatch.setenv(metrics.NAMESPACE_ENV, NS)
    monkeypatch.setenv(metrics.RUNTIME_MARKER_ENV, "true")
    monkeypatch.delenv(metrics.SINK_ENV, raising=False)
    return cw, em


# --- sink selection -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("marker", "sink", "direct"),
    [
        (None, None, False),  # a Lambda / laptop: stdout EMF
        ("true", None, True),  # the deployed agent runtime
        ("true", "emf", False),  # explicit override back to EMF
        (None, "api", True),  # explicit opt-in
        ("false", None, False),
    ],
)
def test_direct_sink_selection(monkeypatch, marker, sink, direct):
    for var, val in ((metrics.RUNTIME_MARKER_ENV, marker), (metrics.SINK_ENV, sink)):
        if val is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, val)
    assert metrics.direct_sink() is direct


def test_runtime_marker_mirrors_core_constant():
    from strandly.core import constants

    assert metrics.RUNTIME_MARKER_ENV == constants.AGENTCORE_RUNTIME_MARKER_ENV


def test_agent_runtime_emit_goes_to_put_metric_data_not_stdout(fake_cw, capsys):
    cw, em = fake_cw
    assert metrics.emit({metrics.INVOCATIONS: 1}, surface=metrics.SURFACE_AGENTCORE)
    assert capsys.readouterr().out == ""  # no EMF line → nothing to double count
    assert em.flush() == 2
    (call,) = cw.calls
    assert call["Namespace"] == NS
    assert {(d["MetricName"], tuple(x["Name"] for x in d["Dimensions"])) for d in call["MetricData"]} == {
        ("Invocations", ()),
        ("Invocations", ("surface",)),
    }


def test_lambda_emit_stays_stdout_emf(monkeypatch, capsys):
    monkeypatch.setenv(metrics.NAMESPACE_ENV, NS)
    monkeypatch.delenv(metrics.RUNTIME_MARKER_ENV, raising=False)
    monkeypatch.delenv(metrics.SINK_ENV, raising=False)
    sentinel = metrics.PutMetricDataEmitter(lambda: pytest.fail("no API call"), start_thread=False)
    monkeypatch.setattr(metrics, "_EMITTER", sentinel)
    assert metrics.emit({metrics.POLL_SUCCESS: 1}, surface=metrics.SURFACE_POLLER)
    line = capsys.readouterr().out.strip()
    assert json.loads(line)["_aws"]["CloudWatchMetrics"][0]["Namespace"] == NS
    assert sentinel.pending() == 0


def test_disabled_namespace_is_noop_on_the_runtime(monkeypatch, fake_cw):
    _, em = fake_cw
    monkeypatch.delenv(metrics.NAMESPACE_ENV)
    assert metrics.emit({metrics.INVOCATIONS: 1}) is False
    assert em.pending() == 0


# --- EMF → datums: same names, dimension sets and units ------------------------------------------------


def test_emf_to_datums_preserves_dimension_sets_units_and_timestamp():
    doc = metrics.build_emf(
        {metrics.COMPLETED: 1, metrics.DURATION_MS: (1234, metrics.MILLISECONDS)},
        namespace=NS,
        surface=metrics.SURFACE_AGENTCORE,
        env="dev",
        timestamp_ms=1_700_000_000_000,
    )
    datums = metrics.emf_to_datums(doc)
    by_key = {(d["MetricName"], tuple((x["Name"], x["Value"]) for x in d["Dimensions"])): d for d in datums}
    assert set(by_key) == {
        ("Completed", ()),
        ("Completed", (("surface", "agentcore"),)),
        ("DurationMs", ()),
        ("DurationMs", (("surface", "agentcore"),)),
    }
    assert by_key[("DurationMs", ())]["Unit"] == "Milliseconds"
    assert by_key[("DurationMs", ())]["Value"] == 1234.0
    assert by_key[("Completed", ())]["Unit"] == "Count"
    assert all(d["_ns"] == NS for d in datums)
    assert by_key[("Completed", ())]["Timestamp"] == datetime.fromtimestamp(1_700_000_000, tz=by_key[("Completed", ())]["Timestamp"].tzinfo)
    # ``env`` rides as a plain field in EMF, never as a dimension.
    assert not any(x["Name"] == "env" for d in datums for x in d["Dimensions"])


def test_tool_call_rollup_suppressed_and_tool_name_dimension_kept(fake_cw):
    cw, em = fake_cw
    metrics.emit_tool_call("use_github", error=True)
    em.flush()
    data = cw.calls[0]["MetricData"]
    assert {(d["MetricName"], d["Value"]) for d in data} == {("ToolCalls", 1.0), ("ToolCallErrors", 1.0)}
    assert all(d["Dimensions"] == [{"Name": "tool_name", "Value": "use_github"}] for d in data)


# --- batching, flushing, fail-open, bounded queue ----------------------------------------------------


def _datums(n: int, ns: str = NS) -> list[dict[str, Any]]:
    doc = metrics.build_emf({metrics.TOOL_ERRORS: 0}, namespace=ns)
    return metrics.emf_to_datums(doc) * n


def test_batches_respect_the_1000_datum_cap():
    cw = FakeCloudWatch()
    em = metrics.PutMetricDataEmitter(lambda: cw, start_thread=False)
    em.put(_datums(2500))
    assert em.flush() == 2500
    assert [len(c["MetricData"]) for c in cw.calls] == [1000, 1000, 500]
    assert all("_ns" not in d for c in cw.calls for d in c["MetricData"])


def test_batches_respect_the_byte_cap(monkeypatch):
    monkeypatch.setattr(metrics, "PUT_MAX_BYTES", 2000)
    cw = FakeCloudWatch()
    em = metrics.PutMetricDataEmitter(lambda: cw, start_thread=False)
    em.put(_datums(50))
    em.flush()
    assert len(cw.calls) > 1
    for c in cw.calls:
        assert sum(metrics._datum_size(d) for d in c["MetricData"]) <= 2000


def test_batches_never_mix_namespaces():
    cw = FakeCloudWatch()
    em = metrics.PutMetricDataEmitter(lambda: cw, start_thread=False)
    em.put(_datums(2, "A") + _datums(1, "B") + _datums(2, "A"))
    em.flush()
    assert [(c["Namespace"], len(c["MetricData"])) for c in cw.calls] == [("A", 4), ("B", 1)]


def test_put_never_does_io_and_flush_drains():
    cw = FakeCloudWatch()
    em = metrics.PutMetricDataEmitter(lambda: cw, start_thread=False)
    em.put(_datums(3))
    assert cw.calls == [] and em.pending() == 3
    em.flush()
    assert em.pending() == 0 and em.sent_datums == 3 and em.sent_calls == 1


def test_fail_open_logs_counts_and_keeps_going(caplog):
    cw = FakeCloudWatch(fail=1)
    em = metrics.PutMetricDataEmitter(lambda: cw, start_thread=False)
    em.put(_datums(5))
    with caplog.at_level("WARNING"):
        assert em.flush() == 0  # never raises
    assert em.failed_calls == 1 and em.dropped_datums == 5
    assert "PutMetricData failed" in caplog.text
    em.put(_datums(2))
    assert em.flush() == 2  # the next flush is unaffected


def test_client_construction_failure_is_fail_open():
    def boom():
        raise RuntimeError("no credentials")

    em = metrics.PutMetricDataEmitter(boom, start_thread=False)
    em.put(_datums(1))
    assert em.flush() == 0 and em.failed_calls == 1


def test_queue_is_bounded_and_drops_oldest():
    cw = FakeCloudWatch()
    em = metrics.PutMetricDataEmitter(lambda: cw, start_thread=False, max_queue=10)
    for i in range(15):
        em.put([{**_datums(1)[0], "Value": float(i)}])
    assert em.pending() == 10 and em.dropped_datums == 5
    em.flush()
    assert [d["Value"] for d in cw.calls[0]["MetricData"]] == [float(i) for i in range(5, 15)]


def test_background_thread_flushes_on_its_interval():
    import time

    cw = FakeCloudWatch()
    em = metrics.PutMetricDataEmitter(lambda: cw, flush_interval_s=0.05)
    em.put(_datums(3))
    deadline = time.monotonic() + 3
    while not cw.calls and time.monotonic() < deadline:
        time.sleep(0.02)
    assert cw.calls and sum(len(c["MetricData"]) for c in cw.calls) == 3


def test_module_flush_is_a_noop_without_an_emitter(monkeypatch):
    monkeypatch.setattr(metrics, "_EMITTER", None)
    assert metrics.flush() == 0


def test_emit_survives_a_broken_emitter(fake_cw, monkeypatch):
    _, em = fake_cw
    monkeypatch.setattr(em, "put", lambda d: (_ for _ in ()).throw(RuntimeError("x")))
    assert metrics.emit({metrics.INVOCATIONS: 1}) is False  # swallowed, never raised


# --- guard: every alarmed harness metric reaches CloudWatch from the agent runtime ------------------


def _monitoring_metric_constants() -> dict[str, str]:
    """``_NAME = "Metric"`` constants in monitoring_stack.py that name a harness (ops.metrics) metric."""
    harness_names = {
        v for k, v in vars(metrics).items() if k.isupper() and isinstance(v, str) and v[:1].isupper()
    }
    out: dict[str, str] = {}
    for node in ast.parse(_MONITORING.read_text()).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.startswith("_")
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node.value.value in harness_names
        ):
            out[node.targets[0].id] = node.value.value
    return out


def _alarmed_metric_names() -> set[str]:
    """Harness metric names monitoring_stack.py actually USES (its constant appears beyond its def).

    Every such use is a ``cw.Metric`` on the Strandly namespace with no dimensions (``metric`` /
    ``harness_metric`` / the alarm loops) — i.e. the namespace-level rollup.
    """
    src = _MONITORING.read_text()
    return {
        name
        for const, name in _monitoring_metric_constants().items()
        if len(re.findall(rf"\b{re.escape(const)}\b", src)) >= 2
    }


def test_guard_alarm_metric_list_is_not_empty():
    names = _alarmed_metric_names()
    # The ones the agent runtime emits — the ones that went dark on Runtime instances.
    for expected in (
        "Invocations",
        "Completed",
        "Failures",
        "ToolErrors",
        "LedgerWriteFailed",
        "SessionWriteFailed",
        "SessionReadFailed",
        "TranscriptWriteFailed",
        "WorkProductParseFailed",
        "SandboxTtlUpsertFailed",
        "SandboxTtlMisconfigured",
    ):
        assert expected in names, f"{expected} no longer alarmed? update this guard"


def _emitted_in_harness(name: str) -> bool:
    """Is the metric constant for ``name`` referenced by some emit site outside ops/metrics.py?"""
    consts = [k for k, v in vars(metrics).items() if k.isupper() and v == name]
    pattern = re.compile(r"\b(?:metrics\.)?(" + "|".join(map(re.escape, consts)) + r")\b")
    for path in (_ROOT / "harness" / "src" / "strandly").rglob("*.py"):
        if path.name == "metrics.py" and path.parent.name == "ops":
            continue
        if pattern.search(path.read_text()):
            return True
    return False


@pytest.mark.parametrize("name", sorted(_alarmed_metric_names()))
def test_guard_every_alarmed_metric_is_emitted_and_reaches_cloudwatch_on_the_runtime(name, fake_cw):
    """Each alarmed metric: (1) has an emit site in the harness, (2) on the agent runtime it is
    delivered by PutMetricData under the namespace with the EMPTY dimension set the alarms read."""
    assert _emitted_in_harness(name), f"no harness emit site references the {name} constant"
    cw, em = fake_cw
    assert metrics.emit({name: 1}, surface=metrics.SURFACE_AGENTCORE)
    em.flush()
    rollups = [
        d
        for c in cw.calls
        if c["Namespace"] == NS
        for d in c["MetricData"]
        if d["MetricName"] == name and d["Dimensions"] == []
    ]
    assert rollups, f"{name} did not reach PutMetricData with the namespace-level rollup"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_values_are_skipped_not_sent(fake_cw, bad):
    # PutMetricData rejects the WHOLE request on a NaN/Inf, taking every good datum in the batch
    # with it — so a non-finite value is dropped at expansion time.
    doc = metrics.build_emf({metrics.DURATION_MS: bad, metrics.COMPLETED: 1}, namespace=NS)
    names = {d["MetricName"] for d in metrics.emf_to_datums(doc)}
    assert metrics.DURATION_MS not in names
    assert metrics.COMPLETED in names


def test_request_flush_wakes_the_background_flusher(monkeypatch):
    import threading

    sent = threading.Event()

    class CW(FakeCloudWatch):
        def put_metric_data(self, **kw: Any) -> dict[str, Any]:
            super().put_metric_data(**kw)
            sent.set()
            return {}

    cw = CW()
    # An hour-long interval: only an explicit wake can make this flush happen within the test.
    em = metrics.PutMetricDataEmitter(lambda: cw, flush_interval_s=3600)
    monkeypatch.setattr(metrics, "_EMITTER", em)
    em.put([{"_ns": NS, "MetricName": "X", "Dimensions": [], "Value": 1.0, "Unit": "Count"}])
    metrics.request_flush()  # returns immediately; the flusher thread sends
    assert sent.wait(5)
    assert sum(len(c["MetricData"]) for c in cw.calls) == 1


def test_module_request_flush_is_a_noop_without_an_emitter(monkeypatch):
    monkeypatch.setattr(metrics, "_EMITTER", None)
    metrics.request_flush()  # no emitter, no thread, no raise
