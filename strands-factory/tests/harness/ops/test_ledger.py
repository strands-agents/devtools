"""Run-ledger tests — hermetic (no AWS): a fake DynamoDB Table captures the writes."""

from __future__ import annotations

from typing import Any

from strandly.core.config import Config
from strandly.ops.ledger import GSI_PARTITION_VALUE, RunLedger


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


def test_finish_records_cache_tokens():
    led, table = _ledger()
    led.finish("t1", usage={"input": 10, "output": 5, "total": 1515, "cacheRead": 1000,
                            "cacheWrite": 500})
    (item,) = table.items
    assert item["tokens_cache_read"] == 1000 and item["tokens_cache_write"] == 500


def test_finish_without_usage_omits_token_keys():
    led, table = _ledger()
    led.finish("t1", result="ok")
    (item,) = table.items
    assert "tokens_in" not in item and "tokens_total" not in item
    assert "tokens_cache_read" not in item and "tokens_cache_write" not in item


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


def test_finish_records_tool_errors_and_stop_reason():
    # tool_errors is written even when 0 — a healthy post-upgrade run must be distinguishable
    # from a pre-upgrade row that never carried the field.
    led, table = _ledger()
    led.finish("t1", result="ok", tool_errors=0, stop_reason="end_turn")
    (item,) = table.items
    assert item["tool_errors"] == 0
    assert item["stop_reason"] == "end_turn"


def test_finish_with_tool_errors_count():
    led, table = _ledger()
    led.finish("t1", result="couldn't do it", tool_errors=4, stop_reason="max_tokens")
    (item,) = table.items
    assert item["tool_errors"] == 4 and item["stop_reason"] == "max_tokens"


def test_finish_omits_qualifiers_when_not_provided():
    # Callers that don't track the signals (older paths) keep writing clean rows.
    led, table = _ledger()
    led.finish("t1", result="ok")
    (item,) = table.items
    assert "tool_errors" not in item and "stop_reason" not in item


def test_fail_records_tool_errors():
    led, table = _ledger()
    led.fail("t1", error="boom", tool_errors=2)
    (item,) = table.items
    assert item["status"] == "failed" and item["tool_errors"] == 2


def test_model_is_written_on_start_and_terminal_rows():
    # The run's model-preset name rides every write so the dashboard's per-model token
    # breakdown can attribute usage even across STRANDLY_MODEL flips between deploys.
    led, table = _ledger()
    led.start("t1", model="opus-4-8")
    led.finish("t1", result="ok", model="opus-4-8")
    led.fail("t2", error="boom", model="opus-5")
    start_row, finish_row, fail_row = table.items
    assert start_row["model"] == "opus-4-8"
    assert finish_row["model"] == "opus-4-8"
    assert fail_row["model"] == "opus-5"


def test_model_omitted_when_not_provided():
    # Older callers (and rows) simply don't carry the field — the dashboard groups them
    # under "unknown" instead of the ledger inventing a value.
    led, table = _ledger()
    led.start("t1")
    led.finish("t1", result="ok")
    led.fail("t2", error="boom")
    assert all("model" not in item for item in table.items)


def test_sigterm_fail_is_conditional_on_the_row_still_running(monkeypatch):
    """P3-C: the shutdown write never overwrites a run that finished meanwhile."""
    from botocore.exceptions import ClientError

    from strandly.ops import ledger as ledger_module

    emitted: list[dict] = []
    monkeypatch.setattr(ledger_module.metrics, "emit", lambda values, **_: emitted.append(values))
    calls: list[dict[str, Any]] = []

    class ConditionalTable:
        def put_item(self, **kwargs: Any) -> None:
            calls.append(kwargs)
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}}, "PutItem"
            )

    led = RunLedger("strandly-runs", region="us-west-2", table=ConditionalTable())
    led.fail("t1", error="RuntimeTerminated", only_if_running=True)
    (call,) = calls
    assert call["Item"]["status"] == "failed"
    assert call["ConditionExpression"] == "#status = :running"
    assert call["ExpressionAttributeNames"] == {"#status": "status"}
    assert call["ExpressionAttributeValues"] == {":running": "running"}
    assert emitted == []  # a row that already finished is not a failed write

    led.fail("t2", error="boom")  # ordinary fails stay unconditional
    assert "ConditionExpression" not in calls[-1]


def test_every_row_of_a_portal_run_records_who_requested_it():
    led, table = _ledger()
    led.start("t1", session_id="s1", requested_by="person:octo")
    led.finish("t1", result="ok", requested_by="person:octo")
    led.fail("t2", error="boom", requested_by="portal:11111111-1111-4111-8111-111111111111")
    led.start("t3", session_id="s3")
    assert [i.get("requested_by") for i in table.items] == [
        "person:octo", "person:octo", "portal:11111111-1111-4111-8111-111111111111", None,
    ]
