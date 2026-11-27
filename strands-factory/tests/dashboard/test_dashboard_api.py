"""Dashboard read-API tests — hermetic (no AWS).

The handler lives in ``dashboard/api/`` (it ships to Lambda, not in the harness package), so we add
that dir to ``sys.path`` and exercise the pure ``route()`` with a fake reader. No boto3 is touched.
"""

from __future__ import annotations

import base64
import copy
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_API_DIR = Path(__file__).resolve().parents[2] / "dashboard" / "api"
sys.path.insert(0, str(_API_DIR))
handler = importlib.import_module("handler")


class FakeReader:
    def __init__(self, items: list[dict[str, Any]]):
        self._items = items
        self.recent_limit: int | None = None

    def recent(self, limit: int) -> list[dict[str, Any]]:
        self.recent_limit = limit
        return self._items[:limit]


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


def test_non_config_route_without_table_is_500():
    status, body = handler.route(_evt("/api/sessions"), None)
    assert status == 500


def test_non_get_method_rejected():
    status, _ = handler.route(_evt("/api/sessions", method="POST"), FakeReader(SAMPLE))
    assert status == 405


def test_lambda_handler_serializes_decimal(monkeypatch):
    from decimal import Decimal

    rows = [{"work_product_id": "wp1", "session_id": "s1", "score": Decimal("100")}]
    monkeypatch.setattr(handler, "_build_reader", lambda: None)
    monkeypatch.setattr(handler, "_build_work_product_reader", lambda: FakeWorkProducts(rows))
    resp = handler.lambda_handler(_evt("/api/workproducts", query={"session_id": "s1"}))
    assert resp["statusCode"] == 200
    assert '"score": 100' in resp["body"]  # Decimal rendered as int, valid JSON
    assert resp["headers"]["content-type"] == "application/json"


@pytest.mark.parametrize("path", ["/api/sessions", "/api/sessions/"])
def test_trailing_slashes_tolerated(path):
    status, _ = handler.route(_evt(path), FakeReader(SAMPLE))
    assert status == 200


@pytest.mark.parametrize("path", [
    "/api/overview", "/api/health", "/api/runs", "/api/runs/a", "/api/runs/a/logs",
    "/api/metrics", "/api/mentions", "/api/today", "/api/artifacts", "/api/workproducts/wp1",
    "/api/workproducts/wp1/curate", "/", "/api", "/api/nope", "/chat", "/x/api/sessions",
])
def test_removed_and_unknown_paths_are_404(path):
    for method in ("GET", "POST"):
        status, body = handler.route(
            _evt(path, method=method, params={"id": "a"}), FakeReader(SAMPLE),
            work_products=FakeWorkProducts(), invoker=FakeInvoker(),
        )
        assert status == 404, (method, path)
        assert body["error"] == "not found"


# ---- sessions + chat (added with the dashboard chat feature) -------------------------------

class FakeInvoker:
    """Stand-in for RuntimeInvoker: records calls, returns canned runtime responses."""

    def __init__(self, launch_resp: dict | None = None, poll_resp: dict | None = None):
        self.launch_resp = launch_resp or {"status": "accepted", "taskId": "deadbeef"}
        self.poll_resp = poll_resp or {"taskId": "deadbeef", "status": "completed", "result": "hi!"}
        self.calls: list[tuple] = []
        self.portal_users: list[str | None] = []

    def launch(
        self, session_id: str, message: str, *, mode=None, trigger_source=None,
        invocation_context=None, portal_user=None, portal_issuer=None,
    ) -> dict:
        self.calls.append(("launch", session_id, message, mode, trigger_source, invocation_context))
        self.portal_users.append(portal_user)
        self.portal_issuer = portal_issuer
        return self.launch_resp

    def poll(self, session_id: str, task_id: str, since: int | None = None) -> dict:
        self.calls.append(("poll", session_id, task_id, since))
        return self.poll_resp

    def cancel(self, session_id: str) -> dict:
        self.calls.append(("cancel", session_id))
        return {"status": "cancelled", "sessionId": session_id, "taskId": "t1"}


# Newest-first window (mirrors the GSI's ScanIndexForward=False), spanning two sessions.
SESSION_ROWS = [
    {"task_id": "t3", "session_id": "s1", "status": "completed",
     "started_at": "2999-01-01T12:00:00+00:00", "ended_at": "2999-01-01T12:01:00+00:00",
     "prompt": "third ask", "result_summary": "r3",
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
    # ended_at is surfaced (when recorded) so the SPA can time the reply at run-finish, not run-start.
    assert body["turns"][1]["ended_at"] == "2999-01-01T12:01:00+00:00"
    assert body["turns"][0]["ended_at"] is None  # t1 has no ended_at in the fixture


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
    assert inv.calls == [("launch", "s1", "hello there", None, None, None)]


def _chat_post(body: dict, claims: dict | None = None) -> dict:
    evt = _evt("/api/chat", method="POST")
    evt["body"] = json.dumps(body)
    if claims is not None:
        evt["requestContext"]["authorizer"] = {"jwt": {"claims": claims}}
    return evt


@pytest.mark.parametrize(
    ("claims", "user"),
    [
        ({"username": "alice", "sub": "1"}, None),
        ({"cognito:username": "bob@example.com"}, None),
        ({"username": "not a user!"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_chat_launch_forwards_the_signed_in_user(claims, user):
    """The runtime's schedule tool records the verified portal user as the requester."""
    inv = FakeInvoker()
    status, _ = handler.route(
        _chat_post({"session_id": "s1", "message": "hi", "trigger_source": "portal:chat"}, claims),
        FakeReader(SESSION_ROWS),
        inv,
    )
    assert status == 200 and inv.portal_users == [user]


@pytest.mark.parametrize(
    ("sent", "forwarded"),
    [("portal:curate", "portal:curate"), ("slack:U0BOB", "portal:chat"),
     ("github:@octocat", "portal:chat"), ("scheduled:x", "portal:chat"), (None, None)],
)
def test_chat_trigger_source_stays_in_the_portal_namespace(sent, forwarded):
    inv = FakeInvoker()
    body = {"session_id": "s1", "message": "hi", **({"trigger_source": sent} if sent else {})}
    handler.route(_chat_post(body, {"username": "alice"}), FakeReader(SESSION_ROWS), inv)
    assert inv.calls[0][4] == forwarded


def test_chat_launch_forwards_post_mode_and_trigger_source():
    """The publish handshake: a post-mode turn stamps mode + provenance."""
    inv = FakeInvoker()
    evt = _evt("/api/chat", method="POST")
    evt["body"] = json.dumps(
        {"session_id": "s1", "message": "publish wp1", "mode": "post",
         "trigger_source": "portal:curate"}
    )
    status, _ = handler.route(evt, FakeReader(SESSION_ROWS), inv)
    assert status == 200
    assert inv.calls == [("launch", "s1", "publish wp1", "post", "portal:curate", None)]


def test_chat_launch_forwards_invocation_context():
    """Ambient UI state (what the user is viewing) rides the launch into the invoke payload."""
    context = {
        "surface": "maintainer-portal",
        "view": "triage",
        "viewing": {"type": "issue", "repo": "o/r", "number": 123},
    }
    inv = FakeInvoker()
    evt = _evt("/api/chat", method="POST")
    evt["body"] = json.dumps(
        {"session_id": "s1", "message": "what about this one?", "invocation_context": context}
    )
    status, _ = handler.route(evt, FakeReader(SESSION_ROWS), inv)
    assert status == 200
    assert inv.calls == [("launch", "s1", "what about this one?", None, None, context)]


def test_chat_launch_accepts_camel_case_invocation_context():
    inv = FakeInvoker()
    evt = _evt("/api/chat", method="POST")
    evt["body"] = json.dumps(
        {"session_id": "s1", "message": "hi", "invocationContext": {"surface": "portal"}}
    )
    handler.route(evt, FakeReader(SESSION_ROWS), inv)
    assert inv.calls == [("launch", "s1", "hi", None, None, {"surface": "portal"})]


@pytest.mark.parametrize("bad", ["a string", 42, ["list"], {}, None])
def test_chat_launch_drops_invalid_invocation_context(bad):
    """Non-object/empty contexts are dropped, never an error — the context is purely additive."""
    inv = FakeInvoker()
    evt = _evt("/api/chat", method="POST")
    evt["body"] = json.dumps({"session_id": "s1", "message": "hi", "invocation_context": bad})
    status, _ = handler.route(evt, FakeReader(SESSION_ROWS), inv)
    assert status == 200
    assert inv.calls == [("launch", "s1", "hi", None, None, None)]


def test_chat_launch_drops_oversized_invocation_context():
    """An absurdly large context is dropped whole (truncated JSON would be junk), chat unaffected."""
    inv = FakeInvoker()
    evt = _evt("/api/chat", method="POST")
    evt["body"] = json.dumps(
        {"session_id": "s1", "message": "hi", "invocation_context": {"blob": "x" * 20000}}
    )
    status, _ = handler.route(evt, FakeReader(SESSION_ROWS), inv)
    assert status == 200
    assert inv.calls == [("launch", "s1", "hi", None, None, None)]


def test_chat_launch_ignores_stream_as_a_portal_mode():
    """`stream` is the transport selector, never a portal mode — it must not reach the ledger."""
    inv = FakeInvoker()
    evt = _evt("/api/chat", method="POST")
    evt["body"] = json.dumps({"session_id": "s1", "message": "hi", "mode": "stream"})
    status, _ = handler.route(evt, FakeReader(SESSION_ROWS), inv)
    assert status == 200
    assert inv.calls == [("launch", "s1", "hi", None, None, None)]


def test_chat_cancel_action_stops_the_running_turn():
    """The Stop button: a control action — no ``message`` is required."""
    inv = FakeInvoker()
    evt = _evt("/api/chat", method="POST")
    evt["body"] = json.dumps({"session_id": "s1", "action": "cancel"})
    status, body = handler.route(evt, FakeReader(SESSION_ROWS), inv)
    assert status == 200
    assert body["status"] == "cancelled"
    assert inv.calls == [("cancel", "s1")]


def test_chat_cancel_requires_a_session():
    inv = FakeInvoker()
    evt = _evt("/api/chat", method="POST")
    evt["body"] = json.dumps({"action": "cancel"})
    status, _ = handler.route(evt, FakeReader(SESSION_ROWS), inv)
    assert status == 400
    assert inv.calls == []


def test_chat_launch_decodes_base64_body():
    import base64

    inv = FakeInvoker()
    evt = _evt("/api/chat", method="POST")
    evt["body"] = base64.b64encode(json.dumps({"session_id": "s1", "message": "hi"}).encode()).decode()
    evt["isBase64Encoded"] = True
    status, _ = handler.route(evt, FakeReader(SESSION_ROWS), inv)
    assert status == 200
    assert inv.calls == [("launch", "s1", "hi", None, None, None)]


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
    assert inv.calls == [("poll", "s1", "deadbeef", None)]  # no cursor -> classic status poll


def test_chat_poll_forwards_since_cursor():
    # `since` rides through to the invoker so the runtime can return the live-event window.
    inv = FakeInvoker(poll_resp={"taskId": "deadbeef", "status": "running",
                                 "events": [{"type": "text", "text": "hel"}], "nextSeq": 7})
    evt = _evt("/api/chat", query={"task_id": "deadbeef", "session_id": "s1", "since": "3"})
    status, body = handler.route(evt, FakeReader(SESSION_ROWS), inv)
    assert status == 200
    assert body["nextSeq"] == 7 and body["events"] == [{"type": "text", "text": "hel"}]
    assert inv.calls == [("poll", "s1", "deadbeef", 3)]


def test_chat_poll_since_garbage_and_negative_are_safe():
    """Garbage cursor -> ignored (None); a negative one -> the runtime's TAIL sentinel. Never a 500.

    ``-1`` means "attach at the live head, no backlog" (``agentcore_app.TAIL_CURSOR``), which the SPA
    sends when it joins a run that was already in flight. This hop used to clamp it to 0 — replaying
    the run's whole event buffer into the panel, which is the duplicated-answer bug. Anything more
    negative is the same request, not a deeper one.
    """
    inv = FakeInvoker(poll_resp={"taskId": "t", "status": "running"})
    for raw, expected in [("abc", None), ("-1", -1), ("-5", -1), ("0", 0), ("", None)]:
        inv.calls.clear()
        evt = _evt("/api/chat", query={"task_id": "t", "session_id": "s1", "since": raw})
        status, _ = handler.route(evt, FakeReader(SESSION_ROWS), inv)
        assert status == 200
        assert inv.calls == [("poll", "s1", "t", expected)], f"since={raw!r}"


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
    long_id = "gh-example-user-strands-issue-351-extra"
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


def test_parse_content_clips_tool_payloads_and_handles_json_results():
    big = "x" * (handler._TOOL_INPUT_LIMIT + 500)
    parsed = handler._parse_content([
        {"toolUse": {"name": "editor", "input": {"blob": big}}},
        {"toolResult": {"status": "error", "content": [{"json": {"err": 1}}, {"image": {}}]}},
    ])
    assert parsed["text"] == ""
    assert len(parsed["tools"][0]["input"]) <= handler._TOOL_INPUT_LIMIT
    assert parsed["tools"][0]["input"].endswith("…")  # clipped with ellipsis
    result = parsed["tool_results"][0]
    assert result["status"] == "error"
    assert '{"err": 1}' in result["text"] and "[image]" in result["text"]


def test_parse_content_blocks_keep_the_content_order():
    """A chat UI must render a message's content in the order the model produced it — text after a
    tool call stays after it. The flat ``text``/``tools``/``tool_results`` split can't say where the
    text sat relative to the calls, so ``blocks`` carries the whole message in its original order."""
    parsed = handler._parse_content([
        {"text": "Let me look."},
        {"toolUse": {"name": "shell", "input": {"command": "ls"}}},
        {"toolResult": {"status": "success", "content": [{"text": "a b"}]}},
        {"text": "Two files."},
        {"toolUse": {"name": "editor", "input": {"path": "a"}}},
    ])
    assert [b["kind"] for b in parsed["blocks"]] == ["text", "tool", "result", "text", "tool"]
    assert parsed["blocks"][0] == {"kind": "text", "text": "Let me look."}
    assert parsed["blocks"][1] == {"kind": "tool", "name": "shell", "input": '{"command": "ls"}'}
    assert parsed["blocks"][2] == {"kind": "result", "status": "success", "text": "a b"}
    assert parsed["blocks"][3] == {"kind": "text", "text": "Two files."}
    # The flat split is the same content, still there for the reference SPA.
    assert parsed["text"] == "Let me look.\nTwo files."
    assert [t["name"] for t in parsed["tools"]] == ["shell", "editor"]
    assert parsed["tool_results"] == [{"status": "success", "text": "a b"}]


def test_parse_content_blocks_keep_the_tool_use_id():
    """Results are paired with their call by ``toolUseId`` (results of a multi-call turn can come back
    out of order); the id rides along on both block kinds and is omitted when the block lacks it."""
    blocks = handler._parse_content([
        {"toolUse": {"toolUseId": "read-1", "name": "read", "input": {"path": "missing.txt"}}},
        {"toolUse": {"toolUseId": "shell-2", "name": "shell", "input": {"command": "pwd"}}},
        {"toolResult": {"toolUseId": "shell-2", "status": "success", "content": [{"text": "/workspace"}]}},
        {"toolResult": {"toolUseId": "read-1", "status": "error", "content": [{"text": "not found"}]}},
        {"toolUse": {"name": "no-id", "input": {}}},
    ])["blocks"]
    assert [b.get("id") for b in blocks] == ["read-1", "shell-2", "shell-2", "read-1", None]
    assert "id" not in blocks[4]


def test_injected_and_summary_blocks_are_context_blocks_kept_out_of_the_text():
    """Text the agent added to a turn renders as a ``context`` block in place, with its source,
    never joined into the user's words; a blank one is dropped like blank text."""
    [message] = handler._store_messages([("snapshot:0", {"role": "user", "content": [
        {"text": "what about this?"},
        {"injected": {"source": "caller_context:injector", "text": "# Caller context\n> {}"}},
        {"injected": {"source": "strands:environment:injector", "text": "  "}},
        {"summary": {"text": "earlier: the user asked for X", "provider": None}},
    ]}, None)])
    assert message["text"] == "what about this?"
    assert message["blocks"] == [
        {"kind": "text", "text": "what about this?"},
        {"kind": "context", "source": "caller_context:injector", "text": "# Caller context\n> {}"},
        {"kind": "context", "source": "summary", "text": "earlier: the user asked for X"},
    ]


def test_injected_text_is_clipped_like_a_tool_result():
    long = "x" * (handler._TOOL_RESULT_LIMIT + 50)
    [block] = handler._parse_content([{"injected": {"source": "s", "text": long}}])["blocks"]
    assert len(block["text"]) < len(long)


def test_store_messages_drop_messages_with_neither_text_nor_tools():
    items = [("0", {"role": "assistant", "content": [{"text": "   "}]}, None)]  # whitespace-only
    assert handler._store_messages(items) == []


def test_store_messages_carry_ordered_blocks_and_drop_blank_text_blocks():
    items = [("0", {"role": "assistant", "content": [
        {"toolUse": {"name": "shell", "input": {"command": "ls"}}},
        {"text": "   "},                # whitespace-only: blanked from `text`, so not a block either
        {"text": "after the call"},
    ]}, None)]
    (msg,) = handler._store_messages(items)
    assert [b["kind"] for b in msg["blocks"]] == ["tool", "text"]  # tool FIRST — as the model did it
    assert msg["blocks"][1]["text"] == "after the call"
    assert msg["text"] == "   \nafter the call"  # the flat join is unchanged for the SPA
    json.dumps(msg, cls=handler._DecimalEncoder)  # serializable


def test_sanitize_session_id_is_unpadded_unlike_runtime_id():
    raw = "a/b"
    assert handler._sanitize_session_id(raw) == "a-b"  # slash-free, NOT padded
    assert len(handler._sanitize_session_id(raw)) < handler.RUNTIME_SESSION_ID_MIN_LEN
    # the runtime id derives from the same sanitize but right-pads to the affinity floor
    assert len(handler._runtime_session_id(raw)) >= handler.RUNTIME_SESSION_ID_MIN_LEN
    # an already-long id is left intact by both
    long_id = "gh-example-user-example-operator-repo-issue-351"
    assert handler._sanitize_session_id(long_id) == long_id
    assert handler._runtime_session_id(long_id) == long_id


class _FakeMemClient:
    """A boto3 ``bedrock-agentcore`` stand-in: two pages of events, records each list_events call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def list_events(self, **kw):
        self.calls.append(kw)
        meta = {"kind": {"stringValue": "text"}}
        if "nextToken" not in kw:
            return {"events": [{"eventTimestamp": 1, "metadata": meta, "payload": []}], "nextToken": "page2"}
        return {"events": [{"eventTimestamp": 2, "metadata": meta, "payload": []},
                           {"eventTimestamp": 3, "payload": []}]}  # no ``kind``: not a transcript event


def test_memory_reader_pages_transcript_events_under_the_given_actor():
    c = _FakeMemClient()
    reader = handler.MemoryReader("mem-123", "us-west-2", client=c)
    events, truncated = reader.transcript_events("team", "gh/o/r/issue-1", deadline=float("inf"))
    assert len(events) == 2 and truncated is False  # both pages collected, the non-transcript event skipped
    first = c.calls[0]
    assert first["sessionId"] == handler._memory_session_id("gh/o/r/issue-1")
    assert first["memoryId"] == "mem-123"
    assert first["actorId"] == "team"
    assert first["maxResults"] == 100 and first["includePayloads"] is True
    assert c.calls[1]["nextToken"] == "page2"  # second page followed the token


def test_build_memory_reader_gated_on_env(monkeypatch):
    monkeypatch.delenv("AGENTCORE_MEMORY_ID", raising=False)
    assert handler._build_memory_reader() is None
    monkeypatch.setenv("AGENTCORE_MEMORY_ID", "mem-xyz")
    assert isinstance(handler._build_memory_reader(), handler.MemoryReader)


# ---- the session-store transcript reader (verbatim transcript events → snapshot) -------------


class _NoSuchKey(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class _FakeS3:
    """list_objects_v2 (paginated) + get_object over an in-memory key map."""

    def __init__(self, objects: dict[str, bytes], *, page: int = 1000):
        self.objects = objects
        self.page = page
        self.gets: list[str] = []
        self.fail = False

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None, MaxKeys=1000, StartAfter=""):  # noqa: N803
        keys = sorted(k for k in self.objects if k.startswith(Prefix) and k > StartAfter)
        start = int(ContinuationToken or 0)
        page = min(self.page, MaxKeys)
        resp = {"Contents": [{"Key": k} for k in keys[start : start + page]],
                "IsTruncated": start + page < len(keys)}
        if start + page < len(keys):
            resp["NextContinuationToken"] = str(start + page)
        return resp

    def get_object(self, Bucket, Key):  # noqa: N803
        import io

        self.gets.append(Key)
        if self.fail:
            raise RuntimeError("s3 unavailable")
        if Key not in self.objects:
            raise _NoSuchKey()
        return {"Body": io.BytesIO(self.objects[Key])}


def _snapshot_obj(sid: str, messages: list) -> tuple[str, bytes]:
    key = f"session/{sid}/scopes/agent/default/snapshots/snapshot_latest.json"
    return key, json.dumps({"scope": "agent", "data": {"messages": messages}}).encode()


_TX = [
    ("user", [{"text": "first ask"}]),
    ("assistant", [{"text": "let me clone the repo"}, {"toolUse": {"name": "shell", "toolUseId": "u1", "input": {}}}]),
    ("user", [{"toolResult": {"toolUseId": "u1", "status": "success", "content": [{"text": "ok"}]}}]),
    ("assistant", [{"text": "done — opened the PR"}]),
]


def _tx_reader(objects: dict | None = None, **s3_kwargs):
    """A store reader whose session gh-o-r-1 has the four _TX messages as transcript events."""
    from fake_agentcore import MEMORY_ID, FakeAgentCoreClient
    from strandly.memory.transcript import transcript_event

    mem = FakeAgentCoreClient()
    for n, (role, content) in enumerate(_TX, 1):
        mem.create_event(
            memoryId=MEMORY_ID, actorId="team", sessionId="gh-o-r-1",
            eventTimestamp=datetime(2026, 11, 6, 0, 0, n, tzinfo=timezone.utc),
            **transcript_event({"role": role, "content": content}, seq=f"{n:020d}-{n:06d}"),
        )
    s3 = _FakeS3(objects or {}, **s3_kwargs)
    reader = handler.SessionStoreReader(
        "b", client=s3, fallback=handler.MemoryReader(MEMORY_ID, "us-west-2", client=mem)
    )
    return reader, s3


def test_store_reader_prefers_the_verbatim_transcript_log():
    reader, s3 = _tx_reader(dict([_snapshot_obj("gh-o-r-1", [{"role": "user", "content": [{"text": "SUMMARY"}]}])]))
    msgs = reader.transcript("gh/o/r/1")  # sanitized internally, like the runtime keys it
    assert [m["text"] for m in msgs] == ["first ask", "let me clone the repo", "", "done — opened the PR"]
    assert msgs[1]["tool_use"] is True and msgs[2]["tool_results"][0]["text"] == "ok"
    assert msgs[0]["timestamp"] == "2026-11-06T00:00:01+00:00"
    assert not any("snapshot_latest" in k for k in s3.gets)  # never needed the snapshot


def test_store_reader_falls_back_to_the_snapshot_else_empty():
    snap = dict([_snapshot_obj("s2", [{"role": "user", "content": [{"text": "hi"}]},
                                      {"role": "assistant", "content": [{"text": "hello"}]}])])
    reader, _ = _tx_reader(snap)
    assert [m["text"] for m in reader.transcript("s2")] == ["hi", "hello"]
    # Neither transcript events nor a snapshot → empty (the route then uses the ledger).
    assert reader.transcript("old") == []
    assert reader.pagination_transcript("old") == ([], False)
    # Without Memory configured the snapshot is the only source.
    assert [m["text"] for m in handler.SessionStoreReader("b", client=_FakeS3(snap)).transcript("s2")] == ["hi", "hello"]
    assert handler.SessionStoreReader("b", client=_FakeS3({})).transcript("old") == []


def test_store_reader_pagination_ids_are_stable_and_unique():
    reader, _ = _tx_reader(page=2)
    first, truncated = reader.pagination_transcript("gh-o-r-1")
    again, _ = reader.pagination_transcript("gh-o-r-1")
    assert truncated is False
    assert [m["id"] for m in first] == [m["id"] for m in again]
    assert len({m["id"] for m in first}) == 4
    assert "tools" not in first[0]  # identified messages carry blocks only


def test_session_detail_reads_the_store_transcript():
    reader, _ = _tx_reader()
    status, body = handler.route(
        _evt("/api/sessions/gh-o-r-1", params={"id": "gh-o-r-1"}), FakeReader(SESSION_ROWS), None, reader
    )
    assert status == 200 and body["source"] == "memory"
    assert body["messages"][-1]["text"] == "done — opened the PR"


def test_build_transcript_reader_is_the_store_over_memory_transcript_events(monkeypatch):
    monkeypatch.delenv("STRANDLY_SESSION_BUCKET", raising=False)
    monkeypatch.delenv("AGENTCORE_MEMORY_ID", raising=False)
    assert handler._build_transcript_reader() is None
    monkeypatch.setenv("AGENTCORE_MEMORY_ID", "mem-xyz")
    assert handler._build_transcript_reader() is None  # Memory alone: no actor marker, no reader
    monkeypatch.setenv("STRANDLY_SESSION_BUCKET", "strandly-dev-sessions-1")
    reader = handler._build_transcript_reader()
    assert isinstance(reader, handler.SessionStoreReader)
    assert isinstance(reader._fallback, handler.MemoryReader)
    monkeypatch.delenv("AGENTCORE_MEMORY_ID")
    assert handler._build_transcript_reader()._fallback is None


# ---- opt-in transcript pagination ----------------------------------------------------------

class _PagedMemClient:
    """Mutable, forward-only service fake: exercises the real reader and message parser."""

    def __init__(self, events, *, page_size=100):
        self.events = events
        self.page_size = page_size
        self.calls = []

    def list_events(self, **params):
        self.calls.append(params)
        offset = int(params.get("nextToken", "0"))
        end = offset + min(params["maxResults"], self.page_size)
        response = {"events": self.events[offset:end]}
        if end < len(self.events):
            response["nextToken"] = str(end)
        return response


def _mem_ev(role: str, blocks: list, ts: int, *, seq: str | None = None) -> dict:
    """A transcript event (``ListEvents`` shape) for one SDK message."""
    from strandly.memory.transcript import transcript_event

    fields = transcript_event({"role": role, "content": blocks}, seq=seq or f"{ts:020d}")
    return {"eventTimestamp": ts, "payload": fields["payload"], "metadata": fields["metadata"]}


def _identified_event(text, index, *, timestamp=None):
    ts = index if timestamp is None else timestamp
    event = _mem_ev("assistant", [{"text": text}], ts, seq=f"{ts:020d}-{index:06d}")
    return {**event, "eventId": f"event-{index:04d}"}


def _tx_memory(client):
    """The production transcript reader (store + Memory transcript events) over ``client``."""
    return handler.SessionStoreReader("b", client=_FakeS3({}), fallback=handler.MemoryReader("m", client=client))


def _session_page(reader, memory=None, *, session_id="s1", limit="2", before=None):
    query = {"limit": limit}
    if before is not None:
        query["before"] = before
    return handler.route(
        _evt(f"/api/sessions/{session_id}", params={"id": session_id}, query=query),
        reader, memory=memory,
    )


def test_session_pages_memory_newest_first_selection_and_append_stable_cursor():
    client = _PagedMemClient([_identified_event(str(i), i) for i in range(7)], page_size=2)
    memory = _tx_memory(client)
    reader = FakeReader(SESSION_ROWS)
    status, first = _session_page(reader, memory)
    assert status == 200 and first["source"] == "memory"
    assert [m["text"] for m in first["messages"]] == ["5", "6"]
    assert first["next_cursor"] and first["history_boundary"] is None
    assert len(client.calls) == 4  # the last service page is reached, not just its oldest head
    assert all(set(p) <= {"memoryId", "actorId", "sessionId", "maxResults", "includePayloads", "nextToken"}
               for p in client.calls)  # no invented reverse ListEvents flag
    client.events.append(_identified_event("new message", 7))
    pages = [first["messages"]]
    cursor = first["next_cursor"]
    while cursor:
        status, body = _session_page(reader, memory, before=cursor)
        assert status == 200
        assert body["source"] == "memory"
        pages.insert(0, body["messages"])
        cursor = body["next_cursor"]
    combined = [m for page in pages for m in page]
    assert [m["text"] for m in combined] == [str(i) for i in range(7)]
    assert len({m["id"] for m in combined}) == 7
    # A new initial read sees appended messages; an older-page read never includes them.
    assert [m["text"] for m in _session_page(reader, memory)[1]["messages"]] == ["6", "new message"]


def test_session_pages_tied_timestamps_have_stable_identity():
    events = [_identified_event("identical", i, timestamp=1) for i in range(4)]
    client = _PagedMemClient(list(reversed(events)), page_size=2)
    memory = _tx_memory(client)
    _, first = _session_page(FakeReader([]), memory)
    client.events = events  # service order among equal timestamps is not stable
    status, older = _session_page(FakeReader([]), memory, before=first["next_cursor"])
    assert status == 200 and older["next_cursor"] is None
    ids = [m["id"] for m in older["messages"] + first["messages"]]
    assert len(ids) == len(set(ids)) == 4
    assert ids == [m["id"] for m in memory.pagination_transcript("s1")[0]]


def test_session_pages_without_event_ids_distinguish_identical_messages():
    event = _mem_ev("assistant", [{"text": "identical"}], 1)
    client = _PagedMemClient([copy.deepcopy(event) for _ in range(4)])
    memory = _tx_memory(client)
    _, first = _session_page(FakeReader([]), memory)
    client.events.append(copy.deepcopy(event))
    status, older = _session_page(FakeReader([]), memory, before=first["next_cursor"])
    assert status == 200 and older["next_cursor"] is None
    assert len({m["id"] for m in older["messages"] + first["messages"]}) == 4


def test_session_pages_count_eligible_messages_not_events_and_keep_ordered_tools():
    call = _mem_ev("assistant", [{"toolUse": {"name": "shell", "input": {"command": "pwd"}}}], 3)
    result = _mem_ev("user", [{"toolResult": {"status": "success", "content": [{"text": "/repo"}]}}], 4)
    events = [_identified_event("older", 1), _mem_ev("assistant", [{"text": "  "}], 2),
              call, result, {"eventTimestamp": 5, "payload": [{"blob": "ignored"}]}]
    client = _PagedMemClient(events)
    _, body = _session_page(FakeReader([]), _tx_memory(client))
    assert len(body["messages"]) == 2 and body["next_cursor"]
    assert [m["blocks"][0]["kind"] for m in body["messages"]] == ["tool", "result"]
    assert body["messages"][0]["blocks"][0]["name"] == "shell"
    assert body["messages"][1]["blocks"][0]["text"] == "/repo"
    assert all("tools" not in m and "tool_results" not in m for m in body["messages"])


def test_session_pages_strip_payloads_but_keep_active_run_metadata_without_mutation():
    huge = "payload-" * 20_000
    rows = [{"session_id": "s1", "task_id": "live", "status": "running",
             "started_at": "2999-01-01T00:00:00+00:00", "prompt": huge,
             "result_summary": huge, "error": huge}, *SESSION_ROWS]
    before = copy.deepcopy(rows)
    client = _PagedMemClient([_identified_event(str(i), i) for i in range(3)])
    memory = _tx_memory(client)
    reader = FakeReader(rows)
    _, first = _session_page(reader, memory)
    _, older = _session_page(reader, memory, before=first["next_cursor"])
    for page in (first, older):
        assert page["runs"] == 3
        assert next(t for t in page["turns"] if t["task_id"] == "live") == {
            "task_id": "live", "status": "running", "started_at": "2999-01-01T00:00:00+00:00",
            "ended_at": None,
        }
        assert all(not ({"prompt", "result", "error"} & t.keys()) for t in page["turns"])
        assert huge not in json.dumps(page)
    assert rows == before
    # Legacy remains unpaged, with the same full turns and flat Memory aliases.
    _, legacy = handler.route(_evt("/api/sessions/s1", params={"id": "s1"}), reader, memory=memory)
    assert len(legacy["messages"]) == 3 and "next_cursor" not in legacy
    assert "tools" in legacy["messages"][0]
    assert legacy["turns"][0]["prompt"] == huge


def test_session_pages_ledger_is_message_paged_and_completion_safe():
    rows = [*SESSION_ROWS]
    reader = FakeReader(rows)
    _, first = _session_page(reader, limit="1")
    assert first["source"] == "ledger" and first["history_boundary"] == "ledger_window"
    assert [m["text"] for m in first["messages"]] == ["r3"]
    assert first["messages"][0]["timestamp"] == SESSION_ROWS[0]["ended_at"]
    assert all(not ({"prompt", "result", "error"} & t.keys()) for t in first["turns"])
    rows.insert(0, {"task_id": "new", "session_id": "s1", "status": "running",
                    "started_at": "2999-01-02", "prompt": "new prompt"})
    status, second = _session_page(reader, before=first["next_cursor"], limit="1")
    assert status == 200 and [m["text"] for m in second["messages"]] == ["third ask"]
    status, third = _session_page(reader, before=second["next_cursor"], limit="2")
    assert status == 200 and [m["text"] for m in third["messages"]] == ["first ask", "r1"]
    assert third["next_cursor"] is None


def test_session_pages_active_ledger_anchor_survives_completion_and_retains_live_status():
    live = {"task_id": "live", "session_id": "s1", "started_at": "2999-01-02",
            "status": "running", "prompt": "working on this"}
    reader = FakeReader([live, *SESSION_ROWS])
    _, first = _session_page(reader, limit="1")
    assert first["messages"][0]["text"] == "working on this"
    live.update(status="completed", result_summary="finished", ended_at="2999-01-03")
    status, older = _session_page(reader, before=first["next_cursor"], limit="2")
    assert status == 200 and [m["text"] for m in older["messages"]] == ["third ask", "r3"]
    assert older["turns"][-1]["status"] == "completed"
    assert _session_page(reader, limit="1")[1]["messages"][0]["text"] == "finished"


def test_session_pages_ledger_ties_and_failed_run():
    rows = [{"task_id": task, "session_id": "s1", "started_at": "2999-01-01",
             "prompt": f"ask {task}", "error": "failed" if task == "b" else None,
             "result_summary": f"answer {task}"} for task in ("b", "a")]
    reader = FakeReader(rows)
    _, first = _session_page(reader)
    rows.reverse()
    status, older = _session_page(reader, before=first["next_cursor"])
    assert status == 200 and older["next_cursor"] is None
    assert [m["text"] for m in older["messages"] + first["messages"]] == ["ask a", "answer a", "ask b", "failed"]
    assert first["messages"][-1]["failed"] is True


@pytest.mark.parametrize("failure", ["error", "empty", "unconfigured"])
def test_session_pages_first_page_falls_back_but_older_memory_page_never_does(failure):
    client = _PagedMemClient([_identified_event(str(i), i) for i in range(3)])
    memory = _tx_memory(client)
    reader = FakeReader(SESSION_ROWS)
    _, first = _session_page(reader, memory)
    if failure == "error":
        memory._client.fail = True  # the session store is unreachable
    elif failure == "empty":
        client.events = []
    else:
        memory = None
    status, older = _session_page(reader, memory, before=first["next_cursor"])
    assert status == (409 if failure == "empty" else 503)
    assert "messages" not in older and "source" not in older
    status, fresh = _session_page(reader, memory)
    assert status == 200 and fresh["source"] == "ledger"


def test_session_pages_ledger_cursor_stays_ledger_when_memory_appears():
    reader = FakeReader(SESSION_ROWS)
    _, first = _session_page(reader)
    client = _PagedMemClient([_identified_event("now available", 1)])
    memory = _tx_memory(client)
    status, older = _session_page(reader, memory, before=first["next_cursor"])
    assert status == 200 and older["source"] == "ledger"
    assert [m["text"] for m in older["messages"]] == ["first ask", "r1"]
    assert not client.calls


@pytest.mark.parametrize("delete_anchor", [False, True])
def test_session_pages_missing_anchor_or_older_prefix_reports_gap(delete_anchor):
    reader = FakeReader(SESSION_ROWS)
    client = _PagedMemClient([_identified_event(str(i), i) for i in range(5)])
    memory = _tx_memory(client)
    _, first = _session_page(reader, memory)
    del client.events[3 if delete_anchor else 0]
    status, body = _session_page(reader, memory, before=first["next_cursor"])
    assert status == 409 and body["code"] == "cursor_expired"
    assert "reload" in body["error"]


def test_session_pages_rolling_ledger_window_reports_gap_even_when_anchor_survives(monkeypatch):
    monkeypatch.setattr(handler, "SESSIONS_WINDOW", 2)
    rows = [dict(t) for t in SESSION_ROWS if t.get("session_id") == "s1"]
    reader = FakeReader(rows)
    _, first = _session_page(reader, limit="1")
    rows.insert(0, {"task_id": "unrelated", "session_id": "s2", "started_at": "2999-01-02"})
    status, body = _session_page(reader, before=first["next_cursor"], limit="1")
    assert status == 409 and body["code"] == "cursor_expired"


def test_session_pages_memory_ceiling_exposes_unread_tail(monkeypatch):
    monkeypatch.setattr(handler, "MEMORY_MAX_EVENTS", 4)
    client = _PagedMemClient([_identified_event(str(i), i) for i in range(6)], page_size=2)
    status, page = _session_page(FakeReader([]), _tx_memory(client))
    assert status == 200 and page["history_boundary"] == "memory_event_limit"
    assert [m["text"] for m in page["messages"]] == ["2", "3"]
    assert len(client.calls) == 2


def test_session_pages_default_limit_and_empty_known_session():
    client = _PagedMemClient([_identified_event(str(i), i) for i in range(55)])
    memory = _tx_memory(client)
    reader = FakeReader([])
    _, first = _session_page(reader, memory, limit="1")
    status, older = handler.route(
        _evt("/api/sessions/s1", params={"id": "s1"}, query={"before": first["next_cursor"]}),
        reader, memory=memory,
    )
    assert status == 200 and len(older["messages"]) == 50
    assert older["messages"][0]["text"] == "4"
    status, empty = _session_page(FakeReader([{"task_id": "live", "session_id": "s1", "status": "running"}]))
    assert status == 200 and empty["messages"] == [] and empty["next_cursor"] is None
    assert empty["turns"][0]["status"] == "running"
    assert _session_page(FakeReader([]))[0] == 404


@pytest.mark.parametrize("limit", ["", "0", "201", "-1", "1.5", "nope", "9" * 1000, None, True])
def test_session_pages_invalid_limit_is_400_before_read(limit):
    reader = FakeReader(SESSION_ROWS)
    status, _ = _session_page(reader, limit=limit)
    assert status == 400 and reader.recent_limit is None


@pytest.mark.parametrize("raw", ["", "!", "a" * 2049, "e30", "bnVsbA", "W10", "MQ", "_w", None, True])
def test_session_pages_invalid_cursor_is_400_before_read(raw):
    reader = FakeReader(SESSION_ROWS)
    status, _ = handler.route(
        _evt("/api/sessions/s1", params={"id": "s1"}, query={"before": raw}), reader,
    )
    assert status == 400 and reader.recent_limit is None


@pytest.mark.parametrize("change", [
    {"session_id": "another"}, {"v": 2}, {"v": True}, {"source": "unknown"},
    {"anchor": "not-an-id"}, {"prefix": 0}, {"extra": True},
])
def test_session_pages_cursor_schema_and_session_are_validated(change):
    reader = FakeReader(SESSION_ROWS)
    _, first = _session_page(reader)
    cursor = first["next_cursor"]
    payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    payload.update(change)
    bad = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    status, body = _session_page(reader, before=bad)
    assert status == 400 and body["error"] == "invalid transcript cursor"


def test_session_pages_limit_caps_response_and_other_query_parameters_keep_legacy():
    client = _PagedMemClient([_identified_event(str(i), i) for i in range(250)])
    memory = _tx_memory(client)
    reader = FakeReader(SESSION_ROWS)
    status, page = _session_page(reader, memory, limit="200")
    assert status == 200 and len(page["messages"]) == 200
    assert page["messages"][0]["text"] == "50" and page["next_cursor"]
    _, legacy = handler.route(
        _evt("/api/sessions/s1", params={"id": "s1"}, query={"unused": "1"}),
        reader, memory=memory,
    )
    assert len(legacy["messages"]) == 250 and "next_cursor" not in legacy
    assert legacy["turns"][0]["prompt"] == "first ask"


def test_session_pages_ledger_failure_is_not_an_empty_page():
    class BrokenReader(FakeReader):
        def recent(self, limit):
            raise RuntimeError("DynamoDB unavailable")

    status, body = _session_page(BrokenReader([]))
    assert status == 503 and "ledger unavailable" in body["error"]
    assert "messages" not in body


# ---- work products (GET /api/workproducts?session_id=) -------------------------------------


class FakeWorkProducts:
    """Stand-in for WorkProductReader: canned rows, queried by session."""

    def __init__(self, items: list[dict[str, Any]] | None = None):
        self._items = list(items or [])
        self.by_session_calls: list[tuple[str, int]] = []

    def by_session(self, session_id, *, limit=100):
        self.by_session_calls.append((session_id, limit))
        rows = [i for i in self._items if i.get("session_id") == session_id]
        rows.sort(key=lambda i: i.get("created_at", ""), reverse=True)
        return rows[:limit]


_WP_ROWS = [
    {"work_product_id": "wp1", "session_id": "gh-o-r-pr-42", "kind": "review", "status": "draft",
     "verdict": "changes-requested", "created_at": "2999-01-01T10:00:00+00:00",
     "updated_at": "2999-01-01T10:00:00+00:00",
     "findings": [{"id": "f1", "included": True}, {"id": "f2", "included": False}]},
    {"work_product_id": "wp2", "session_id": "gh-o-r-issue-9", "kind": "triage", "status": "draft",
     "verdict": "accept", "created_at": "2999-01-01T11:00:00+00:00",
     "updated_at": "2999-01-01T11:00:00+00:00"},
    {"work_product_id": "wp3", "session_id": "gh-o-r-pr-42", "kind": "review", "status": "posted",
     "verdict": "approve", "created_at": "2999-01-01T09:00:00+00:00",
     "updated_at": "2999-01-01T09:00:00+00:00"},
]


def test_workproducts_by_session_query():
    wp = FakeWorkProducts(_WP_ROWS)
    status, body = handler.route(
        _evt("/api/workproducts", query={"session_id": "gh-o-r-pr-42"}),
        FakeReader(SAMPLE), work_products=wp,
    )
    # Every artifact for the item (both statuses), newest-created first.
    assert status == 200
    assert [i["work_product_id"] for i in body["items"]] == ["wp1", "wp3"]


def test_workproducts_by_session_clamps_limit():
    wp = FakeWorkProducts(_WP_ROWS)
    cases = (("1", 1), ("9999", handler.RUNS_MAX_LIMIT), ("junk", handler.RUNS_DEFAULT_LIMIT))
    for raw, expected in cases:
        handler.route(
            _evt("/api/workproducts", query={"session_id": "s", "limit": raw}), None, work_products=wp
        )
        assert wp.by_session_calls[-1] == ("s", expected)


@pytest.mark.parametrize("query", [{}, {"session_id": ""}, {"session_id": "  "}, {"status": "draft"}])
def test_workproducts_without_session_id_is_400(query):
    wp = FakeWorkProducts(_WP_ROWS)
    status, body = handler.route(_evt("/api/workproducts", query=query), FakeReader(SAMPLE),
                                 work_products=wp)
    assert status == 400
    assert "session_id" in body["error"]
    assert wp.by_session_calls == []


def test_workproducts_is_get_only():
    wp = FakeWorkProducts(_WP_ROWS)
    status, _ = handler.route(
        _evt("/api/workproducts", method="POST", query={"session_id": "gh-o-r-pr-42"}),
        FakeReader(SAMPLE), work_products=wp,
    )
    assert status == 405


def test_workproducts_503_when_unconfigured():
    # No reader wired (STRANDLY_WORK_PRODUCT_TABLE unset) → 503, like chat without a runtime arn.
    for query in ({"session_id": "s"}, {}):
        status, body = handler.route(_evt("/api/workproducts", query=query), FakeReader(SAMPLE))
        assert status == 503
        assert "STRANDLY_WORK_PRODUCT_TABLE" in body["error"]


def test_workproducts_work_without_ledger_reader():
    # They read their own table; a missing run-ledger must not block them.
    wp = FakeWorkProducts(_WP_ROWS)
    status, body = handler.route(
        _evt("/api/workproducts", query={"session_id": "gh-o-r-pr-42"}), None, work_products=wp
    )
    assert status == 200 and len(body["items"]) == 2


def test_session_workproducts_path_is_404_not_a_session_lookup():
    # /api/sessions/{id}/workproducts is gone; it must not fall through to the transcript route.
    wp = FakeWorkProducts(_WP_ROWS)
    reader = FakeReader(SESSION_ROWS)
    status, body = handler.route(
        _evt("/api/sessions/s1/workproducts", params={"id": "s1"}), reader, work_products=wp
    )
    assert status == 404 and body["error"] == "not found"
    assert reader.recent_limit is None and wp.by_session_calls == []


def test_session_transcript_still_routes_with_work_products_wired():
    wp = FakeWorkProducts(_WP_ROWS)
    status, body = handler.route(
        _evt("/api/sessions/s1", params={"id": "s1"}), FakeReader(SESSION_ROWS), work_products=wp
    )
    assert status == 200 and body["session_id"] == "s1" and "turns" in body


def test_work_product_reader_by_session_over_a_fake_table():
    # Exercise the real WorkProductReader against a fake DynamoDB Table (parses the KeyCondition).
    class FakeTable:
        def __init__(self):
            self.items = [
                {"work_product_id": "wp1", "session_id": "s1", "created_at": "2"},
                {"work_product_id": "wp2", "session_id": "s1", "created_at": "3"},
                {"work_product_id": "wp3", "session_id": "s2", "created_at": "4"},
            ]
            self.calls: list[dict[str, Any]] = []

        def query(self, *, IndexName, KeyConditionExpression, ScanIndexForward=True, Limit=100):
            self.calls.append({"IndexName": IndexName, "Limit": Limit})
            key, value = KeyConditionExpression.get_expression()["values"]
            rows = [i for i in self.items if i.get(key.name) == value]
            rows.sort(key=lambda i: i.get("created_at", ""), reverse=not ScanIndexForward)
            return {"Items": rows[:Limit]}

    table = FakeTable()
    reader = handler.WorkProductReader(table)
    assert [r["work_product_id"] for r in reader.by_session("s1")] == ["wp2", "wp1"]
    assert [r["work_product_id"] for r in reader.by_session("s1", limit=1)] == ["wp2"]
    assert table.calls[0]["IndexName"] == handler.WORK_PRODUCT_SESSION_GSI_NAME == "by_session"
    assert table.calls[1]["Limit"] == 1


def test_work_products_kind_keeps_one_kind():
    rows = _WP_ROWS + [
        {"work_product_id": "wp9", "session_id": "gh-o-r-pr-42", "kind": "triage", "status": "draft",
         "created_at": "2999-01-02T00:00:00+00:00", "updated_at": "2999-01-02T00:00:00+00:00"},
    ]
    status, body = handler.route(
        _evt("/api/workproducts", query={"session_id": "gh-o-r-pr-42", "kind": "review"}),
        FakeReader(SESSION_ROWS),
        work_products=FakeWorkProducts(rows),
    )
    assert status == 200
    assert body["items"] and {i["kind"] for i in body["items"]} == {"review"}


def test_direct_invoke_shape_still_routes():
    # An authorized external site invokes the Lambda directly with this exact shape: v2.0, no query string
    # key at all, the id URL-encoded in rawPath and decoded in pathParameters.
    reader = FakeReader(SESSION_ROWS)
    listing = {"version": "2.0", "rawPath": "/api/sessions",
               "requestContext": {"http": {"method": "GET", "path": "/api/sessions"}}}
    status, body = handler.route(listing, reader)
    assert status == 200 and "sessions" in body
    detail = {"version": "2.0", "rawPath": "/api/sessions/s1",
              "requestContext": {"http": {"method": "GET", "path": "/api/sessions/s1"}},
              "pathParameters": {"id": "s1"}}
    status, body = handler.route(detail, reader)
    assert status == 200 and body["session_id"] == "s1"


def test_runtime_invoker_puts_the_portal_user_in_the_payload(monkeypatch):
    inv = handler.RuntimeInvoker.__new__(handler.RuntimeInvoker)
    sent: list[dict] = []
    monkeypatch.setattr(inv, "_invoke", lambda session_id, payload: sent.append(payload) or {}, raising=False)
    inv.launch("s1", "hi", trigger_source="portal:chat", portal_user="11111111-1111-4111-8111-111111111111", portal_issuer="https://issuer/pool")
    inv.launch("s1", "hi")
    assert sent == [
        {"prompt": "hi", "sessionId": "s1", "triggerSource": "portal:chat", "portalUser": "11111111-1111-4111-8111-111111111111", "portalIssuer": "https://issuer/pool"},
        {"prompt": "hi", "sessionId": "s1"},
    ]


@pytest.mark.parametrize("change,expected", [({}, True), ({"iss": "https://foreign/pool"}, False), ({"token_use": "id"}, False), ({"sub": "alice@example.com"}, False)])
def test_portal_schedule_identity_uses_verified_issuer_and_sub(monkeypatch, change, expected):
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-west-2_pool")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    issuer = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_pool"
    sub = "11111111-1111-4111-8111-111111111111"
    claims = {"sub": sub, "iss": issuer, "token_use": "access", "username": "mutable-name", **change}
    inv = FakeInvoker()
    event = _chat_post({"session_id": "s1", "message": "hi", "trigger_source": "portal:chat", "portalUser": "attacker", "portalIssuer": "fake"}, claims)
    assert handler.route(event, FakeReader(SESSION_ROWS), inv)[0] == 200
    assert inv.portal_users == [sub if expected else None]
    assert inv.portal_issuer == (issuer if expected else None)


def test_portal_schedule_identity_ignores_body_and_missing_issuer_config(monkeypatch):
    monkeypatch.delenv("COGNITO_USER_POOL_ID", raising=False)
    inv = FakeInvoker()
    event = _chat_post({"session_id": "s1", "message": "hi", "portalUser": "11111111-1111-4111-8111-111111111111", "portalIssuer": "https://issuer/pool"}, {"username": "alice"})
    assert handler.route(event, FakeReader(SESSION_ROWS), inv)[0] == 200
    assert inv.portal_users == [None]


@pytest.fixture
def receipt_identity(monkeypatch):
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "pool")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    return {"sub": "12345678-1234-1234-1234-123456789abc", "token_use": "access",
            "iss": "https://cognito-idp.us-west-2.amazonaws.com/pool"}


class ReceiptInvoker:
    def __init__(self):
        self.payloads = []

    def receipt(self, payload):
        if payload.get("capabilities") == "runReceipts":
            assert payload["action"] == "poll"
            return {"runReceipts": 1}
        self.payloads.append(payload)
        return {"status": "accepted"}


def _receipt_event(claims, method="GET", **values):
    event = _evt("/api/chat", method=method, query=values if method == "GET" else None)
    event["requestContext"]["authorizer"] = {"jwt": {"claims": claims}}
    if method == "POST":
        event["body"] = json.dumps(values)
    return event


def test_receipt_identity_is_gateway_verified_and_body_cannot_override(receipt_identity):
    invoker = ReceiptInvoker()
    status, capabilities = handler.route(_receipt_event(receipt_identity, capabilities="acp"), None, invoker=invoker)
    assert status == 200 and capabilities["runReceipts"] == 1
    sid = capabilities["sessionPrefix"] + "a" * 32
    values = {"session_id": sid, "result_id": "b" * 32, "message": "hello"}
    assert handler.route(_receipt_event(receipt_identity, "POST", **values), None, invoker=invoker)[0] == 200
    assert invoker.payloads[-1] == {"sessionId": sid, "resultId": "b" * 32, "prompt": "hello",
                                    "triggerSource": "acp:prompt", "portalUser": receipt_identity["sub"],
                                    "portalIssuer": receipt_identity["iss"]}
    for key in ("portalUser", "portalIssuer", "triggerSource", "mode", "model", "permissions", "mcpServers", "cwd"):
        status, _ = handler.route(_receipt_event(receipt_identity, "POST", **values, **{key: "forged"}), None, invoker=invoker)
        assert status == 400
    assert len(invoker.payloads) == 1
    for claims in ({}, {**receipt_identity, "token_use": "id"}, {**receipt_identity, "iss": "forged"}):
        assert handler.route(_receipt_event(claims, "POST", **values), None, invoker=invoker)[0] == 401
    other = {**receipt_identity, "sub": "23456789-1234-1234-1234-123456789abc"}
    assert handler.route(_receipt_event(other, "POST", **values), None, invoker=invoker)[0] == 400
    assert handler.route(_receipt_event(other, session_id=sid, result_id="b" * 32), None, invoker=invoker)[0] == 400
    assert handler.route(_receipt_event(other, "POST", session_id=sid, result_id="b" * 32, action="cancel"), None, invoker=invoker)[0] == 400
    assert len(invoker.payloads) == 1


def test_legacy_chat_cannot_mutate_reserved_acp_sessions(receipt_identity):
    invoker = ReceiptInvoker()
    for values in ({"session_id": "acp-reserved", "message": "hello"},
                   {"session_id": "acp-reserved", "action": "cancel"}):
        assert handler.route(_receipt_event(receipt_identity, "POST", **values), None, invoker=invoker)[0] == 400
    assert invoker.payloads == []


def test_receipt_handshake_refuses_old_runtime_without_launch(receipt_identity):
    class OldRuntime:
        def receipt(self, payload):
            assert payload["action"] == "poll"
            assert "prompt" not in payload
            return {"status": "unknown"}

    status, _ = handler.route(_receipt_event(receipt_identity, capabilities="acp"), None, invoker=OldRuntime())
    assert status == 503


class ReadTimeoutError(Exception):
    """Named like botocore's: the invoker matches the type name (no botocore import in the Lambda)."""


class ColdClient:
    def __init__(self, error=ReadTimeoutError):
        self.payloads, self.error = [], error

    def invoke_agent_runtime(self, **kwargs):
        self.payloads.append(json.loads(kwargs["payload"]))
        raise self.error("upstream detail that must not reach the caller")


def test_control_calls_on_a_cold_session_answer_retryable_starting(receipt_identity, monkeypatch):
    client = ColdClient()
    invoker = handler.RuntimeInvoker("arn:runtime", client=client)
    probe = handler.route(_receipt_event(receipt_identity, capabilities="acp"), None, invoker=ReceiptInvoker())
    sid, rid = probe[1]["sessionPrefix"] + "a" * 32, "b" * 32
    control = [
        _receipt_event(receipt_identity, capabilities="acp"),
        _receipt_event(receipt_identity, session_id=sid, result_id=rid, since="0"),
        _receipt_event(receipt_identity, "POST", session_id=sid, result_id=rid, action="cancel"),
        _evt("/api/chat", query={"session_id": "s1", "task_id": "t1"}),  # legacy portal poll
        _chat_post({"session_id": "s1", "action": "cancel"}),  # legacy portal Stop
    ]
    for event in control:
        assert handler.route(event, None, invoker=invoker) == (
            504, {"status": "starting", "retryable": True, "error": "agent session is starting; retry this call"},
        )
    assert [p.get("action") for p in client.payloads] == ["poll", "poll", "cancel", "poll", "cancel"]
    # The Lambda entrypoint serializes it: no unhandled exception, no upstream detail.
    monkeypatch.setattr(handler, "_build_invoker", lambda: invoker)
    response = handler.lambda_handler(control[1])
    assert response["statusCode"] == 504 and json.loads(response["body"]) == handler.RUNTIME_STARTING
    # Distinct from forbidden / invalid, which never reach the runtime.
    sent = len(client.payloads)
    assert handler.route(_receipt_event({}, capabilities="acp"), None, invoker=invoker)[0] == 401
    assert handler.route(_receipt_event(receipt_identity, session_id="acp-x", result_id=rid), None, invoker=invoker)[0] == 400
    assert len(client.payloads) == sent


def test_a_cold_launch_is_never_answered_as_retryable(receipt_identity):
    client = ColdClient()
    invoker = handler.RuntimeInvoker("arn:runtime", client=client)
    prefix = handler.route(_receipt_event(receipt_identity, capabilities="acp"), None, invoker=ReceiptInvoker())[1]["sessionPrefix"]
    sid = prefix + "a" * 32
    for event in (
        _receipt_event(receipt_identity, "POST", session_id=sid, result_id="b" * 32, message="go"),
        _chat_post({"session_id": "s1", "message": "go"}),
    ):
        status, body = handler.route(event, None, invoker=invoker)
        assert status == 200 and body["status"] == "starting" and "retryable" not in body
    assert len(client.payloads) == 2 and all("prompt" in p for p in client.payloads)


def test_a_real_control_failure_is_not_reported_as_starting(receipt_identity):
    invoker = handler.RuntimeInvoker("arn:runtime", client=ColdClient(error=RuntimeError))
    with pytest.raises(RuntimeError):
        handler.route(_evt("/api/chat", query={"session_id": "s1", "task_id": "t1"}), None, invoker=invoker)
