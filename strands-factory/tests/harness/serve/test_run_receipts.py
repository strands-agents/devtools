from __future__ import annotations

import asyncio

import pytest

from strandly.core.config import Config
from strandly.core.events import HarnessEvent
from strandly.serve import agentcore_app as ac
from strandly.serve import run_receipts


@pytest.fixture
def config(tmp_path, monkeypatch):
    import strandly.memory.session_store as storage

    monkeypatch.setattr(storage, "LOCAL_SESSION_STORE_DIR", tmp_path)
    return Config(values={})


def payload(result_id="a" * 32):
    return {"resultId": result_id, "sessionId": "acp-test", "prompt": "hello"}


async def settled(invoke, body):
    for _ in range(200):
        result = await invoke({**body, "action": "poll"})
        if result["status"] in {"completed", "failed", "cancelled"}:
            return result
        await asyncio.sleep(0.01)
    pytest.fail("run did not settle")


@pytest.mark.asyncio
async def test_receipt_is_exact_task_final_text_not_reasoning_tools_or_old_snapshot(
    config, monkeypatch
):
    async def turn(*args, **kwargs):
        yield HarnessEvent(kind="reasoning", text="PRIVATE REASONING")
        yield HarnessEvent(kind="tool_start", tool="shell", data={"input": "SECRET"})
        yield HarnessEvent(kind="text", text="answer")
        yield HarnessEvent(kind="done", data={"stop_reason": "end_turn"})

    monkeypatch.setattr(ac, "run_turn", turn)
    invoke = ac.build_app(config).handlers["main"]
    assert (await invoke(payload()))["status"] == "accepted"
    assert await settled(invoke, payload()) == {"status": "completed", "result": "answer"}
    fresh = ac.build_app(config).handlers["main"]
    assert await fresh({**payload(), "action": "poll"}) == {
        "status": "completed",
        "result": "answer",
    }
    # A different task cannot borrow this session's old answer.
    assert await fresh({**payload("b" * 32), "action": "poll"}) == {"status": "unknown"}


@pytest.mark.asyncio
async def test_failure_receipt_never_contains_exception(config, monkeypatch):
    async def turn(*args, **kwargs):
        raise ValueError("SECRET TOKEN")
        yield  # pragma: no cover

    monkeypatch.setattr(ac, "run_turn", turn)
    invoke = ac.build_app(config).handlers["main"]
    await invoke(payload())
    assert await settled(invoke, payload()) == {"status": "failed"}


@pytest.mark.asyncio
async def test_cancel_before_cold_start_is_durable_and_terminal_only_after_cleanup(
    config, monkeypatch
):
    seen = []

    async def turn(*args, cancel_signal, outcome, **kwargs):
        outcome["skipped"] = True
        seen.append(cancel_signal.is_set())
        yield HarnessEvent(kind="done", data={"stop_reason": "skipped"})

    monkeypatch.setattr(ac, "run_turn", turn)
    invoke = ac.build_app(config).handlers["main"]
    # Cancellation arrives before the cold-start invocation is registered.
    assert (await invoke({**payload(), "action": "cancel"}))["status"] == "cancel_requested"
    assert (await invoke({**payload(), "action": "poll"}))["status"] == "unknown"
    fresh = ac.build_app(config).handlers["main"]
    await fresh(payload())
    assert await settled(fresh, payload()) == {"status": "cancelled"}
    assert seen == [True]


@pytest.mark.asyncio
async def test_result_bound_cancel_does_not_cancel_another_run(config, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    signal = None

    async def turn(*args, cancel_signal, outcome, **kwargs):
        nonlocal signal
        signal = cancel_signal
        started.set()
        await release.wait()
        outcome["abandoned"] = cancel_signal.is_set()
        yield HarnessEvent(kind="text", text="answer")
        yield HarnessEvent(kind="done", data={"stop_reason": "end_turn"})

    monkeypatch.setattr(ac, "run_turn", turn)
    invoke = ac.build_app(config).handlers["main"]
    await invoke(payload())
    await asyncio.wait_for(started.wait(), 5)
    assert (await invoke({**payload("b" * 32), "action": "cancel"}))["status"] == "cancel_requested"
    assert not signal.is_set()
    assert (await invoke({**payload(), "action": "cancel"}))["status"] == "cancel_requested"
    assert signal.is_set()
    assert (await invoke({**payload(), "action": "poll"}))["status"] == "running"
    release.set()
    assert await settled(invoke, payload()) == {"status": "cancelled"}


@pytest.mark.parametrize(
    "change",
    [
        {"resultId": "../escape"},
        {"resultId": ""},
        {"resultId": None},
        {"sessionId": "../escape"},
        {"sessionId": ""},
        {"sessionId": None},
    ],
)
@pytest.mark.asyncio
async def test_invalid_receipt_keys_never_start(config, change):
    invoke = ac.build_app(config).handlers["main"]
    assert (await invoke({**payload(), **change}))["status"] == "error"


@pytest.mark.asyncio
async def test_receipt_start_failure_stops_agent_tools(config, monkeypatch):
    started = []
    writes = []
    real_write = run_receipts.write

    async def write(config, body, result):
        writes.append(result)
        if result["status"] == "running":
            raise OSError("storage unavailable")
        await real_write(config, body, result)

    async def turn(*args, **kwargs):
        started.append(True)
        yield HarnessEvent(kind="done")

    monkeypatch.setattr(run_receipts, "write", write)
    monkeypatch.setattr(ac, "run_turn", turn)
    invoke = ac.build_app(config).handlers["main"]
    await invoke(payload())
    assert await settled(invoke, payload()) == {"status": "failed"}
    assert started == []


@pytest.mark.asyncio
async def test_transient_terminal_write_retries_and_exhaustion_repairs_on_exact_poll(
    config, monkeypatch
):
    async def turn(*args, **kwargs):
        yield HarnessEvent(kind="text", text="durable answer")
        yield HarnessEvent(kind="done", data={"stop_reason": "end_turn"})

    real_write = run_receipts.write
    failures = 3

    async def flaky_write(config, body, result):
        nonlocal failures
        if result["status"] == "completed" and failures:
            failures -= 1
            raise OSError("temporary storage failure")
        await real_write(config, body, result)

    monkeypatch.setattr(ac, "run_turn", turn)
    monkeypatch.setattr(run_receipts, "write", flaky_write)
    invoke = ac.build_app(config).handlers["main"]
    await invoke(payload())
    # Exhaust the background retry budget with no protocol polling to repair it yet.
    await asyncio.sleep(0.5)
    assert failures == 0
    assert (await run_receipts.read(config, payload()))["status"] == "running"
    assert await settled(invoke, payload()) == {"status": "completed", "result": "durable answer"}
    fresh = ac.build_app(config).handlers["main"]
    assert await fresh({**payload(), "action": "poll"}) == {
        "status": "completed",
        "result": "durable answer",
    }
    assert await fresh({**payload("b" * 32), "action": "poll"}) == {"status": "unknown"}


@pytest.mark.asyncio
async def test_off_instance_cancel_is_observed_while_running(config, monkeypatch):
    started = asyncio.Event()

    async def turn(*args, cancel_signal, outcome, **kwargs):
        started.set()
        while not cancel_signal.is_set():
            await asyncio.sleep(0.01)
        outcome["abandoned"] = True
        yield HarnessEvent(kind="done", data={"stop_reason": "cancelled"})

    monkeypatch.setattr(ac, "run_turn", turn)
    monkeypatch.setattr(run_receipts, "CANCEL_POLL_SECONDS", 0.01)
    worker = ac.build_app(config).handlers["main"]
    other = ac.build_app(config).handlers["main"]
    await worker(payload())
    await asyncio.wait_for(started.wait(), 5)
    assert (await other({**payload(), "action": "cancel"}))["status"] == "cancel_requested"
    assert await settled(other, payload()) == {"status": "cancelled"}


@pytest.mark.asyncio
async def test_cancel_does_not_depend_on_result_read_availability(config, monkeypatch):
    started = asyncio.Event()
    signal = None

    async def turn(*args, cancel_signal, outcome, **kwargs):
        nonlocal signal
        signal = cancel_signal
        started.set()
        while not cancel_signal.is_set():
            await asyncio.sleep(0.01)
        outcome["abandoned"] = True
        yield HarnessEvent(kind="done", data={"stop_reason": "cancelled"})

    async def failed_read(*args):
        raise OSError("result temporarily unavailable")

    monkeypatch.setattr(ac, "run_turn", turn)
    invoke = ac.build_app(config).handlers["main"]
    await invoke(payload())
    await asyncio.wait_for(started.wait(), 5)
    with monkeypatch.context() as patch:
        patch.setattr(run_receipts, "read", failed_read)
        assert (await invoke({**payload(), "action": "cancel"}))["status"] == "cancel_requested"
        assert signal.is_set()
        assert await run_receipts.cancel_requested(config, payload())
    assert await settled(invoke, payload()) == {"status": "cancelled"}


async def test_receipt_relaunch_is_idempotent_and_stream_is_public(config, monkeypatch):
    calls = 0

    async def turn(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield HarnessEvent(kind="text", text="answer", data={"private": "SECRET"})
        yield HarnessEvent(kind="reasoning", text="HIDDEN")
        yield HarnessEvent(kind="done", data={"stop_reason": "end_turn"})

    monkeypatch.setattr(ac, "run_turn", turn)
    invoke = ac.build_app(config).handlers["main"]
    assert (await invoke(payload()))["status"] == "accepted"
    await invoke(payload())
    assert await settled(invoke, payload()) == {"status": "completed", "result": "answer"}
    assert await invoke(payload()) == {"status": "completed", "result": "answer"}
    fresh = ac.build_app(config).handlers["main"]
    assert await fresh(payload()) == {"status": "completed", "result": "answer"}
    assert calls == 1
    live = await invoke({**payload(), "action": "poll", "since": 0})
    assert live["events"] == [{"seq": 0, "kind": "text", "text": "answer"}]
    assert "SECRET" not in str(live) and "HIDDEN" not in str(live)


async def test_poll_cannot_publish_terminal_before_finalization(config, monkeypatch):
    finalizing = asyncio.Event()
    release = asyncio.Event()

    async def turn(*args, **kwargs):
        await asyncio.sleep(0)
        yield HarnessEvent(kind="text", text="answer")
        yield HarnessEvent(kind="done", data={"stop_reason": "end_turn"})

    async def watch(*args):
        try:
            await asyncio.Future()
        finally:
            finalizing.set()
            await release.wait()

    monkeypatch.setattr(ac, "run_turn", turn)
    monkeypatch.setattr(run_receipts, "watch_cancel", watch)
    invoke = ac.build_app(config).handlers["main"]
    await invoke(payload())
    await asyncio.wait_for(finalizing.wait(), 5)
    assert (await invoke({**payload(), "action": "poll"}))["status"] == "running"
    assert await run_receipts.read(config, payload()) == {"status": "running"}
    release.set()
    assert await settled(invoke, payload()) == {"status": "completed", "result": "answer"}


def test_receipt_floor_tracks_all_events_before_public_filter():
    store = ac._TaskStore()
    store.start("task", "receipt-key")
    for _ in range(ac._MAX_EVENTS_PER_TASK + 10):
        store.emit("task", {"kind": "reasoning", "text": "SECRET"})
    result = store.receipt("receipt-key", 0)
    assert result["firstSeq"] == 10
    assert result["nextSeq"] == ac._MAX_EVENTS_PER_TASK + 10
    assert result["events"] == []
    assert result["taskId"] == "task"
    store.emit("task", {"kind": "text", "text": "λ🙂"})
    result = store.receipt("receipt-key", result["nextSeq"])
    assert result["firstSeq"] == 11
    assert result["events"] == [{"kind": "text", "text": "λ🙂", "seq": ac._MAX_EVENTS_PER_TASK + 10}]
    assert "SECRET" not in str(result)


async def test_final_receipt_on_other_instance_has_no_live_floor(config, monkeypatch):
    async def turn(*args, **kwargs):
        yield HarnessEvent(kind="text", text="λ🙂exact")
        yield HarnessEvent(kind="done", data={"stop_reason": "end_turn"})

    monkeypatch.setattr(ac, "run_turn", turn)
    invoke = ac.build_app(config).handlers["main"]
    await invoke(payload())
    await settled(invoke, payload())
    restarted = ac.build_app(config).handlers["main"]
    result = await restarted({**payload(), "action": "poll", "since": 99})
    assert result == {"status": "completed", "result": "λ🙂exact", "events": [],
                      "nextSeq": 0, "firstSeq": None, "taskId": None}


@pytest.mark.asyncio
async def test_an_issued_only_poll_reads_only_tasks_started_on_that_session(config, monkeypatch):
    async def turn(*args, **kwargs):
        yield HarnessEvent(kind="text", text="answer")
        yield HarnessEvent(kind="done", data={"stop_reason": "end_turn"})

    monkeypatch.setattr(ac, "run_turn", turn)
    invoke = ac.build_app(config).handlers["main"]
    sid = "gh-o-r-96458419-issue-1"
    task_id = (await invoke({"sessionId": sid, "prompt": "hello"}))["taskId"]
    strict = {"action": "poll", "sessionId": sid, ac.POLL_ISSUED_ONLY_KEY: True}
    for _ in range(200):
        result = await invoke({**strict, "taskId": task_id})
        if result["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    assert result["result"] == "answer" and ac.POLL_ISSUED_KEY not in result
    for body in ({**strict, "taskId": "made-up"},
                 {**strict, "taskId": task_id, "sessionId": "gh-o-r-96458419-issue-12"}):
        assert await invoke(body) == {"taskId": body["taskId"], "status": "unknown", ac.POLL_ISSUED_KEY: False}
