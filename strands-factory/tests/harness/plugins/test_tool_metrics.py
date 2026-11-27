"""Tests for the ToolMetrics plugin — per-tool call/error rollup on AfterToolCallEvent."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from strandly.ops import metrics
from strandly.plugins.tool_metrics import ToolMetrics, _is_cancelled, _is_error


def _event(name, *, result=None, exception=None, cancel_message=None):
    return SimpleNamespace(
        tool_use={"name": name} if name is not None else {},
        result=result,
        exception=exception,
        cancel_message=cancel_message,
    )


def test_is_error_true_on_error_result():
    assert _is_error(_event("read", result={"status": "error"})) is True


def test_is_error_true_on_exception():
    assert _is_error(_event("read", result={"status": "success"}, exception=RuntimeError("x"))) is True


def test_is_error_false_on_success():
    assert _is_error(_event("read", result={"status": "success"})) is False


def test_is_cancelled_via_cancel_message():
    # The user-cancel path sets status="error" AND cancel_message — must NOT count as an error.
    ev = _event("read", result={"status": "error"}, cancel_message="tool cancelled by user")
    assert _is_cancelled(ev) is True


def test_is_cancelled_via_result_flag():
    assert _is_cancelled(_event("read", result={"status": "error", "cancelled": True})) is True


def test_not_cancelled_on_a_plain_error():
    assert _is_cancelled(_event("read", result={"status": "error"})) is False


@pytest.mark.asyncio
async def test_emits_per_tool_metric_on_error(capsys, monkeypatch):
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "Strandly-dev")
    await ToolMetrics()._on_tool_call(_event("programmatic_tool_caller", result={"status": "error"}))
    doc = json.loads(capsys.readouterr().out.strip())
    assert doc[metrics.TOOL_NAME_DIM] == "programmatic_tool_caller"
    assert doc[metrics.TOOL_CALLS] == 1
    assert doc[metrics.TOOL_CALL_ERRORS] == 1


@pytest.mark.asyncio
async def test_cancelled_call_emits_nothing(capsys, monkeypatch):
    # A cancelled call never ran — excluded from both numerator and denominator, so no line at all.
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "Strandly-dev")
    await ToolMetrics()._on_tool_call(
        _event("read", result={"status": "error"}, cancel_message="tool cancelled by user")
    )
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_missing_tool_name_is_skipped(capsys, monkeypatch):
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "Strandly-dev")
    await ToolMetrics()._on_tool_call(_event(None, result={"status": "success"}))
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_never_raises_on_a_malformed_event(capsys, monkeypatch):
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "Strandly-dev")
    # tool_use is not a dict and there's no result — the hook must swallow, not propagate.
    await ToolMetrics()._on_tool_call(SimpleNamespace(tool_use="oops", result=None, exception=None))
    assert capsys.readouterr().out == ""
