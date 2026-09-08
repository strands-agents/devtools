"""Run-ledger tests — hermetic (no AWS): a fake DynamoDB Table captures the writes."""

from __future__ import annotations

from typing import Any

from strandly_harness.core.config import Config
from strandly_harness.ops.ledger import GSI_PARTITION_VALUE, RunLedger


class FakeTable:
    """Records put_item calls; can be told to raise to exercise the fail-open path."""

    def __init__(self, raise_on_put: bool = False):
        self.items: list[dict[str, Any]] = []
        self.raise_on_put = raise_on_put

    def put_item(self, *, Item: dict[str, Any]) -> None:  # noqa: N803 - boto3 kwarg name
        if self.raise_on_put:
            raise RuntimeError("dynamo unavailable")
        self.items.append(Item)


def _ledger(**kw: Any) -> tuple[RunLedger, FakeTable]:
    table = FakeTable(**kw)
    return RunLedger("strandly-runs", region="us-west-2", table=table), table


def test_from_config_disabled_returns_none():
    assert RunLedger.from_config(Config(values={})) is None


def test_from_config_enabled_returns_ledger():
    cfg = Config(values={"STRANDLY_RUN_LEDGER_TABLE": "t", "AWS_REGION": "us-west-2"})
    led = RunLedger.from_config(cfg)
    assert isinstance(led, RunLedger)
    assert led.table_name == "t" and led.region == "us-west-2"


def test_start_writes_running_row_with_gsi_key():
    led, table = _ledger()
    led.start("t1", session_id="s1", github_target="https://github.com/o/r/pull/9", repo="o/r",
              prompt="review pr", trace_id="1-abc", started_at="2026-01-01T00:00:00+00:00")
    (item,) = table.items
    assert item["task_id"] == "t1"
    assert item["status"] == "running"
    assert item["gsi_pk"] == GSI_PARTITION_VALUE
    assert item["started_at"] == "2026-01-01T00:00:00+00:00"
    assert item["github_target"].endswith("/pull/9")
    assert item["repo"] == "o/r"
    assert item["trace_id"] == "1-abc"
    assert item["prompt"] == "review pr"


def test_finish_records_status_tokens_and_duration():
    led, table = _ledger()
    led.finish("t1", result="all good", usage={"input": 100, "output": 50, "total": 150},
               duration_ms=1234, started_at="2026-01-01T00:00:00+00:00")
    (item,) = table.items
    assert item["status"] == "completed"
    assert item["result_summary"] == "all good"
    assert item["tokens_in"] == 100 and item["tokens_out"] == 50 and item["tokens_total"] == 150
    assert item["duration_ms"] == 1234
    assert item["started_at"] == "2026-01-01T00:00:00+00:00"
    assert "ended_at" in item


def test_finish_without_usage_omits_token_keys():
    led, table = _ledger()
    led.finish("t1", result="ok")
    (item,) = table.items
    assert "tokens_in" not in item and "tokens_total" not in item


def test_fail_records_error():
    led, table = _ledger()
    led.fail("t2", error="ValueError: boom", duration_ms=5)
    (item,) = table.items
    assert item["status"] == "failed"
    assert item["error"] == "ValueError: boom"
    assert item["duration_ms"] == 5


def test_result_summary_is_truncated():
    led, table = _ledger()
    led.finish("t1", result="x" * 10_000)
    (item,) = table.items
    assert len(item["result_summary"]) == 4_000


def test_empty_optional_fields_are_omitted():
    led, table = _ledger()
    led.start("t1")  # no session/target/repo/trace/prompt
    (item,) = table.items
    for absent in ("session_id", "github_target", "repo", "trace_id", "prompt"):
        assert absent not in item


def test_writes_are_fail_open():
    """A DynamoDB error must be swallowed — telemetry can't break a run."""
    led, _ = _ledger(raise_on_put=True)
    # None of these may raise.
    led.start("t1")
    led.finish("t1", result="ok", usage={"input": 1})
    led.fail("t1", error="boom")


def test_lazy_table_not_built_on_construction():
    """Constructing a ledger must not touch boto3/AWS; the table is built lazily on first use."""
    led = RunLedger("t", region=None)  # no injected table
    assert led._table is None


def test_finish_recarries_prompt_on_terminal_write():
    """put_item REPLACES the start() row, so finish()/fail() must re-carry the prompt.

    Regression for the dashboard's sessions list showing "(no description)" everywhere:
    the prompt written at start() vanished the moment the run finished.
    """
    led, table = _ledger()
    led.start("t1", prompt="fix the flaky test")
    led.finish("t1", result="done", prompt="fix the flaky test")
    assert table.items[-1]["prompt"] == "fix the flaky test"


def test_fail_recarries_prompt_and_clips_it():
    led, table = _ledger()
    led.fail("t1", error="boom", prompt="p" * 10_000)
    item = table.items[-1]
    assert item["status"] == "failed"
    assert len(item["prompt"]) == 4_000  # clipped to the result-summary limit


def test_terminal_write_without_prompt_omits_key():
    led, table = _ledger()
    led.finish("t1", result="done")  # legacy caller that doesn't pass a prompt
    assert "prompt" not in table.items[-1]
