"""Tests for OTEL env sanitation (drops a stray xray propagator that would crash on import)."""

from __future__ import annotations

from strandly_harness.otel_guard import sanitize_otel_env


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
