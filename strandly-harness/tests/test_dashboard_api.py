"""Dashboard read-API tests — hermetic (no AWS).

The handler lives in ``dashboard/api/`` (it ships to Lambda, not in the harness package), so we add
that dir to ``sys.path`` and exercise the pure ``route()`` with a fake reader. No boto3 is touched.
"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

_API_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "api"
sys.path.insert(0, str(_API_DIR))
handler = importlib.import_module("handler")


class FakeReader:
    def __init__(self, items: list[dict[str, Any]]):
        self._items = items
        self.recent_limit: int | None = None

    def recent(self, limit: int) -> list[dict[str, Any]]:
        self.recent_limit = limit
        return self._items[:limit]

    def get(self, task_id: str) -> dict[str, Any] | None:
        return next((it for it in self._items if it["task_id"] == task_id), None)


def _evt(path: str, method: str = "GET", params: dict | None = None,
         query: dict | None = None) -> dict[str, Any]:
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": method, "path": path}},
        "pathParameters": params or {},
        "queryStringParameters": query or {},
    }


SAMPLE = [
    {"task_id": "a", "status": "completed", "started_at": "2999-01-01T10:00:00+00:00",
     "tokens_total": 100},
    {"task_id": "b", "status": "failed", "started_at": "2999-01-01T09:00:00+00:00",
     "tokens_total": 50},
    {"task_id": "c", "status": "running", "started_at": "2000-01-01T09:00:00+00:00"},
]


def test_runs_returns_recent_list():
    status, body = handler.route(_evt("/api/runs"), FakeReader(SAMPLE))
    assert status == 200
    assert [r["task_id"] for r in body["runs"]] == ["a", "b", "c"]


def test_runs_respects_and_clamps_limit():
    reader = FakeReader(SAMPLE)
    handler.route(_evt("/api/runs", query={"limit": "1"}), reader)
    assert reader.recent_limit == 1
    reader2 = FakeReader(SAMPLE)
    handler.route(_evt("/api/runs", query={"limit": "9999"}), reader2)
    assert reader2.recent_limit == handler.RUNS_MAX_LIMIT
    reader3 = FakeReader(SAMPLE)
    handler.route(_evt("/api/runs", query={"limit": "junk"}), reader3)
    assert reader3.recent_limit == handler.RUNS_DEFAULT_LIMIT


def test_run_by_id_found_and_missing():
    status, body = handler.route(_evt("/api/runs/a", params={"id": "a"}), FakeReader(SAMPLE))
    assert status == 200 and body["task_id"] == "a"
    status, body = handler.route(_evt("/api/runs/zzz", params={"id": "zzz"}), FakeReader(SAMPLE))
    assert status == 404


def test_overview_aggregates_counts_and_success_rate():
    status, body = handler.route(_evt("/api/overview"), FakeReader(SAMPLE))
    assert status == 200
    assert body["active"] == 1
    assert body["completed"] == 1 and body["failed"] == 1
    assert body["success_rate"] == 0.5  # 1 completed / (1 completed + 1 failed)
    assert body["tokens_total"] == 150


def test_config_route_is_public_and_reads_env(monkeypatch):
    monkeypatch.setenv("COGNITO_CLIENT_ID", "abc123")
    monkeypatch.setenv("COGNITO_DOMAIN", "strandly.auth.us-west-2.amazoncognito.com")
    status, body = handler.route(_evt("/api/config"), None)  # reader=None: config needs no table
    assert status == 200
    assert body["clientId"] == "abc123"
    assert body["cognitoDomain"].endswith("amazoncognito.com")


def test_non_config_route_without_table_is_500():
    status, body = handler.route(_evt("/api/runs"), None)
    assert status == 500


def test_non_get_method_rejected():
    status, _ = handler.route(_evt("/api/runs", method="POST"), FakeReader(SAMPLE))
    assert status == 405


def test_lambda_handler_serializes_decimal(monkeypatch):
    from decimal import Decimal

    monkeypatch.setattr(handler, "_build_reader",
                        lambda: FakeReader([{"task_id": "a", "status": "completed",
                                             "started_at": "2999-01-01T00:00:00+00:00",
                                             "tokens_total": Decimal("100")}]))
    resp = handler.lambda_handler(_evt("/api/runs"))
    assert resp["statusCode"] == 200
    assert '"tokens_total": 100' in resp["body"]  # Decimal rendered as int, valid JSON
    assert resp["headers"]["content-type"] == "application/json"


@pytest.mark.parametrize("path", ["/api/runs", "/api/runs/", "/api/overview", "/api/overview/"])
def test_trailing_slashes_tolerated(path):
    status, _ = handler.route(_evt(path), FakeReader(SAMPLE))
    assert status == 200


# ---- /api/runs/{id}/logs --------------------------------------------------------------

class FakeLogs:
    def __init__(self, events):
        self._events = events
        self.calls: list[dict[str, Any]] = []

    def for_run(self, session_id, *, start_ms, end_ms):
        self.calls.append({"session_id": session_id, "start_ms": start_ms, "end_ms": end_ms})
        return self._events


_LOGS_SAMPLE = [
    {"task_id": "x", "status": "completed", "session_id": "gh-repo-pr-5",
     "started_at": "2999-01-01T10:00:00+00:00", "ended_at": "2999-01-01T10:01:00+00:00"},
    {"task_id": "nosess", "status": "completed", "started_at": "2999-01-01T10:00:00+00:00"},
]


def test_logs_route_returns_events_scoped_to_session_and_window():
    logs = FakeLogs([{"timestamp": 1, "message": "hello"}])
    status, body = handler.route(
        _evt("/api/runs/x/logs", params={"id": "x"}), FakeReader(_LOGS_SAMPLE), logs=logs
    )
    assert status == 200 and body["source"] == "cloudwatch"
    assert body["events"] == [{"timestamp": 1, "message": "hello"}]
    # the reader's session id + a padded time window were passed through
    call = logs.calls[0]
    assert call["session_id"] == "gh-repo-pr-5"
    assert call["start_ms"] is not None and call["end_ms"] is not None
    assert call["start_ms"] < call["end_ms"]


def test_logs_route_unconfigured_is_empty_not_error():
    # No LogsReader wired (RUNTIME_LOG_GROUP unset) → empty events, source flag, still 200.
    status, body = handler.route(_evt("/api/runs/x/logs", params={"id": "x"}), FakeReader(_LOGS_SAMPLE))
    assert status == 200 and body["events"] == [] and body["source"] == "unconfigured"


def test_logs_route_run_without_session():
    logs = FakeLogs([{"timestamp": 1, "message": "x"}])
    status, body = handler.route(
        _evt("/api/runs/nosess/logs", params={"id": "nosess"}), FakeReader(_LOGS_SAMPLE), logs=logs
    )
    assert status == 200 and body["source"] == "no-session" and not logs.calls


def test_logs_route_unknown_run_is_404():
    status, _ = handler.route(
        _evt("/api/runs/zzz/logs", params={"id": "zzz"}), FakeReader(_LOGS_SAMPLE), logs=FakeLogs([])
    )
    assert status == 404


class _FakeLogsClient:
    """Stub CloudWatch Logs client: real streams carry a DATE prefix before [runtime-logs-<sid>]."""

    def __init__(self, stream_names, events):
        self._streams = [{"logStreamName": n} for n in stream_names]
        self._events = events
        self.filter_kwargs = None

    def describe_log_streams(self, **kw):
        return {"logStreams": self._streams}

    def filter_log_events(self, **kw):
        self.filter_kwargs = kw
        return {"events": self._events}


def test_logs_reader_matches_date_prefixed_stream():
    # Regression: AgentCore names streams "2026/06/30/[runtime-logs-<sid>]<uuid>", so a start-anchored
    # logStreamNamePrefix of "[runtime-logs-<sid>]" never matched. LogsReader must substring-match the
    # marker and pass the exact stream name(s) to filter_log_events.
    sid = "e2e-gh-123"
    real_stream = f"2026/06/30/[runtime-logs-{sid}]389076dc-uuid"
    client = _FakeLogsClient(
        [real_stream, "2026/06/30/[runtime-logs-other-session]zzz", "otel-rt-logs"],
        [{"timestamp": 1, "message": "line"}],
    )
    r = handler.LogsReader("/grp", client=client)
    out = r.for_run(sid, start_ms=10, end_ms=20)
    assert out == [{"timestamp": 1, "message": "line"}]
    # Only this session's stream was targeted, with the window.
    assert client.filter_kwargs["logStreamNames"] == [real_stream]
    assert client.filter_kwargs["startTime"] == 10 and client.filter_kwargs["endTime"] == 20


def test_logs_reader_no_matching_stream_returns_empty():
    client = _FakeLogsClient(["2026/06/30/[runtime-logs-different]uuid"], [{"timestamp": 1, "message": "x"}])
    r = handler.LogsReader("/grp", client=client)
    assert r.for_run("e2e-gh-123", start_ms=None, end_ms=None) == []


def test_iso_to_ms_parses_and_pads():
    base = handler._iso_to_ms("2999-01-01T00:00:00+00:00")
    assert isinstance(base, int)
    assert handler._iso_to_ms("2999-01-01T00:00:00+00:00", pad_ms=1000) == base + 1000
    assert handler._iso_to_ms(None) is None
    assert handler._iso_to_ms("not-a-date") is None


# ---- sessions + chat (added with the dashboard chat feature) -------------------------------

class FakeInvoker:
    """Stand-in for RuntimeInvoker: records calls, returns canned runtime responses."""

    def __init__(self, launch_resp: dict | None = None, poll_resp: dict | None = None):
        self.launch_resp = launch_resp or {"status": "accepted", "taskId": "deadbeef"}
        self.poll_resp = poll_resp or {"taskId": "deadbeef", "status": "completed", "result": "hi!"}
        self.calls: list[tuple] = []

    def launch(self, session_id: str, message: str) -> dict:
        self.calls.append(("launch", session_id, message))
        return self.launch_resp

    def poll(self, session_id: str, task_id: str) -> dict:
        self.calls.append(("poll", session_id, task_id))
        return self.poll_resp


# Newest-first window (mirrors the GSI's ScanIndexForward=False), spanning two sessions.
SESSION_ROWS = [
    {"task_id": "t3", "session_id": "s1", "status": "completed",
     "started_at": "2999-01-01T12:00:00+00:00", "prompt": "third ask", "result_summary": "r3",
     "tokens_total": 30, "github_target": "https://github.com/o/r/issues/9"},
    {"task_id": "t2", "session_id": "s2", "status": "running",
     "started_at": "2999-01-01T11:00:00+00:00", "prompt": "other session", "tokens_total": 5},
    {"task_id": "t1", "session_id": "s1", "status": "completed",
     "started_at": "2999-01-01T10:00:00+00:00", "prompt": "first ask", "result_summary": "r1",
     "tokens_total": 20},
    {"task_id": "t0", "status": "completed", "started_at": "2999-01-01T09:00:00+00:00",
     "prompt": "sessionless"},  # no session_id -> skipped
]


def test_sessions_groups_and_orders_newest_first():
    status, body = handler.route(_evt("/api/sessions"), FakeReader(SESSION_ROWS))
    assert status == 200
    sessions = body["sessions"]
    # Two sessions (the sessionless row is skipped), newest-active first (s1's latest run is t3).
    assert [s["session_id"] for s in sessions] == ["s1", "s2"]
    s1 = sessions[0]
    assert s1["runs"] == 2
    assert s1["tokens_total"] == 50  # 30 + 20
    assert s1["last_status"] == "completed"
    assert s1["last_activity"] == "2999-01-01T12:00:00+00:00"
    # description is the ORIGINATING (oldest) prompt; target carried from whichever row had one.
    assert s1["description"] == "first ask"
    assert s1["target"] == "https://github.com/o/r/issues/9"


def test_session_detail_is_ordered_transcript():
    status, body = handler.route(
        _evt("/api/sessions/s1", params={"id": "s1"}), FakeReader(SESSION_ROWS)
    )
    assert status == 200
    assert body["session_id"] == "s1" and body["runs"] == 2
    # oldest-first so the chat reads top -> bottom
    assert [t["task_id"] for t in body["turns"]] == ["t1", "t3"]
    assert body["turns"][0]["prompt"] == "first ask"
    assert body["turns"][0]["result"] == "r1"


def test_session_detail_unknown_is_404():
    status, _ = handler.route(
        _evt("/api/sessions/nope", params={"id": "nope"}), FakeReader(SESSION_ROWS)
    )
    assert status == 404


def test_chat_launch_posts_to_runtime():
    inv = FakeInvoker()
    evt = _evt("/api/chat", method="POST")
    evt["body"] = json.dumps({"session_id": "s1", "message": "hello there"})
    status, body = handler.route(evt, FakeReader(SESSION_ROWS), inv)
    assert status == 200
    assert body["taskId"] == "deadbeef"
    assert inv.calls == [("launch", "s1", "hello there")]


def test_chat_launch_decodes_base64_body():
    import base64

    inv = FakeInvoker()
    evt = _evt("/api/chat", method="POST")
    evt["body"] = base64.b64encode(json.dumps({"session_id": "s1", "message": "hi"}).encode()).decode()
    evt["isBase64Encoded"] = True
    status, _ = handler.route(evt, FakeReader(SESSION_ROWS), inv)
    assert status == 200
    assert inv.calls == [("launch", "s1", "hi")]


def test_chat_launch_requires_session_and_message():
    inv = FakeInvoker()
    evt = _evt("/api/chat", method="POST")
    evt["body"] = json.dumps({"session_id": "s1"})  # missing message
    status, body = handler.route(evt, FakeReader(SESSION_ROWS), inv)
    assert status == 400
    assert inv.calls == []  # never hit the runtime


def test_chat_poll_reads_runtime():
    inv = FakeInvoker(poll_resp={"taskId": "deadbeef", "status": "running"})
    evt = _evt("/api/chat", query={"task_id": "deadbeef", "session_id": "s1"})
    status, body = handler.route(evt, FakeReader(SESSION_ROWS), inv)
    assert status == 200
    assert body["status"] == "running"
    assert inv.calls == [("poll", "s1", "deadbeef")]


def test_chat_poll_requires_task_and_session():
    inv = FakeInvoker()
    status, _ = handler.route(_evt("/api/chat", query={"task_id": "x"}), FakeReader(SESSION_ROWS), inv)
    assert status == 400


def test_chat_disabled_without_invoker_is_503():
    evt = _evt("/api/chat", method="POST")
    evt["body"] = json.dumps({"session_id": "s1", "message": "hi"})
    status, body = handler.route(evt, FakeReader(SESSION_ROWS), None)  # invoker=None -> chat off
    assert status == 503


def test_chat_unsupported_method_is_405():
    status, _ = handler.route(_evt("/api/chat", method="DELETE"), FakeReader(SESSION_ROWS), FakeInvoker())
    assert status == 405


def test_runtime_session_id_is_padded_and_slash_free():
    sid = handler._runtime_session_id("a/b")
    assert "/" not in sid
    assert len(sid) >= handler.RUNTIME_SESSION_ID_MIN_LEN
    # deterministic: same input -> same affinity key
    assert handler._runtime_session_id("a/b") == sid
    # an already-long id is left intact
    long_id = "gh-agent-of-mkmeral-strands-issue-351-extra"
    assert handler._runtime_session_id(long_id) == long_id


def test_sessions_route_needs_table():
    status, _ = handler.route(_evt("/api/sessions"), None)
    assert status == 500


# ---- memory-backed transcripts (verbatim AgentCore Memory conversation) ---------------------

class FakeMemory:
    """Stand-in for MemoryReader: records the session id read, returns canned messages (or raises)."""

    def __init__(self, messages: list[dict] | None = None, raise_exc: bool = False):
        self._messages = messages or []
        self._raise = raise_exc
        self.calls: list[str] = []

    def transcript(self, session_id: str) -> list[dict]:
        self.calls.append(session_id)
        if self._raise:
            raise RuntimeError("memory data-plane 403")
        return self._messages


_MEM_MSGS = [
    {"role": "user", "text": "first ask", "tool_use": False},
    {"role": "assistant", "text": "let me clone the repo", "tool_use": True},
    {"role": "assistant", "text": "done — opened the PR", "tool_use": False},
]


def test_session_detail_prefers_memory_transcript():
    mem = FakeMemory(messages=_MEM_MSGS)
    status, body = handler.route(
        _evt("/api/sessions/s1", params={"id": "s1"}), FakeReader(SESSION_ROWS), None, mem
    )
    assert status == 200
    assert body["source"] == "memory"
    assert [m["text"] for m in body["messages"]] == [
        "first ask", "let me clone the repo", "done — opened the PR"
    ]
    assert mem.calls == ["s1"]  # read by the (original) session id; reader sanitizes internally


def test_session_detail_falls_back_to_ledger_when_memory_empty():
    mem = FakeMemory(messages=[])
    status, body = handler.route(
        _evt("/api/sessions/s1", params={"id": "s1"}), FakeReader(SESSION_ROWS), None, mem
    )
    assert status == 200
    assert body["source"] == "ledger"
    assert "messages" not in body
    assert [t["task_id"] for t in body["turns"]] == ["t1", "t3"]  # ledger transcript intact


def test_session_detail_falls_back_to_ledger_when_memory_errors():
    mem = FakeMemory(raise_exc=True)
    status, body = handler.route(
        _evt("/api/sessions/s1", params={"id": "s1"}), FakeReader(SESSION_ROWS), None, mem
    )
    assert status == 200  # a Memory failure must never take the chat panel down
    assert body["source"] == "ledger"
    assert [t["task_id"] for t in body["turns"]] == ["t1", "t3"]


def test_session_detail_memory_only_session_is_200():
    # A session that rolled out of the ledger window but still lives in Memory is still openable.
    mem = FakeMemory(messages=[{"role": "user", "text": "hello", "tool_use": False}])
    status, body = handler.route(
        _evt("/api/sessions/ghost", params={"id": "ghost"}), FakeReader(SESSION_ROWS), None, mem
    )
    assert status == 200
    assert body["source"] == "memory" and body["runs"] == 0
    assert body["messages"][0]["text"] == "hello"


def test_session_detail_without_memory_reader_is_ledger_only():
    # No memory reader passed -> existing behavior (no source flip, ledger turns).
    status, body = handler.route(
        _evt("/api/sessions/s1", params={"id": "s1"}), FakeReader(SESSION_ROWS)
    )
    assert status == 200
    assert body["source"] == "ledger"
    assert "messages" not in body


def _mem_ev(role: str, blocks: list, ts: int) -> dict:
    """A Memory event whose payload wraps ``blocks`` as a JSON-encoded SDK SessionMessage."""
    text = json.dumps({"message": {"role": role, "content": blocks}})
    return {"eventTimestamp": ts,
            "payload": [{"conversational": {"role": role, "content": {"text": text}}}]}


def test_memory_messages_parses_wrapped_orders_and_keeps_tool_turns():
    # Deliberately out of chronological order; _memory_messages sorts by eventTimestamp.
    events = [
        _mem_ev("assistant", [{"text": "final answer"}], 3),
        _mem_ev("user", [{"text": "hello"}], 1),
        _mem_ev("assistant", [{"text": "working"}, {"toolUse": {"name": "shell", "input": {"command": "ls"}}}], 2),
        _mem_ev("user", [{"toolResult": {"status": "success", "content": [{"text": "ok"}]}}], 4),
    ]
    msgs = handler._memory_messages(events)
    assert [(m["role"], m["text"], m["tool_use"]) for m in msgs] == [
        ("user", "hello", False),
        ("assistant", "working", True),     # carries a toolUse -> narration
        ("assistant", "final answer", False),
        ("user", "", False),                # tool-result turn is now kept (with empty text)
    ]
    # The structured tool activity is surfaced for the SPA to render inline.
    assert msgs[1]["tools"] == [{"name": "shell", "input": '{"command": "ls"}'}]
    assert msgs[3]["tool_results"] == [{"status": "success", "text": "ok"}]


def test_memory_messages_drops_messages_with_neither_text_nor_tools():
    events = [_mem_ev("assistant", [{"text": "   "}], 1)]  # whitespace-only, no tool blocks
    assert handler._memory_messages(events) == []


def test_memory_messages_tolerates_non_session_message_payloads():
    # A plain (non double-wrapped) text value is returned verbatim, not crashed on.
    events = [{"eventTimestamp": 1,
               "payload": [{"conversational": {"role": "assistant", "content": {"text": "raw"}}}]}]
    msgs = handler._memory_messages(events)
    assert msgs == [{"role": "assistant", "text": "raw", "tool_use": False,
                     "tools": [], "tool_results": []}]


def test_parse_session_message_clips_tool_payloads_and_handles_json_results():
    big = "x" * (handler._TOOL_INPUT_LIMIT + 500)
    raw = json.dumps({"message": {"role": "assistant", "content": [
        {"toolUse": {"name": "editor", "input": {"blob": big}}},
        {"toolResult": {"status": "error", "content": [{"json": {"err": 1}}, {"image": {}}]}},
    ]}})
    parsed = handler._parse_session_message(raw)
    assert parsed["text"] == ""
    assert len(parsed["tools"][0]["input"]) <= handler._TOOL_INPUT_LIMIT
    assert parsed["tools"][0]["input"].endswith("…")  # clipped with ellipsis
    result = parsed["tool_results"][0]
    assert result["status"] == "error"
    assert '{"err": 1}' in result["text"] and "[image]" in result["text"]


def test_sanitize_session_id_is_unpadded_unlike_runtime_id():
    raw = "a/b"
    assert handler._sanitize_session_id(raw) == "a-b"  # slash-free, NOT padded
    assert len(handler._sanitize_session_id(raw)) < handler.RUNTIME_SESSION_ID_MIN_LEN
    # the runtime id derives from the same sanitize but right-pads to the affinity floor
    assert len(handler._runtime_session_id(raw)) >= handler.RUNTIME_SESSION_ID_MIN_LEN
    # an already-long id is left intact by both
    long_id = "gh-agent-of-mkmeral-strands-coder-private-issue-351"
    assert handler._sanitize_session_id(long_id) == long_id
    assert handler._runtime_session_id(long_id) == long_id


class _FakeMemClient:
    """A boto3 ``bedrock-agentcore`` stand-in: two pages of events, records each list_events call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def list_events(self, **kw):
        self.calls.append(kw)
        if "nextToken" not in kw:
            return {"events": [{"eventTimestamp": 1, "payload": []}], "nextToken": "page2"}
        return {"events": [{"eventTimestamp": 2, "payload": []}]}


def test_memory_reader_paginates_and_sanitizes_session_id():
    c = _FakeMemClient()
    reader = handler.MemoryReader("mem-123", "us-west-2", None, client=c)
    events = reader.events("gh/o/r/issue-1")
    assert len(events) == 2  # both pages collected
    first = c.calls[0]
    assert first["sessionId"] == "gh-o-r-issue-1"  # sanitized (slash-free)
    assert first["memoryId"] == "mem-123"
    assert first["actorId"] == handler.DEFAULT_ACTOR_ID  # stable default actor
    assert first["maxResults"] == 100 and first["includePayloads"] is True
    assert c.calls[1]["nextToken"] == "page2"  # second page followed the token


def test_memory_reader_actor_id_override():
    c = _FakeMemClient()
    handler.MemoryReader("m", actor_id="custom-actor", client=c).events("s")
    assert c.calls[0]["actorId"] == "custom-actor"


def test_build_memory_reader_gated_on_env(monkeypatch):
    monkeypatch.delenv("AGENTCORE_MEMORY_ID", raising=False)
    assert handler._build_memory_reader() is None
    monkeypatch.setenv("AGENTCORE_MEMORY_ID", "mem-xyz")
    monkeypatch.setenv("STRANDLY_ACTOR_ID", "actor-1")
    reader = handler._build_memory_reader()
    assert isinstance(reader, handler.MemoryReader)
    assert reader._actor_id == "actor-1"


# ---------------------------------------------------------------------------
# /api/health — alarm states + mention-poller liveness
# ---------------------------------------------------------------------------


class FakeAlarms:
    """Stand-in for AlarmsReader: returns canned alarm dicts (or raises)."""

    def __init__(self, items=None, err: Exception | None = None):
        self._items = items or []
        self._err = err

    def alarms(self):
        if self._err:
            raise self._err
        return self._items


def _alarm(name: str, state: str = "OK", reason: str = "") -> dict:
    return {"name": name, "state": state, "reason": reason, "since": "2026-07-01T00:00:00+00:00"}


def test_health_reports_alarms_and_active_poller():
    alarms = FakeAlarms([
        _alarm("strandly-dev-failure-rate"),
        _alarm("strandly-dev-poll-silent", "OK"),
    ])
    status, body = handler.route(_evt("/api/health"), None, alarms=alarms)
    assert status == 200  # health never needs the ledger table
    assert body["configured"] is True
    assert [a["name"] for a in body["alarms"]] == [
        "strandly-dev-failure-rate", "strandly-dev-poll-silent",
    ]
    assert body["poller"]["status"] == "active"  # poll-silent OK -> mentions check is running


def test_health_poller_silent_when_poll_alarm_fires():
    alarms = FakeAlarms([_alarm("x-poll-silent", "ALARM", "no successful poll in 30 minutes")])
    _, body = handler.route(_evt("/api/health"), None, alarms=alarms)
    assert body["poller"]["status"] == "silent"
    assert "no successful poll" in body["poller"]["detail"]


def test_health_poller_unknown_on_insufficient_data_or_missing_alarm():
    _, body = handler.route(
        _evt("/api/health"), None, alarms=FakeAlarms([_alarm("x-poll-silent", "INSUFFICIENT_DATA")])
    )
    assert body["poller"]["status"] == "unknown"
    _, body = handler.route(
        _evt("/api/health"), None, alarms=FakeAlarms([_alarm("x-failure-rate", "OK")])
    )
    assert body["poller"]["status"] == "unknown"  # no poll-silent alarm found


def test_health_unconfigured_and_error_degrade_not_500():
    status, body = handler.route(_evt("/api/health"), None)  # no alarms reader wired
    assert status == 200 and body["configured"] is False and body["alarms"] == []
    status, body = handler.route(
        _evt("/api/health"), None, alarms=FakeAlarms(err=RuntimeError("denied"))
    )
    assert status == 200 and body["configured"] is True and body["alarms"] == []
    assert body["poller"]["status"] == "unknown"


class _FakeCwClient:
    """boto3 cloudwatch stand-in: two pages of describe_alarms, records the calls."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def describe_alarms(self, **kw):
        self.calls.append(kw)
        if "NextToken" not in kw:
            return {
                "MetricAlarms": [{
                    "AlarmName": "p-zebra", "StateValue": "OK",
                    "StateReason": "fine", "StateUpdatedTimestamp": datetime(2026, 7, 1),
                }],
                "NextToken": "page2",
            }
        return {"MetricAlarms": [{"AlarmName": "p-alpha", "StateValue": "ALARM"}]}


def test_alarms_reader_paginates_scopes_by_prefix_and_sorts():
    c = _FakeCwClient()
    out = handler.AlarmsReader("p-", client=c).alarms()
    assert c.calls[0]["AlarmNamePrefix"] == "p-"
    assert c.calls[1]["NextToken"] == "page2"  # second page followed the token
    assert [a["name"] for a in out] == ["p-alpha", "p-zebra"]  # name-sorted
    assert out[1]["since"] == "2026-07-01T00:00:00"  # datetime -> isoformat (JSON-safe)
    assert out[0]["state"] == "ALARM" and out[0]["reason"] is None


def test_build_alarms_reader_gated_on_env(monkeypatch):
    monkeypatch.delenv("ALARM_NAME_PREFIX", raising=False)
    assert handler._build_alarms_reader() is None
    monkeypatch.setenv("ALARM_NAME_PREFIX", "strandly-dev-")
    reader = handler._build_alarms_reader()
    assert isinstance(reader, handler.AlarmsReader)
    assert reader._prefix == "strandly-dev-"


# ---- mentions (poller-written mention log) ----------------------------------------------


class FakeMentions:
    def __init__(self, items):
        self._items = items
        self.recent_limit = None

    def recent(self, limit):
        self.recent_limit = limit
        return self._items[:limit]


_MENTION_ROWS = [
    {"mention_id": "t1#2999-01-01T10:00:00Z#dispatched", "author": "mkmeral", "authorized": True,
     "outcome": "dispatched", "repo": "ext/repo", "number": 42, "is_pull_request": True,
     "seen_at": "2999-01-01T10:00:05+00:00"},
    {"mention_id": "t2#2999-01-01T09:00:00Z#unauthorized", "author": "eve", "authorized": False,
     "outcome": "unauthorized", "repo": "ext/repo", "number": 7, "is_pull_request": False,
     "seen_at": "2999-01-01T09:00:05+00:00"},
]


def test_mentions_route_returns_rows_and_enabled():
    m = FakeMentions(_MENTION_ROWS)
    status, body = handler.route(_evt("/api/mentions"), FakeReader(SAMPLE), mentions=m)
    assert status == 200 and body["enabled"] is True
    assert [r["author"] for r in body["mentions"]] == ["mkmeral", "eve"]
    assert body["mentions"][1]["authorized"] is False


def test_mentions_route_clamps_limit():
    m = FakeMentions(_MENTION_ROWS)
    handler.route(_evt("/api/mentions", query={"limit": "9999"}), FakeReader(SAMPLE), mentions=m)
    assert m.recent_limit == handler.RUNS_MAX_LIMIT


def test_mentions_route_unconfigured_is_disabled_not_error():
    # No mention-log table (older deploy) → the tab gets an explicit off signal, not a 500.
    status, body = handler.route(_evt("/api/mentions"), FakeReader(SAMPLE))
    assert status == 200
    assert body == {"mentions": [], "enabled": False}


def test_mentions_route_works_without_ledger_reader():
    # Mentions read their own table — a missing run-ledger must not block them.
    m = FakeMentions(_MENTION_ROWS)
    status, body = handler.route(_evt("/api/mentions"), None, mentions=m)
    assert status == 200 and body["enabled"] is True
