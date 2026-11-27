from __future__ import annotations

from typing import Any

from strandly.ops import runtime_client
from strandly.ops.runtime_client import _parse_sse, launch_run


def test_resolve_runtime_arn_order(monkeypatch):
    """Flag, then $STRANDLY_RUNTIME_ARN, then the record ``strandly deploy`` writes — nothing else."""
    monkeypatch.setattr(runtime_client, "_recorded", lambda: {"runtime_arn": "arn:recorded"})
    monkeypatch.setenv("STRANDLY_RUNTIME_ARN", "arn:env")
    assert runtime_client.resolve_runtime_arn("arn:flag") == "arn:flag"
    assert runtime_client.resolve_runtime_arn() == "arn:env"
    monkeypatch.delenv("STRANDLY_RUNTIME_ARN")
    assert runtime_client.resolve_runtime_arn() == "arn:recorded"
    monkeypatch.setattr(runtime_client, "_recorded", lambda: {})
    assert runtime_client.resolve_runtime_arn() is None


def test_launch_run_stamps_mode_and_trigger_source(monkeypatch):
    """--mode / --trigger-source become the payload's mode / triggerSource."""
    captured: dict[str, Any] = {}

    def _fake_invoke(runtime_arn, region, session_id, payload):
        captured.update(payload)
        return {"status": "accepted", "taskId": "x"}

    monkeypatch.setattr(runtime_client, "_invoke", _fake_invoke)
    launch_run("arn", "us-west-2", "gh-o-r-pr-1", "hi",
               mode="draft", trigger_source="event:pull_request.opened")
    assert captured["mode"] == "draft"
    assert captured["triggerSource"] == "event:pull_request.opened"
    assert captured["prompt"] == "hi" and captured["sessionId"] == "gh-o-r-pr-1"


def test_launch_run_omits_mode_and_trigger_when_unset(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(runtime_client, "_invoke",
                        lambda *a: captured.update(a[3]) or {"status": "accepted"})
    launch_run("arn", "us-west-2", "s", "hi")
    assert "mode" not in captured and "triggerSource" not in captured


def test_parse_sse_filters_and_decodes_json():
    lines = [
        b"data: {\"kind\": \"text\", \"text\": \"hi\"}",
        b"",  # blank line between SSE frames
        b": comment line ignored",
        b"data: {\"kind\": \"done\"}",
    ]
    events = list(_parse_sse(iter(lines)))
    assert events == [{"kind": "text", "text": "hi"}, {"kind": "done"}]


def test_parse_sse_non_json_data_becomes_text():
    events = list(_parse_sse(iter(["data: plain words"])))
    assert events == [{"kind": "text", "text": "plain words"}]


class _TrickleStream:
    """urllib3-like raw stream: ``read1`` hands back one small piece per call."""

    def __init__(self, pieces: list[bytes]) -> None:
        self.pieces = list(pieces)
        self.reads = 0

    def read1(self, amt: int = -1) -> bytes:
        self.reads += 1
        return self.pieces.pop(0) if self.pieces else b""


class _TrickleBody:
    """A ``StreamingBody`` stand-in whose fixed-chunk ``iter_lines`` must not be used."""

    def __init__(self, pieces: list[bytes]) -> None:
        self._raw_stream = _TrickleStream(pieces)

    def iter_lines(self, chunk_size: int = 1024):
        raise AssertionError("iter_lines waits for 1024-byte chunks")


def test_stream_run_yields_each_small_event_as_soon_as_it_arrives(monkeypatch):
    body = _TrickleBody(
        [
            b'data: {"kind": "te',  # one event split across two reads
            b'xt", "text": "a"}\n\n',
            b'data: {"kind": "text", "text": "b"}\n\ndata: {"kind": "done"}',  # no final newline
        ]
    )

    class _Client:
        def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
            return {"response": body}

    monkeypatch.setattr(runtime_client, "_client", lambda region, launch: _Client())
    events = runtime_client.stream_run("arn", "us-west-2", "sess-1", "hi")
    assert next(events) == {"kind": "text", "text": "a"}
    assert body._raw_stream.reads == 2  # the first event did not wait for the later ones
    assert list(events) == [{"kind": "text", "text": "b"}, {"kind": "done"}]
