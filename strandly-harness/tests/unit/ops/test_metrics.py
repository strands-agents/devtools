"""Tests for the EMF metrics emitter (``strandly_harness.ops.metrics``) + its config gate."""

from __future__ import annotations

import json

from strandly_harness.core.config import Config
from strandly_harness.ops import metrics


def test_disabled_by_default_is_noop(capsys, monkeypatch):
    monkeypatch.delenv(metrics.NAMESPACE_ENV, raising=False)
    assert metrics.enabled() is False
    assert metrics.namespace() is None
    assert metrics.emit({metrics.INVOCATIONS: 1}, surface=metrics.SURFACE_AGENTCORE) is False
    assert capsys.readouterr().out == ""  # nothing written when disabled


def test_emit_writes_one_emf_line_when_enabled(capsys, monkeypatch):
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "Strandly-dev")
    monkeypatch.setenv(metrics.ENV_ENV, "dev")
    assert metrics.enabled() is True
    assert metrics.emit({metrics.INVOCATIONS: 1}, surface=metrics.SURFACE_AGENTCORE) is True

    out = capsys.readouterr().out.strip()
    assert "\n" not in out  # exactly one line
    doc = json.loads(out)
    cw = doc["_aws"]["CloudWatchMetrics"][0]
    assert cw["Namespace"] == "Strandly-dev"
    # Both the namespace-level rollup ([]) and the [surface] drill-down set are declared.
    assert [] in cw["Dimensions"]
    assert ["surface"] in cw["Dimensions"]
    assert {"Name": metrics.INVOCATIONS, "Unit": metrics.COUNT} in cw["Metrics"]
    assert doc[metrics.INVOCATIONS] == 1
    assert doc["surface"] == metrics.SURFACE_AGENTCORE
    assert doc["env"] == "dev"


def test_build_emf_tuple_value_carries_unit():
    doc = metrics.build_emf(
        {metrics.DURATION_MS: (1234, metrics.MILLISECONDS)},
        namespace="Strandly-dev",
        surface=metrics.SURFACE_AGENTCORE,
        timestamp_ms=42,
    )
    assert doc["_aws"]["Timestamp"] == 42
    assert doc[metrics.DURATION_MS] == 1234
    assert {"Name": metrics.DURATION_MS, "Unit": metrics.MILLISECONDS} in (
        doc["_aws"]["CloudWatchMetrics"][0]["Metrics"]
    )


def test_build_emf_no_surface_only_rollup_dimension():
    doc = metrics.build_emf({metrics.STUCK_RUNS: 0}, namespace="Strandly-dev")
    dims = doc["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
    assert dims == [[]]  # only the namespace-level rollup, no [surface]
    assert "surface" not in doc


def test_emit_empty_metrics_is_noop(capsys, monkeypatch):
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "Strandly-dev")
    assert metrics.emit({}) is False
    assert capsys.readouterr().out == ""


def test_emit_is_fail_open_on_bad_value(capsys, monkeypatch):
    # A non-JSON-serializable value must not raise — emit swallows and returns False.
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "Strandly-dev")
    assert metrics.emit({metrics.INVOCATIONS: object()}, surface="x") is False


def test_config_metrics_gate(monkeypatch):
    assert Config(values={}).metrics_enabled is False
    assert Config(values={}).metrics_namespace is None
    cfg = Config(values={"STRANDLY_METRICS_NAMESPACE": "Strandly-prod"})
    assert cfg.metrics_enabled is True
    assert cfg.metrics_namespace == "Strandly-prod"
