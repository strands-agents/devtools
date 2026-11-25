"""The SigV4 transport — AWS-free via monkeypatched seams (no boto3 needed to run these)."""

from __future__ import annotations

import builtins
import json

import pytest
from _home_isolation import _isolated_home  # noqa: F401  (autouse: tmp HOME)

from strandly_mcp import agentcore


def test_runtime_session_id_pads_short_ids_deterministically():
    padded = agentcore.runtime_session_id("mcp-abc")
    assert len(padded) == agentcore.RUNTIME_SESSION_ID_MIN_LEN
    assert padded.startswith("mcp-abc-0") and padded == agentcore.runtime_session_id("mcp-abc")
    long_id = "x" * 40
    assert agentcore.runtime_session_id(long_id) == long_id


def test_sanitize_strips_unsafe_characters():
    assert agentcore.sanitize_session_id("a/b c") == "a-b-c"
    assert agentcore.sanitize_session_id("///") == "session"


def test_parse_sse_yields_json_events_and_falls_back_to_text():
    lines = [b"data: {\"kind\": \"text\", \"text\": \"hi\"}", b"", b": comment", b"data: not json", "data:"]
    out = list(agentcore._parse_sse(iter(lines)))
    assert out == [{"kind": "text", "text": "hi"}, {"kind": "text", "text": "not json"}]


class _FakeBoto:
    def __init__(self, response):
        self.calls = []
        self._response = response

    def invoke_agent_runtime(self, **kw):
        self.calls.append(kw)
        return {"response": self._response}


def test_launch_and_poll_send_the_padded_runtime_id_and_the_raw_memory_id(monkeypatch):
    fake = _FakeBoto(json.dumps({"status": "accepted", "taskId": "t-1"}).encode())
    monkeypatch.setattr(agentcore, "_client", lambda region, **kw: fake)
    out = agentcore.launch_run("arn:rt", "us-west-2", "s-1", "hi", trigger_source="mcp:ask")
    assert out == {"status": "accepted", "taskId": "t-1"}
    call = fake.calls[0]
    assert call["runtimeSessionId"] == agentcore.runtime_session_id("s-1")
    payload = json.loads(call["payload"])
    assert payload == {"prompt": "hi", "sessionId": "s-1", "triggerSource": "mcp:ask"}

    agentcore.poll_run("arn:rt", "us-west-2", "s-1", "t-1")
    assert json.loads(fake.calls[1]["payload"]) == {"action": "poll", "taskId": "t-1", "sessionId": "s-1"}


def test_invoke_wraps_a_non_json_body(monkeypatch):
    monkeypatch.setattr(agentcore, "_client", lambda region, **kw: _FakeBoto(b"<html>oops"))
    assert agentcore.poll_run("arn", "r", "s", "t") == {"raw": "<html>oops"}


def test_missing_boto3_is_a_clear_extra_error(monkeypatch):
    real_import = builtins.__import__

    def no_boto(name, *a, **k):
        if name == "boto3":
            raise ImportError("No module named 'boto3'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_boto)
    with pytest.raises(agentcore.AwsExtraMissingError, match=r"strandly-mcp\[aws\]"):
        agentcore._client("us-west-2")


@pytest.mark.asyncio
async def test_collect_concatenates_text_and_reports_tool_activity(monkeypatch):
    events = [
        {"kind": "turn_start", "data": {"reason": "start"}},
        {"kind": "tool_start", "tool": "bash", "data": {}},
        {"kind": "text", "text": "hel"},
        {"kind": "text", "text": "lo"},
        {"kind": "done", "data": {"stop_reason": "end_turn"}},
    ]
    captured = {}
    monkeypatch.setattr(
        agentcore,
        "stream_run",
        lambda arn, region, sid, prompt: captured.update(arn=arn, sid=sid, prompt=prompt) or iter(events),
    )
    seen: list[str] = []

    async def on_activity(msg: str) -> None:
        seen.append(msg)

    out = await agentcore.run_deployed_collect("arn:rt", "us-west-2", "s-1", "hi", on_activity=on_activity)
    assert out == "hello"
    assert seen == ["→ bash"]
    assert captured == {"arn": "arn:rt", "sid": "s-1", "prompt": "hi"}


@pytest.mark.asyncio
async def test_collect_resumed_turn_supersedes_the_draft(monkeypatch):
    events = [
        {"kind": "turn_start", "data": {"reason": "start"}},
        {"kind": "text", "text": "draft answer"},
        {"kind": "turn_start", "data": {"reason": "resume"}},
        {"kind": "text", "text": "final answer"},
        {"kind": "done", "data": {}},
    ]
    monkeypatch.setattr(agentcore, "stream_run", lambda *a, **k: iter(events))
    assert await agentcore.run_deployed_collect("arn:rt", "us-west-2", "s", "hi") == "final answer"


@pytest.mark.asyncio
async def test_collect_surfaces_error_events(monkeypatch):
    events = [{"kind": "turn_start"}, {"kind": "text", "text": "partial"}, {"kind": "error", "text": "boom"}]
    monkeypatch.setattr(agentcore, "stream_run", lambda *a, **k: iter(events))
    assert await agentcore.run_deployed_collect("arn:rt", "us-west-2", "s", "hi") == "[error] boom"


def test_launch_runs_on_the_given_session_as_mcp_ask(monkeypatch):
    captured = {}

    def fake_launch(arn, region, sid, prompt, *, trigger_source=None):
        captured.update(sid=sid, prompt=prompt, trigger_source=trigger_source)
        return {"status": "accepted", "taskId": "t-1"}

    monkeypatch.setattr(agentcore, "launch_run", fake_launch)
    assert agentcore.launch_deployed_task("arn:rt", "us-west-2", "gh-o-r-pr-1", "do it") == {
        "status": "accepted", "taskId": "t-1"
    }
    assert captured == {"sid": "gh-o-r-pr-1", "prompt": "do it", "trigger_source": "mcp:ask"}


def test_check_polls_with_the_launch_session(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        agentcore,
        "poll_run",
        lambda arn, region, sid, tid: captured.update(sid=sid, tid=tid) or {"status": "completed"},
    )
    assert agentcore.check_deployed_task("arn:rt", "us-west-2", "s-1", "t-1") == {"status": "completed"}
    assert captured == {"sid": "s-1", "tid": "t-1"}


def test_runtime_resolution_order(monkeypatch, tmp_path):
    record = tmp_path / "runtime.json"
    record.write_text(json.dumps({"runtime_arn": "arn:recorded", "region": "eu-west-1"}))
    monkeypatch.setattr(agentcore, "_GLOBAL_FILE", record)
    monkeypatch.delenv("STRANDLY_RUNTIME_ARN", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    assert agentcore.resolve_runtime_arn() == "arn:recorded"
    assert agentcore.resolve_region() == "eu-west-1"
    monkeypatch.setenv("STRANDLY_RUNTIME_ARN", "arn:env")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    assert agentcore.resolve_runtime_arn() == "arn:env"
    assert agentcore.resolve_region() == "us-east-1"
    assert agentcore.resolve_runtime_arn("arn:flag") == "arn:flag"
    record.write_text("garbage")
    monkeypatch.delenv("STRANDLY_RUNTIME_ARN")
    assert agentcore.resolve_runtime_arn() is None


@pytest.mark.asyncio
async def test_collect_reports_a_cancelled_run_instead_of_its_partial_text(monkeypatch):
    events = [
        {"kind": "turn_start"},
        {"kind": "text", "text": "partial"},
        {"kind": "done", "data": {"stop_reason": "cancelled"}},
        {"kind": "cancelled", "data": {}},
    ]
    monkeypatch.setattr(agentcore, "stream_run", lambda *a, **k: iter(events))
    out = await agentcore.run_deployed_collect("arn:rt", "us-west-2", "s", "hi")
    assert out == agentcore.CANCELLED_ANSWER


class _IncrementalBody:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.reads = 0
        self.closed = False
        self._raw_stream = self

    def read1(self, size):
        self.reads += 1
        return next(self.chunks, b"")

    def iter_lines(self):
        raise AssertionError("fixed-size buffered reader must not be used")

    def close(self):
        self.closed = True


def test_stream_first_event_arrives_without_reading_later_chunks(monkeypatch):
    body = _IncrementalBody([b'data: {"kind":"text","text":"one"}\n\n',
                             b'data: {"kind":"cancelled"}\n\n'])
    monkeypatch.setattr(agentcore, "_client", lambda *a, **k: _FakeBoto(body))
    events = agentcore.stream_run("arn:rt", "us-west-2", "s", "hi")
    assert next(events) == {"kind": "text", "text": "one"}
    assert body.reads == 1
    assert list(events) == [{"kind": "cancelled"}]
    assert body.closed


def test_stream_handles_split_utf8_crlf_and_unterminated_last_line(monkeypatch):
    raw = 'data: {"kind":"text","text":"café"}\r\n\r\ndata: {"kind":"done"}'.encode()
    split = raw.index(b'\xc3') + 1
    body = _IncrementalBody([raw[:split], raw[split:split + 2], raw[split + 2:]])
    monkeypatch.setattr(agentcore, "_client", lambda *a, **k: _FakeBoto(body))
    assert list(agentcore.stream_run("arn:rt", "us-west-2", "s", "hi")) == [
        {"kind": "text", "text": "café"}, {"kind": "done"}]
    assert body.closed


def test_stream_closes_body_when_consumer_stops(monkeypatch):
    body = _IncrementalBody([b'data: {"kind":"text","text":"one"}\n'])
    monkeypatch.setattr(agentcore, "_client", lambda *a, **k: _FakeBoto(body))
    events = agentcore.stream_run("arn:rt", "us-west-2", "s", "hi")
    next(events)
    events.close()
    assert body.closed and body.reads == 1
