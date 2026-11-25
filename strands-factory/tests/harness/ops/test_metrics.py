"""Tests for the EMF metrics emitter (``strandly.ops.metrics``) + its config gate."""

from __future__ import annotations

import json
import os

from strandly.core.config import Config
from strandly.ops import metrics


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


def test_configure_bridges_a_secret_only_namespace_into_the_env(monkeypatch):
    """The deployed runtime's namespace arrives in the config SECRET, never as a container env var
    (the toolkit — not the CDK — creates the runtime), and ``emit`` gates on ``os.environ`` only.
    Without this bridge every runtime emit stayed a no-op."""
    # setenv("") rather than delenv(): "" is falsy, so it reads as unset to the gate AND to
    # `configure`, and monkeypatch removes the key again at teardown — so the namespace this test
    # causes to be set can't leak into another test's emits (delenv on an absent var records
    # nothing to restore).
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "")
    cfg = Config(values={"STRANDLY_METRICS_NAMESPACE": "Strandly-prod"})

    assert metrics.configure(cfg.metrics_namespace) is True
    assert metrics.namespace() == "Strandly-prod"
    assert metrics.enabled() is True


def test_configure_never_overrides_an_explicit_env_var(monkeypatch):
    # Same precedence as Config.load: the process env beats the secret, so an explicit
    # `--env STRANDLY_METRICS_NAMESPACE=…` on a runtime is never silently rewritten.
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "Strandly-explicit")
    assert metrics.configure("Strandly-from-secret") is False
    assert metrics.namespace() == "Strandly-explicit"


def test_configure_with_no_namespace_leaves_metrics_disabled(monkeypatch):
    # An unconfigured deployment (and every local run / test) must stay a pure no-op.
    monkeypatch.delenv(metrics.NAMESPACE_ENV, raising=False)
    assert metrics.configure(Config(values={}).metrics_namespace) is False
    assert metrics.enabled() is False
    assert metrics.NAMESPACE_ENV not in os.environ


def test_build_emf_dimensions_add_per_key_sets_and_fields():
    doc = metrics.build_emf(
        {metrics.TOOL_CALLS: 1, metrics.TOOL_CALL_ERRORS: 0},
        namespace="Strandly-dev",
        dimensions={metrics.TOOL_NAME_DIM: "programmatic_tool_caller"},
        rollup=False,
    )
    cw = doc["_aws"]["CloudWatchMetrics"][0]
    # rollup=False suppresses the empty set so a GROUP BY tool_name read can't double-count it.
    assert [] not in cw["Dimensions"]
    assert [metrics.TOOL_NAME_DIM] in cw["Dimensions"]
    assert doc[metrics.TOOL_NAME_DIM] == "programmatic_tool_caller"
    assert doc[metrics.TOOL_CALLS] == 1
    assert doc[metrics.TOOL_CALL_ERRORS] == 0


def test_build_emf_rollup_false_without_dims_falls_back_to_empty_set():
    # Defensive: EMF needs at least one dimension set; a mis-call must not emit an invalid doc.
    doc = metrics.build_emf({metrics.TOOL_CALLS: 1}, namespace="Strandly-dev", rollup=False)
    assert doc["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [[]]


def test_emit_tool_call_writes_per_tool_series(capsys, monkeypatch):
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "Strandly-dev")
    assert metrics.emit_tool_call("programmatic_tool_caller", error=True) is True
    doc = json.loads(capsys.readouterr().out.strip())
    cw = doc["_aws"]["CloudWatchMetrics"][0]
    assert cw["Dimensions"] == [[metrics.TOOL_NAME_DIM]]  # per-tool only, no empty rollup
    assert doc[metrics.TOOL_NAME_DIM] == "programmatic_tool_caller"
    assert doc[metrics.TOOL_CALLS] == 1
    assert doc[metrics.TOOL_CALL_ERRORS] == 1


def test_emit_tool_call_success_counts_zero_errors(capsys, monkeypatch):
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "Strandly-dev")
    assert metrics.emit_tool_call("read", error=False) is True
    doc = json.loads(capsys.readouterr().out.strip())
    assert doc[metrics.TOOL_CALLS] == 1
    assert doc[metrics.TOOL_CALL_ERRORS] == 0


def test_emit_tool_call_disabled_is_noop(capsys, monkeypatch):
    monkeypatch.delenv(metrics.NAMESPACE_ENV, raising=False)
    assert metrics.emit_tool_call("read", error=False) is False
    assert capsys.readouterr().out == ""
