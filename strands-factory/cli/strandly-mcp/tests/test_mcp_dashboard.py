"""The dashboard-API transport (the default `serve`) — faked HTTP through the ``request`` seam."""

from __future__ import annotations

import json

import pytest
from _home_isolation import _isolated_home  # noqa: F401  (autouse: tmp HOME)

from strandly_mcp import dashboard
from strandly_mcp.auth import NotLoggedInError

_AUTH = {
    "api_url": "https://api.example.com",
    "cognito_domain": "https://pool.example.com",
    "client_id": "c-1",
    "access_token": "at-1",
    "refresh_token": "rt-1",
    "expires_at": 9e12,  # far future — no refresh unless a test forces one
}


class _FakeApi:
    """A scripted (status, body) HTTP fake recording every request the client sends."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, method, url, *, headers=None, data=None, **kw):
        self.requests.append({"method": method, "url": url, "headers": headers or {}, "data": data})
        status, body = self.responses.pop(0)
        return status, body if isinstance(body, bytes) else json.dumps(body).encode()


def _client(responses, auth=None, tmp_path=None):
    return dashboard.DashboardClient(
        auth or _AUTH,
        request=_FakeApi(responses),
        auth_file=(tmp_path / "auth.json") if tmp_path else None,
    )


def test_launch_sends_the_bearer_and_the_chat_contract():
    client = _client([(200, {"status": "accepted", "taskId": "t-1"})])
    out = client.launch("s-1", "hello", trigger_source="mcp:ask")
    assert out == {"status": "accepted", "taskId": "t-1"}
    (req,) = client._request.requests
    assert req["method"] == "POST" and req["url"] == "https://api.example.com/api/chat"
    assert req["headers"]["Authorization"] == "Bearer at-1"
    assert json.loads(req["data"]) == {"session_id": "s-1", "message": "hello", "trigger_source": "mcp:ask"}


def test_launch_defaults_to_the_portal_chat_trigger_like_the_spa():
    """Without it the turn's ledger source was empty and the user was no requester (no schedule)."""
    client = _client([(200, {"status": "accepted", "taskId": "t-1"})] * 2)
    client.launch("s-1", "hello")
    client.launch("s-1", "hello", trigger_source=None)
    first, second = (json.loads(r["data"]) for r in client._request.requests)
    assert first["trigger_source"] == "portal:chat"
    assert "trigger_source" not in second


def test_poll_builds_the_since_query():
    client = _client([(200, {"status": "running", "taskId": "t-1"})])
    client.poll("s-1", "t-1", since=-1)
    (req,) = client._request.requests
    assert req["method"] == "GET"
    assert "task_id=t-1" in req["url"] and "session_id=s-1" in req["url"] and "since=-1" in req["url"]


def test_poll_without_since_omits_the_cursor():
    client = _client([(200, {"status": "running", "taskId": "t-1"})])
    client.poll("s-1", "t-1")
    assert "since" not in client._request.requests[0]["url"]


def test_cancel_is_a_control_action():
    client = _client([(200, {"status": "cancelled"})])
    assert client.cancel("s-1") == {"status": "cancelled"}
    assert json.loads(client._request.requests[0]["data"]) == {"action": "cancel", "session_id": "s-1"}


def test_401_forces_one_refresh_then_retries(tmp_path):
    responses = [
        (401, b"unauthorized"),
        (200, {"access_token": "at-2", "expires_in": 3600}),
        (200, {"status": "accepted", "taskId": "t-1"}),
    ]
    client = _client(responses, tmp_path=tmp_path)
    assert client.launch("s", "m")["taskId"] == "t-1"
    reqs = client._request.requests
    assert "/oauth2/token" in reqs[1]["url"]
    assert reqs[2]["headers"]["Authorization"] == "Bearer at-2"
    assert json.loads((tmp_path / "auth.json").read_text())["access_token"] == "at-2"


def test_second_401_says_run_login():
    responses = [(401, b"unauthorized"), (200, {"access_token": "at-2", "expires_in": 3600}), (401, b"still")]
    with pytest.raises(NotLoggedInError, match="strandly-mcp login"):
        _client(responses).launch("s", "m")


def test_503_maps_to_chat_disabled():
    with pytest.raises(dashboard.DashboardApiError, match="not enabled"):
        _client([(503, {"error": "chat not configured"})]).launch("s", "m")


def test_other_4xx_surfaces_the_body_and_status():
    with pytest.raises(dashboard.DashboardApiError, match="400") as info:
        _client([(400, {"error": "chat requires 'session_id' and 'message'"})]).launch("s", "m")
    assert info.value.status == 400


def test_client_without_api_url_says_run_login():
    with pytest.raises(NotLoggedInError, match="strandly-mcp login"):
        dashboard.DashboardClient({"access_token": "at"})


def test_transport_failure_is_actionable():
    def boom(*a, **k):
        raise OSError("connection refused")

    client = dashboard.DashboardClient(_AUTH, request=boom)
    with pytest.raises(dashboard.DashboardApiError, match="could not reach"):
        client.launch("s", "m")


# ---- session reads ------------------------------------------------------------------------------


def test_sessions_reads_the_list_route():
    rows = [{"session_id": "gh-o-r-pr-1", "runs": 2}]
    client = _client([(200, {"sessions": rows})])
    assert client.sessions() == rows
    assert client._request.requests[0]["url"] == "https://api.example.com/api/sessions"


def test_session_reads_the_detail_route_url_encoded():
    client = _client([(200, {"session_id": "a/b", "runs": 1, "turns": []})])
    assert client.session("a/b")["runs"] == 1
    assert client._request.requests[0]["url"] == "https://api.example.com/api/sessions/a%2Fb"


def test_session_404_is_none_not_an_error():
    client = _client([(404, {"error": "session not found"})])
    assert client.session("gh-o-r-pr-9") is None


def test_summarize_sessions_keeps_the_picking_fields_and_limits():
    rows = [
        {"session_id": f"s-{i}", "description": "ask", "last_status": "completed", "last_activity": "t",
         "target": None, "runs": 1, "tokens_total": 999, "latest_prompt": "x"}
        for i in range(5)
    ]
    out = dashboard.summarize_sessions(rows, 2)
    assert [r["session"] for r in out] == ["s-0", "s-1"]
    assert set(out[0]) == {"session", "title", "description", "last_status", "last_activity", "target", "runs"}
    assert out[0]["title"] is None
    assert dashboard.summarize_sessions([{**rows[0], "title": "Named"}], 1)[0]["title"] == "Named"
    assert dashboard.summarize_sessions(rows, 0) == []


def test_clip_session_prefers_messages_clips_text_and_bounds_entries():
    long = "x" * (dashboard.SESSION_TEXT_CLIP + 50)
    messages = [
        {"role": "user", "text": long, "tool_use": False, "tools": [], "tool_results": []}
        for _ in range(dashboard.SESSION_MAX_ENTRIES + 3)
    ]
    messages[-1] = {"role": "assistant", "text": "last", "tools": ["bash"], "tool_results": [{"text": "big"}]}
    detail = {"session_id": "s", "runs": 4, "source": "memory", "turns": [{"prompt": "p"}], "messages": messages}
    out = dashboard.clip_session(detail)
    assert out["session"] == "s" and "session_id" not in out
    assert "turns" not in out and out["source"] == "memory"
    assert out["truncated"] == 3 and len(out["messages"]) == dashboard.SESSION_MAX_ENTRIES
    assert len(out["messages"][0]["text"]) == dashboard.SESSION_TEXT_CLIP
    assert out["messages"][0]["text"].endswith("…")
    # tool payloads are dropped by default; a message that also talks keeps a count
    assert out["messages"][-1] == {"role": "assistant", "text": "last", "tool_calls": 2}


def _tool_heavy_detail():
    big = "r" * (dashboard.SESSION_TOOL_CLIP + 500)
    return {
        "session_id": "s",
        "messages": [
            {"role": "user", "text": "please fix it", "blocks": [{"kind": "text", "text": "please fix it"}]},
            {
                "role": "assistant",
                "text": "",
                "tool_use": True,
                "tools": [{"name": "shell", "input": big}],
                "blocks": [{"kind": "tool", "name": "shell", "input": big}],
            },
            {
                "role": "user",
                "text": "",
                "tool_results": [{"status": "success", "text": big}],
                "blocks": [{"kind": "result", "status": "success", "text": big}],
            },
            {
                "role": "assistant",
                "text": "done",
                "tools": [{"name": "gh", "input": "pr create"}],
                "blocks": [{"kind": "text", "text": "done"}, {"kind": "tool", "name": "gh", "input": "pr create"}],
            },
        ],
    }


def test_clip_session_hides_tool_activity_by_default():
    out = dashboard.clip_session(_tool_heavy_detail())
    assert out["messages"] == [
        {"role": "user", "text": "please fix it"},
        {"role": "assistant", "text": "done", "tool_calls": 1},
    ]
    assert out["tool_messages_hidden"] == 2
    assert "rrr" not in json.dumps(out)


def test_clip_session_include_tools_returns_ordered_clipped_blocks():
    out = dashboard.clip_session(_tool_heavy_detail(), include_tools=True)
    msgs = out["messages"]
    assert len(msgs) == 4 and "tool_messages_hidden" not in out
    for m in msgs:  # no duplicate flat fields next to the ordered blocks
        assert not {"text", "tools", "tool_results", "tool_use"} & m.keys()
    assert msgs[1]["blocks"][0]["name"] == "shell"
    assert len(msgs[1]["blocks"][0]["input"]) == dashboard.SESSION_TOOL_CLIP
    assert len(msgs[2]["blocks"][0]["text"]) == dashboard.SESSION_TOOL_CLIP
    assert [b["kind"] for b in msgs[3]["blocks"]] == ["text", "tool"]


def test_clip_session_bounds_entries_after_filtering():
    tool_only = {"role": "assistant", "text": "", "blocks": [{"kind": "tool", "name": "x", "input": "y"}]}
    talk = {"role": "assistant", "text": "hi", "blocks": [{"kind": "text", "text": "hi"}]}
    msgs = [talk] * dashboard.SESSION_MAX_ENTRIES + [tool_only] * 10
    out = dashboard.clip_session({"messages": msgs})
    assert len(out["messages"]) == dashboard.SESSION_MAX_ENTRIES and "truncated" not in out
    assert out["tool_messages_hidden"] == 10


def test_clip_session_falls_back_to_ledger_turns():
    detail = {"session_id": "s", "runs": 1, "source": "ledger",
              "turns": [{"task_id": "t", "prompt": "p", "result": "r", "error": None}]}
    out = dashboard.clip_session(detail)
    assert out["turns"] == detail["turns"] and "truncated" not in out


# ---- the collect loop ---------------------------------------------------------------------------


class _ScriptedClient:
    """A DashboardClient stand-in scripting launch/poll responses for the collect loop."""

    def __init__(self, launch_resp, polls):
        self.launch_resp = launch_resp
        self.polls = list(polls)
        self.launches = []
        self.poll_calls = []

    def launch(self, session_id, message, *, trigger_source=None):
        self.launches.append({"session_id": session_id, "message": message, "trigger_source": trigger_source})
        return self.launch_resp

    def poll(self, session_id, task_id, since=None):
        self.poll_calls.append({"task_id": task_id, "since": since})
        return self.polls.pop(0)


async def _no_sleep(_s):
    return None


async def test_collect_polls_to_completion_and_reports_tools():
    client = _ScriptedClient(
        {"status": "accepted", "taskId": "t-1"},
        [
            {"status": "running", "taskId": "t-1", "events": [{"kind": "tool_start", "tool": "bash"}], "nextSeq": 7},
            {"status": "completed", "taskId": "t-1", "result": "the answer", "events": [], "nextSeq": 7},
        ],
    )
    seen = []

    async def on_activity(msg):
        seen.append(msg)

    out = await dashboard.run_dashboard_collect(client, "s-1", "hi", on_activity=on_activity, sleep=_no_sleep)
    assert out == "the answer"
    assert seen == ["→ bash"]
    assert client.launches[0]["trigger_source"] == "mcp:ask"
    assert [p["since"] for p in client.poll_calls] == [0, 7]


async def test_collect_prefers_the_final_turn_over_the_merged_result():
    client = _ScriptedClient(
        {"status": "accepted", "taskId": "t-1"},
        [{"status": "completed", "taskId": "t-1", "result": "draft (superseded)final answer",
          "lastTurnResult": "final answer", "events": [], "nextSeq": 5}],
    )
    assert await dashboard.run_dashboard_collect(client, "s", "hi", sleep=_no_sleep) == "final answer"


async def test_collect_empty_final_turn_is_a_real_answer():
    client = _ScriptedClient(
        {"status": "accepted", "taskId": "t-1"},
        [{"status": "completed", "taskId": "t-1", "result": "earlier turn", "lastTurnResult": ""}],
    )
    assert await dashboard.run_dashboard_collect(client, "s", "hi", sleep=_no_sleep) == ""


async def test_collect_own_failed_run_reports_its_own_error():
    client = _ScriptedClient(
        {"status": "accepted", "taskId": "t-1"},
        [{"status": "failed", "taskId": "t-1", "error": "boom"}],
    )
    assert await dashboard.run_dashboard_collect(client, "s", "hi", sleep=_no_sleep) == "[error] boom"


async def test_collect_reports_a_superseded_run_instead_of_polling_forever():
    client = _ScriptedClient(
        {"status": "accepted", "taskId": "t-1"},
        [{"status": "cancelled", "taskId": "t-1", "result": "partial"}],
    )
    out = await dashboard.run_dashboard_collect(client, "s", "hi", sleep=_no_sleep)
    assert out.startswith("[cancelled]") and len(client.poll_calls) == 1


async def test_collect_reports_a_launch_without_a_task_id():
    client = _ScriptedClient({"status": "error", "error": "nope"}, [])
    out = await dashboard.run_dashboard_collect(client, "s", "hi", sleep=_no_sleep)
    assert out.startswith("[error] launch failed")
