"""The MCP tool surface — built over scripted transport callables; `mcp` is a real dependency."""

from __future__ import annotations

import contextlib
import json

import anyio
import pytest
from _home_isolation import _isolated_home  # noqa: F401  (autouse: tmp HOME)

from strandly_mcp import agentcore, server
from strandly_mcp.auth import NotLoggedInError
from strandly_mcp.dashboard import SESSION_MAX_ENTRIES, SESSION_TEXT_CLIP, DashboardApiError

PR_URL = "https://github.com/o/r/pull/1"
PR_SID = "gh-o-r-96458419-pr-1"


def _text(result) -> str:
    """The text of a call_tool result across mcp majors: CallToolResult (2.x) / list or tuple (1.x)."""
    if hasattr(result, "content"):
        result = result.content
    if isinstance(result, tuple):
        result = result[0]
    return result[0].text


def _schema(tool) -> dict:
    return getattr(tool, "input_schema", None) or tool.inputSchema


def _hints(tool) -> dict:
    return tool.annotations.model_dump(by_alias=True, exclude_none=True)


class _ScriptedClient:
    def __init__(self, launch_resp=None, polls=(), sessions=(), detail=None):
        self.launch_resp = launch_resp or {}
        self.polls = list(polls)
        self._sessions = list(sessions)
        self._detail = detail
        self.launches = []
        self.poll_calls = []
        self.detail_calls = []

    def launch(self, session_id, message, *, trigger_source=None):
        self.launches.append({"session_id": session_id, "message": message, "trigger_source": trigger_source})
        return self.launch_resp

    def poll(self, session_id, task_id, since=None):
        self.poll_calls.append({"session_id": session_id, "task_id": task_id, "since": since})
        return self.polls.pop(0)

    def sessions(self):
        return self._sessions

    def session(self, session_id):
        self.detail_calls.append(session_id)
        return self._detail


async def _tools(srv) -> dict:
    return {t.name: t for t in await srv.list_tools()}


# ---- the served surface -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "build", [lambda: server.build_dashboard_proxy_server(_ScriptedClient()), lambda: server.build_proxy_server("arn:rt", "us-west-2")]
)
async def test_exactly_two_tools_on_both_transports(build):
    assert set(await _tools(build())) == {"ask", "session"}


async def test_server_carries_instructions_and_the_tools_stand_alone():
    srv = server.build_dashboard_proxy_server(_ScriptedClient())
    instructions = srv.instructions
    assert instructions and instructions.startswith(server.INSTRUCTIONS)
    for needle in ("operator", "`ask(", "`session(", "URL", "@mention", "wait=false"):
        assert needle in instructions, needle
    tools = await _tools(srv)
    # Strands CLI and Kiro never show INSTRUCTIONS to the model: `ask` must carry the essentials
    # itself and must not defer to text the model may not see.
    ask = tools["ask"].description
    for needle in ("TRUST BOUNDARY", "confidential", "operator controls", "public", "wait=false", "session(id)"):
        assert needle in ask, needle
    assert "server instructions" not in ask
    assert "session(url)" in _schema(tools["ask"])["properties"]["session"]["description"]
    for tool in tools.values():
        assert 80 < len(tool.description) < 1400, tool.name
        assert "JOIN" not in tool.description


async def test_ask_schema_has_described_params_and_a_boolean_wait():
    schema = _schema((await _tools(server.build_dashboard_proxy_server(_ScriptedClient())))["ask"])
    assert set(schema["properties"]) == {"prompt", "session", "wait"}  # ctx is not an argument
    assert schema["required"] == ["prompt"]
    assert schema["properties"]["wait"]["type"] == "boolean" and schema["properties"]["wait"]["default"] is True
    assert schema["properties"]["session"]["default"] is None
    for prop in schema["properties"].values():
        assert prop["description"]
    assert "URL" in schema["properties"]["session"]["description"]


async def test_session_schema_has_described_params():
    schema = _schema((await _tools(server.build_dashboard_proxy_server(_ScriptedClient())))["session"])
    assert set(schema["properties"]) == {"id", "limit", "include_tools"} and "required" not in schema
    assert schema["properties"]["limit"]["type"] == "integer" and schema["properties"]["limit"]["default"] == 20
    for prop in schema["properties"].values():
        assert prop["description"]


async def test_tool_annotations():
    tools = await _tools(server.build_dashboard_proxy_server(_ScriptedClient()))
    assert _hints(tools["ask"]) == {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}
    assert _hints(tools["session"]) == {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}


# ---- ask ----------------------------------------------------------------------------------------


async def test_ask_waits_on_one_default_thread_per_process():
    client = _ScriptedClient({"status": "accepted", "taskId": "t-1"},
                             [{"status": "completed", "lastTurnResult": "a"}, {"status": "completed", "lastTurnResult": "b"}])
    srv = server.build_dashboard_proxy_server(client)
    assert _text(await srv.call_tool("ask", {"prompt": "one"})) == "a"
    assert _text(await srv.call_tool("ask", {"prompt": "two"})) == "b"
    sids = {launch["session_id"] for launch in client.launches}
    assert len(sids) == 1 and next(iter(sids)).startswith("mcp-")
    assert client.launches[0]["trigger_source"] == "mcp:ask"


async def test_ask_joins_a_github_thread_by_url():
    client = _ScriptedClient({"status": "accepted", "taskId": "t-1"}, [{"status": "completed", "lastTurnResult": "ok"}])
    srv = server.build_dashboard_proxy_server(client)
    out = _text(await srv.call_tool("ask", {"prompt": "p", "session": "https://www.github.com/o/r/pull/1/files#r9"}))
    assert out == "ok" and client.launches[0]["session_id"] == PR_SID


async def test_ask_uses_any_other_string_verbatim():
    client = _ScriptedClient({"status": "accepted", "taskId": "t-1"}, [{"status": "completed", "lastTurnResult": "ok"}])
    srv = server.build_dashboard_proxy_server(client)
    await srv.call_tool("ask", {"prompt": "p", "session": "my-thread"})
    assert client.launches[0]["session_id"] == "my-thread"


async def test_ask_rejects_a_non_item_github_url_before_launching():
    client = _ScriptedClient({"status": "accepted", "taskId": "t-1"})
    srv = server.build_dashboard_proxy_server(client)
    out = _text(await srv.call_tool("ask", {"prompt": "p", "session": "https://github.com/o/r/commits/main"}))
    assert out.startswith("[error]") and "(issues|pull|discussions)/<number>" in out
    assert client.launches == []


async def test_ask_wait_false_launches_and_returns_the_handle():
    client = _ScriptedClient({"status": "accepted", "taskId": "t-9"})
    srv = server.build_dashboard_proxy_server(client)
    out = json.loads(_text(await srv.call_tool("ask", {"prompt": "long job", "session": PR_URL, "wait": False})))
    assert out == {"session": PR_SID, "taskId": "t-9", "status": "accepted"}
    assert client.launches == [{"session_id": PR_SID, "message": "long job", "trigger_source": "mcp:ask"}]
    assert client.poll_calls == []


async def test_ask_wait_false_without_a_session_uses_the_process_thread():
    client = _ScriptedClient({"status": "accepted", "taskId": "t-9"}, [{"status": "completed", "lastTurnResult": "x"}])
    srv = server.build_dashboard_proxy_server(client)
    out = json.loads(_text(await srv.call_tool("ask", {"prompt": "p", "wait": False})))
    assert out["session"].startswith("mcp-")
    await srv.call_tool("ask", {"prompt": "q"})
    assert client.launches[1]["session_id"] == out["session"]


async def test_ask_wait_false_surfaces_a_failed_launch():
    client = _ScriptedClient({"status": "error", "error": "nope"})
    srv = server.build_dashboard_proxy_server(client)
    out = json.loads(_text(await srv.call_tool("ask", {"prompt": "p", "wait": False})))
    assert out["taskId"] is None and out["status"] == "error" and out["error"] == "nope"


async def test_ask_streams_tool_activity_as_progress():
    client = _ScriptedClient(
        {"status": "accepted", "taskId": "t-1"},
        [{"status": "running", "events": [{"kind": "tool_start", "tool": "bash"}], "nextSeq": 1},
         {"status": "completed", "lastTurnResult": "done", "nextSeq": 1}],
    )
    srv = server.build_dashboard_proxy_server(client)
    assert _text(await srv.call_tool("ask", {"prompt": "p"})) == "done"  # ctx.info without a live session must not fail the call


# ---- session ------------------------------------------------------------------------------------


async def test_session_lists_recent_threads_with_the_picking_fields():
    rows = [{"session_id": f"s-{i}", "description": "d", "last_status": "completed", "last_activity": "t",
             "target": {"repo": "o/r"}, "runs": i, "tokens_total": 5} for i in range(3)]
    srv = server.build_dashboard_proxy_server(_ScriptedClient(sessions=rows))
    out = json.loads(_text(await srv.call_tool("session", {"limit": 2})))
    assert [r["session"] for r in out] == ["s-0", "s-1"]
    assert "tokens_total" not in out[0] and "session_id" not in out[0] and out[0]["target"] == {"repo": "o/r"}
    assert len(json.loads(_text(await srv.call_tool("session", {})))) == 3  # default limit 20 > rows
    assert len(json.loads(_text(await srv.call_tool("session", {"id": "  "})))) == 3  # blank id == no id


async def test_session_detail_clips_and_derives_latest_run_from_the_newest_turn():
    long = "r" * (SESSION_TEXT_CLIP + 10)
    detail = {"session_id": PR_SID, "runs": 2, "source": "memory",
              "messages": [{"role": "user", "text": "x" * 10000, "tools": [], "tool_results": []}] * (SESSION_MAX_ENTRIES + 1),
              "turns": [{"task_id": "t-1", "status": "completed", "prompt": "a", "result": "old", "error": None},
                        {"task_id": "t-2", "status": "failed", "prompt": "b", "result": long, "error": "boom"}]}
    client = _ScriptedClient(detail=detail)
    srv = server.build_dashboard_proxy_server(client)
    out = json.loads(_text(await srv.call_tool("session", {"id": PR_URL})))
    assert out["session"] == PR_SID and out["exists"] is True
    assert out["truncated"] == 1 and len(out["messages"]) == SESSION_MAX_ENTRIES and "turns" not in out
    assert len(out["messages"][0]["text"]) < 10000
    assert out["latest_run"] == {"taskId": "t-2", "status": "failed", "result": "r" * (SESSION_TEXT_CLIP - 1) + "…", "error": "boom"}
    assert client.detail_calls == [PR_SID] and client.poll_calls == []


async def test_session_detail_hides_tool_payloads_unless_include_tools():
    blob = "PAYLOAD" * 200
    detail = {"session_id": "s", "runs": 1, "source": "memory", "turns": [],
              "messages": [{"role": "user", "text": "go", "blocks": [{"kind": "text", "text": "go"}]},
                           {"role": "assistant", "text": "", "tools": [{"name": "shell", "input": blob}],
                            "blocks": [{"kind": "tool", "name": "shell", "input": blob}]},
                           {"role": "assistant", "text": "ok", "blocks": [{"kind": "text", "text": "ok"}]}]}
    srv = server.build_dashboard_proxy_server(_ScriptedClient(detail=detail))
    lean = json.loads(_text(await srv.call_tool("session", {"id": "s"})))
    assert [m["text"] for m in lean["messages"]] == ["go", "ok"] and lean["tool_messages_hidden"] == 1
    assert "PAYLOAD" not in json.dumps(lean)
    full = json.loads(_text(await srv.call_tool("session", {"id": "s", "include_tools": True})))
    assert full["messages"][1]["blocks"][0]["name"] == "shell" and "PAYLOAD" in json.dumps(full)


async def test_session_detail_without_turns_has_no_latest_run():
    detail = {"session_id": "s", "runs": 0, "source": "memory", "turns": [], "messages": [{"role": "user", "text": "hi"}]}
    srv = server.build_dashboard_proxy_server(_ScriptedClient(detail=detail))
    out = json.loads(_text(await srv.call_tool("session", {"id": "s"})))
    assert out["exists"] is True and out["latest_run"] is None


async def test_session_for_a_never_run_thread():
    srv = server.build_dashboard_proxy_server(_ScriptedClient(detail=None))
    out = json.loads(_text(await srv.call_tool("session", {"id": "https://github.com/o/r/issues/9"})))
    assert out == {"session": "gh-o-r-96458419-issue-9", "exists": False, "latest_run": None}


async def test_session_rejects_a_non_item_github_url():
    client = _ScriptedClient()
    srv = server.build_dashboard_proxy_server(client)
    out = _text(await srv.call_tool("session", {"id": "https://github.com/o/r"}))
    assert out.startswith("[error]") and client.detail_calls == []


async def test_session_polls_the_run_this_process_launched_and_prefers_the_final_turn():
    detail = {"session_id": PR_SID, "runs": 1, "source": "ledger",
              "turns": [{"task_id": "t-old", "status": "completed", "prompt": "a", "result": "old", "error": None}]}
    client = _ScriptedClient({"status": "accepted", "taskId": "t-9"},
                             [{"status": "running", "taskId": "t-9"},
                              {"status": "completed", "taskId": "t-9", "result": "draft…final", "lastTurnResult": "final"}],
                             detail=detail)
    srv = server.build_dashboard_proxy_server(client)
    await srv.call_tool("ask", {"prompt": "long job", "session": PR_URL, "wait": False})

    out = json.loads(_text(await srv.call_tool("session", {"id": PR_SID})))  # the id form, copied from ask's output
    assert out["latest_run"] == {"taskId": "t-9", "status": "running", "result": None}
    assert out["exists"] is True and out["turns"] == detail["turns"]
    assert client.poll_calls == [{"session_id": PR_SID, "task_id": "t-9", "since": None}]

    out = json.loads(_text(await srv.call_tool("session", {"id": PR_URL})))  # the URL form resolves to the same run
    assert out["latest_run"] == {"taskId": "t-9", "status": "completed", "result": "final", "lastTurnResult": "final"}


async def test_session_launched_run_wins_even_before_the_ledger_sees_the_thread():
    client = _ScriptedClient({"status": "accepted", "taskId": "t-9"}, [{"status": "running"}], detail=None)
    srv = server.build_dashboard_proxy_server(client)
    handle = json.loads(_text(await srv.call_tool("ask", {"prompt": "p", "wait": False})))
    out = json.loads(_text(await srv.call_tool("session", {"id": handle["session"]})))
    assert out == {"session": handle["session"], "exists": False, "latest_run": {"taskId": "t-9", "status": "running", "result": None}}


async def test_session_poll_carries_the_error_of_a_failed_run():
    client = _ScriptedClient({"status": "accepted", "taskId": "t-9"}, [{"status": "failed", "error": "boom", "result": ""}], detail=None)
    srv = server.build_dashboard_proxy_server(client)
    await srv.call_tool("ask", {"prompt": "p", "session": "s", "wait": False})
    out = json.loads(_text(await srv.call_tool("session", {"id": "s"})))
    assert out["latest_run"] == {"taskId": "t-9", "status": "failed", "result": "", "error": "boom"}


# ---- the --agentcore transport ------------------------------------------------------------------


async def test_agentcore_ask_wait_false_then_session_polls(monkeypatch):
    captured = {}

    def fake_launch(arn, region, sid, prompt, *, trigger_source=None):
        captured.update(sid=sid, prompt=prompt, trigger_source=trigger_source)
        return {"status": "accepted", "taskId": "t-9"}

    monkeypatch.setattr(agentcore, "launch_run", fake_launch)
    monkeypatch.setattr(agentcore, "poll_run", lambda arn, region, sid, tid: {"status": "running", "taskId": tid, "sid": sid})
    srv = server.build_proxy_server("arn:rt", "us-west-2")
    out = json.loads(_text(await srv.call_tool("ask", {"prompt": "long job", "session": PR_URL, "wait": False})))
    assert out == {"session": PR_SID, "taskId": "t-9", "status": "accepted"}
    assert captured == {"sid": PR_SID, "prompt": "long job", "trigger_source": "mcp:ask"}
    out = json.loads(_text(await srv.call_tool("session", {"id": PR_URL})))
    assert out == {"session": PR_SID, "latest_run": {"taskId": "t-9", "status": "running", "result": None}}


async def test_agentcore_ask_waits_on_the_stream(monkeypatch):
    events = [{"kind": "turn_start"}, {"kind": "text", "text": "draft"}, {"kind": "turn_start"}, {"kind": "text", "text": "final"}]
    seen = {}
    monkeypatch.setattr(agentcore, "stream_run", lambda arn, region, sid, prompt: seen.update(sid=sid) or iter(events))
    srv = server.build_proxy_server("arn:rt", "us-west-2")
    assert _text(await srv.call_tool("ask", {"prompt": "p", "session": PR_URL})) == "final"
    assert seen == {"sid": PR_SID}


async def test_agentcore_session_reads_are_unavailable():
    srv = server.build_proxy_server("arn:rt", "us-west-2")
    out = _text(await srv.call_tool("session", {}))
    assert out.startswith("[error]") and "--agentcore" in out and "strandly-mcp login" in out
    out = _text(await srv.call_tool("session", {"id": PR_URL}))
    assert out.startswith("[error]") and "--agentcore" in out


# ---- transport failures reach the caller (over the wire, both mcp majors) -----------------------


class _FailingClient(_ScriptedClient):
    def __init__(self, exc: Exception, **kwargs):
        super().__init__(**kwargs)
        self.exc = exc

    def launch(self, session_id, message, *, trigger_source=None):
        raise self.exc

    def poll(self, session_id, task_id, since=None):
        raise self.exc

    def sessions(self):
        raise self.exc


@contextlib.asynccontextmanager
async def _over_the_wire(srv):
    """A real MCP client session talking to ``srv`` over in-memory streams — what an IDE sees."""
    from mcp.client.session import ClientSession
    from mcp.server.lowlevel.server import NotificationOptions
    from mcp.shared.memory import create_client_server_memory_streams

    low = getattr(srv, "_lowlevel_server", None) or srv._mcp_server  # 2.x / 1.x
    async with create_client_server_memory_streams() as ((c_read, c_write), (s_read, s_write)):
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                low.run, s_read, s_write, low.create_initialization_options(NotificationOptions()), False
            )
            async with ClientSession(c_read, c_write) as cs:
                await cs.initialize()
                yield cs
            tg.cancel_scope.cancel()


def _is_error(result) -> bool:
    return bool(getattr(result, "is_error", None) if hasattr(result, "is_error") else result.isError)


@pytest.mark.parametrize(
    "tool, args",
    [
        ("ask", {"prompt": "p"}),
        ("ask", {"prompt": "p", "wait": False}),
        ("session", {}),
    ],
)
async def test_dashboard_transport_errors_reach_the_caller_with_their_message(tool, args):
    message = "the dashboard API rejected the session (401) — run `strandly-mcp login`"
    srv = server.build_dashboard_proxy_server(_FailingClient(NotLoggedInError(message)))
    async with _over_the_wire(srv) as cs:
        result = await cs.call_tool(tool, args)
    assert _is_error(result) and message in _text(result)


async def test_session_poll_failure_reaches_the_caller_with_its_message():
    client = _FailingClient(DashboardApiError("GET /api/chat failed (502): b'bad gateway'", 502),
                            launch_resp={"status": "accepted", "taskId": "t-9"})
    client.launch = lambda session_id, message, *, trigger_source=None: client.launch_resp  # launch works, poll fails
    srv = server.build_dashboard_proxy_server(client)
    async with _over_the_wire(srv) as cs:
        await cs.call_tool("ask", {"prompt": "p", "session": "s", "wait": False})
        result = await cs.call_tool("session", {"id": "s"})
    assert _is_error(result) and "GET /api/chat failed (502)" in _text(result)


async def test_agentcore_missing_boto3_reaches_the_caller(monkeypatch):
    def no_boto(*a, **k):
        raise agentcore.AwsExtraMissingError()

    monkeypatch.setattr(agentcore, "_client", no_boto)
    srv = server.build_proxy_server("arn:rt", "us-west-2")
    async with _over_the_wire(srv) as cs:
        result = await cs.call_tool("ask", {"prompt": "p", "wait": False})
    assert _is_error(result) and "strandly-mcp[aws]" in _text(result)


async def test_deployment_identity_and_scope_hints_are_explicit_configuration(monkeypatch):
    monkeypatch.setenv("STRANDLY_MCP_DEPLOYMENT_LABEL", "Example deployment")
    monkeypatch.setenv("STRANDLY_MCP_REPOSITORIES", "example-owner/example-repo")
    srv = server.build_dashboard_proxy_server(_ScriptedClient())
    assert "Example deployment" in srv.instructions
    assert "example-owner/example-repo" in srv.instructions
    assert "do not grant access" in srv.instructions
