"""Unmodified ACP reference client -> real stdio -> real loopback HTTP -> native routes."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from acp import RequestError, spawn_agent_process
from acp.schema import ImageContentBlock, McpServerStdio, TextContentBlock

import strandly_mcp.auth as auth
from strandly.core.config import Config
from strandly.core.events import HarnessEvent
from strandly.serve import agentcore_app as runtime
from strandly.serve import run_receipts

TOKEN = "test-access-token-not-a-real-secret"
SUB = "12345678-1234-1234-1234-123456789abc"
ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/test-pool"
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dashboard" / "api"))
handler = importlib.import_module("handler")
# The same suite runs against a generated CLI: its executable, and its login cache under HOME.
ACP_COMMAND = os.environ.get("STRANDLY_TEST_ACP_COMMAND")
AUTH_FILE = Path(os.environ.get("STRANDLY_TEST_ACP_AUTH", ".strandly/auth.json"))


class RecordingClient:
    def __init__(self):
        self.updates = []
        self.arrived = asyncio.Event()

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update.model_dump(mode="json", by_alias=True))
        self.arrived.set()

    async def request_permission(self, **kwargs):
        pytest.fail("Remote agent must not auto-approve or request fictional permissions")

    async def read_text_file(self, **kwargs):
        pytest.fail("Remote agent must not use client filesystem")


@pytest.fixture
async def backend(tmp_path, monkeypatch):
    import strandly.memory.session_store as storage

    monkeypatch.setattr(storage, "LOCAL_SESSION_STORE_DIR", tmp_path / "store")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "test-pool")
    monkeypatch.setattr(run_receipts, "CANCEL_POLL_SECONDS", 0.01)
    config = Config(values={})
    loop = asyncio.get_running_loop()
    launched, released = asyncio.Event(), asyncio.Event()
    state = {"calls": [], "launch_delay": False, "no_receipts": False, "bad_status": False,
             "cold": {}, "invoked": []}

    async def turn(*args, cancel_signal, outcome, **kwargs):
        launched.set()
        if state.get("fail_turn"):
            raise ValueError("REMOTE PROVIDER SECRET")
        yield HarnessEvent(kind="reasoning", text="HIDDEN REASONING")
        yield HarnessEvent(kind="tool_start", tool="shell", data={"input": "PROVIDER SECRET"})
        yield HarnessEvent(kind="text", text="Visible ", data={"private": "SECRET TEXT METADATA"})
        while not released.is_set() and not cancel_signal.is_set():
            await asyncio.sleep(0.01)
        if cancel_signal.is_set():
            outcome["abandoned"] = True
            yield HarnessEvent(kind="done", data={"stop_reason": "cancelled"})
        else:
            yield HarnessEvent(kind="text", text="answer\nsecond line")
            yield HarnessEvent(kind="done", data={"stop_reason": "end_turn"})

    monkeypatch.setattr(runtime, "run_turn", turn)
    state["invoke"] = runtime.build_app(config).handlers["main"]

    class Invoker(handler.RuntimeInvoker):
        def __init__(self):
            pass

        def _invoke(self, session_id, payload):
            assert session_id == payload["sessionId"]
            kind = "probe" if payload.get("capabilities") else payload.get("action") or "launch"
            if state["cold"].get(kind, 0):
                # What the real invoker raises when a control call read-times-out on a cold session.
                assert kind != "launch"
                state["cold"][kind] -= 1
                raise handler.RuntimeStarting(session_id)
            state["invoked"].append(kind)
            if payload.get("prompt") and state.get("launch_gate"):
                asyncio.run_coroutine_threadsafe(state["launch_gate"].wait(), loop).result(10)
            if payload.get("prompt") and state["launch_delay"]:
                time.sleep(0.25)
            result = asyncio.run_coroutine_threadsafe(state["invoke"](payload), loop).result(10)
            return (
                {"status": "starting"}
                if payload.get("prompt") and state.get("starting")
                else result
            )

    class HTTP(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond("GET")

        def do_POST(self):
            self.respond("POST")

        def respond(self, method):
            parsed = urlsplit(self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state["calls"].append((method, self.path, body))
            event = {
                "rawPath": parsed.path,
                "body": body.decode(),
                "queryStringParameters": {k: v[0] for k, v in parse_qs(parsed.query).items()},
                "requestContext": {"http": {"method": method, "path": parsed.path}},
            }
            if self.headers.get("Authorization") == "Bearer " + TOKEN:
                event["requestContext"]["authorizer"] = {
                    "jwt": {
                        "claims": {
                            "sub": SUB,
                            "iss": ISSUER,
                            "token_use": "access",
                        }
                    }
                }
            if method == "POST" and b'"cancel"' in body and state.get("cancel_failures", 0):
                state["cancel_failures"] -= 1
                status, result = 503, {"error": "PRIVATE COLD START DETAILS"}
            elif method == "GET" and "result_id=" in self.path and state.get("poll_failures", 0):
                state["poll_failures"] -= 1
                status, result = 503, {"error": "PRIVATE COLD START DETAILS"}
            elif state["bad_status"]:
                status, result = 500, {"error": "BACKEND SECRET"}
            elif state["no_receipts"]:
                status, result = 200, {"status": "unknown"}
            else:
                status, result = handler.route(event, None, invoker=Invoker())
                if method == "POST" and b'"message"' in body and state.get("uncertain_launch"):
                    status, result = (
                        (504, dict(handler.RUNTIME_STARTING))  # never sent for a launch
                        if state["uncertain_launch"] == "starting"
                        else (503, {"error": "PRIVATE ACCEPTED LAUNCH DETAILS"})
                    )
            data = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), HTTP)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "home"
    home.mkdir()
    auth.save_auth(
        {
            "api_url": f"http://127.0.0.1:{server.server_port}",
            "access_token": TOKEN,
            "expires_at": time.time() + 3600,
        },
        home / AUTH_FILE,
    )
    env = {**os.environ, "HOME": str(home), "PYTHONUNBUFFERED": "1"}
    env.pop("STRANDLY_SECRETS_ARN", None)
    env.pop("STRANDLY_API_URL", None)
    state.update(env=env, launched=launched, released=released, config=config)
    try:
        yield state
    finally:
        released.set()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)


@asynccontextmanager
async def connection(backend):
    client = RecordingClient()
    command = (
        (ACP_COMMAND,)
        if ACP_COMMAND
        else (sys.executable, "-c", "from strandly.serve.cli.main import main; raise SystemExit(main())")
    )
    async with spawn_agent_process(
        client,
        *command,
        "acp",
        env=backend["env"],
        cwd=ROOT,
    ) as (agent, process):
        yield agent, client, process


async def session(agent):
    init = await agent.initialize(protocol_version=1)
    assert init.protocol_version == 1
    assert not init.agent_capabilities.load_session
    assert not init.agent_capabilities.prompt_capabilities.image
    return (
        await agent.new_session(cwd="/client/workspace/never-forwarded", mcp_servers=[])
    ).session_id


async def ask(agent, sid, text="hello", **kwargs):
    return await agent.prompt(
        session_id=sid, prompt=[TextContentBlock(type="text", text=text)], **kwargs
    )


async def test_reference_client_streams_native_public_text(backend):
    async with connection(backend) as (agent, client, process):
        sid = await session(agent)
        await agent.authenticate(method_id="strandly-login")
        prompt = asyncio.create_task(ask(agent, sid, "hello\nUnicode: λ"))
        await asyncio.wait_for(client.arrived.wait(), 10)
        assert not prompt.done(), "must emit session/update before completion"
        backend["released"].set()
        assert (await asyncio.wait_for(prompt, 10)).stop_reason == "end_turn"
        text = "".join(update["content"]["text"] for update in client.updates)
        assert text == "Visible answer\nsecond line"
        assert all(update["sessionUpdate"] == "agent_message_chunk" for update in client.updates)
        assert "SECRET" not in json.dumps(client.updates) and "HIDDEN" not in text
        calls = repr(backend["calls"])
        assert "/client/workspace" not in calls
        launch = next(json.loads(body) for method, _, body in backend["calls"] if method == "POST")
        assert launch["message"] == "hello\nUnicode: λ"


@pytest.mark.parametrize("early", [False, True])
async def test_reference_client_cancel_including_before_launch_receipt(backend, early):
    backend["launch_delay"] = early
    async with connection(backend) as (agent, client, process):
        sid = await session(agent)
        prompt = asyncio.create_task(ask(agent, sid))
        if early:
            for _ in range(200):
                if any(
                    method == "POST" and b'"message"' in body
                    for method, _, body in backend["calls"]
                ):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("launch never reached backend")
        else:
            await asyncio.wait_for(backend["launched"].wait(), 10)
        await agent.cancel(session_id=sid)
        assert (await asyncio.wait_for(prompt, 10)).stop_reason == "cancelled"
        launch = next(
            json.loads(body)
            for method, _, body in backend["calls"]
            if method == "POST" and b'"message"' in body
        )
        receipt = await run_receipts.read(
            backend["config"], {"sessionId": sid, "resultId": launch["result_id"]}
        )
        assert receipt == {"status": "cancelled"}


async def test_reference_client_rejects_unsupported_and_forged_identity(backend):
    async with connection(backend) as (agent, client, process):
        with pytest.raises(RequestError) as error:
            await agent.new_session(cwd="/")
        assert error.value.code == -32600
        sid = await session(agent)
        for kwargs in (
            {"portalUser": "forged"},
            {"mode": "auto"},
            {"model": "fake"},
            {"permission": "allow"},
        ):
            with pytest.raises(RequestError) as error:
                await ask(agent, sid, **kwargs)
            assert error.value.code == -32602
        with pytest.raises(RequestError):
            await agent.new_session(
                cwd="/", mcp_servers=[McpServerStdio(name="evil", command="sh", args=[], env=[])]
            )
        with pytest.raises(RequestError):
            await agent.prompt(
                session_id=sid,
                prompt=[ImageContentBlock(type="image", data="fake", mime_type="image/png")],
            )
        with pytest.raises(RequestError) as error:
            await agent.load_session(cwd="/", session_id=sid)
        assert error.value.code == -32601
        with pytest.raises(RequestError):
            await agent.set_session_mode(session_id=sid, mode_id="auto")
        with pytest.raises(RequestError):
            await ask(agent, "acp-other-user")
        assert not any(method == "POST" for method, _, _ in backend["calls"])
        backend["released"].set()
        assert (
            await ask(agent, sid, _meta={"portalUser": "forged", "cwd": "/forged"})
        ).stop_reason == "end_turn"
        assert "forged" not in repr(backend["calls"])


@pytest.mark.parametrize("failure", ["no_receipts", "bad_status", "no_login"])
async def test_reference_client_errors_do_not_leak_and_old_backends_fail_closed(backend, failure):
    if failure == "no_login":
        (Path(backend["env"]["HOME"]) / AUTH_FILE).unlink()
    else:
        backend[failure] = True
    async with connection(backend) as (agent, client, process):
        await agent.initialize(protocol_version=999)
        with pytest.raises(RequestError) as error:
            await agent.new_session(cwd="/")
        assert "SECRET" not in str(error.value.data) and TOKEN not in str(error.value.data)
        assert error.value.code == (-32000 if failure == "no_login" else -32603)
        assert not any(method == "POST" for method, _, _ in backend["calls"])


async def test_restart_does_not_claim_session_load_support(backend):
    backend["released"].set()
    async with connection(backend) as (agent, client, process):
        old = await session(agent)
        assert (await ask(agent, old)).stop_reason == "end_turn"
    backend["invoke"] = runtime.build_app(backend["config"]).handlers["main"]
    async with connection(backend) as (agent, client, process):
        new = await session(agent)
        assert new != old
        with pytest.raises(RequestError) as error:
            await agent.load_session(cwd="/", session_id=old)
        assert error.value.code == -32601
        with pytest.raises(RequestError) as error:
            await ask(agent, old)
        assert error.value.code == -32002
        assert (await ask(agent, new)).stop_reason == "end_turn"


@pytest.mark.parametrize("startup_delay", [0, 10.1])
async def test_strict_stdout_framing_and_parse_errors(backend, startup_delay):
    # Raw byte inspection complements (does not replace) the reference-client round trips.
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        f"import time; time.sleep({startup_delay}); from strandly.serve.cli.main import main; main()",
        "acp",
        env=backend["env"],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr = asyncio.create_task(process.stderr.read())
    try:
        # Initialization is a protocol readiness barrier, not a guessed import-time sleep.
        # Keep the malformed/batched frame deadline separate from process startup (which can
        # exceed 10s with cold imports in the combined suite). Drain stderr concurrently.
        process.stdin.write(
            b'{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":1}}\n'
        )
        await process.stdin.drain()
        ready = json.loads(await asyncio.wait_for(process.stdout.readline(), 30))
        assert ready["id"] == 0 and ready["result"]["protocolVersion"] == 1
        process.stdin.write(
            b'not-json\n{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}\n'
        )
        await process.stdin.drain()
        frames = [
            json.loads(await asyncio.wait_for(process.stdout.readline(), 10)) for _ in range(2)
        ]
        assert all(frame["jsonrpc"] == "2.0" for frame in frames)
        assert any(frame.get("error", {}).get("code") == -32700 for frame in frames)
        assert any(frame.get("id") == 1 and "result" in frame for frame in frames)
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 10)
        assert await process.stdout.read() == b""
        assert TOKEN.encode() not in await stderr
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await stderr


async def test_concurrent_prompt_rejected_and_next_prompt_works(backend):
    async with connection(backend) as (agent, client, process):
        sid = await session(agent)
        prompt = asyncio.create_task(ask(agent, sid))
        await asyncio.wait_for(client.arrived.wait(), 10)
        with pytest.raises(RequestError) as error:
            await ask(agent, sid, "overlap")
        assert error.value.code == -32600
        await agent.cancel(session_id=sid)
        assert (await asyncio.wait_for(prompt, 10)).stop_reason == "cancelled"
        backend["released"].set()
        assert (await ask(agent, sid, "next")).stop_reason == "end_turn"
        launches = [
            json.loads(body)
            for method, _, body in backend["calls"]
            if method == "POST" and b'"message"' in body
        ]
        assert len(launches) == 2
        assert launches[0]["result_id"] != launches[1]["result_id"]


async def test_eof_requests_remote_cancellation(backend):
    async with connection(backend) as (agent, client, process):
        sid = await session(agent)
        prompt = asyncio.create_task(ask(agent, sid))
        await asyncio.wait_for(client.arrived.wait(), 10)
        process.stdin.close()
        await agent.close()
        with pytest.raises(ConnectionError):
            await prompt
        await asyncio.wait_for(process.wait(), 10)
        launch = next(
            json.loads(body)
            for method, _, body in backend["calls"]
            if method == "POST" and b'"message"' in body
        )
        payload = {"sessionId": sid, "resultId": launch["result_id"]}
        for _ in range(100):
            receipt = await run_receipts.read(backend["config"], payload)
            if receipt["status"] == "cancelled":
                break
            await asyncio.sleep(0.01)
        assert receipt["status"] == "cancelled"


async def test_failed_prompt_is_sanitized_and_next_turn_can_run(backend):
    backend["fail_turn"] = True
    async with connection(backend) as (agent, client, process):
        sid = await session(agent)
        with pytest.raises(RequestError) as error:
            await ask(agent, sid)
        assert error.value.code == -32603
        assert "SECRET" not in str(error.value.data)
        assert not client.updates
        backend["fail_turn"] = False
        backend["released"].set()
        assert (await ask(agent, sid, "next")).stop_reason == "end_turn"


async def test_cold_start_ack_and_transient_poll_failure_never_repeat_launch(backend):
    backend.update(starting=True, poll_failures=2)
    backend["released"].set()
    async with connection(backend) as (agent, client, process):
        sid = await session(agent)
        assert (await asyncio.wait_for(ask(agent, sid), 10)).stop_reason == "end_turn"
        launches = [
            body
            for method, _, body in backend["calls"]
            if method == "POST" and b'"message"' in body
        ]
        assert len(launches) == 1
        assert "PRIVATE" not in str(client.updates)
        assert backend["poll_failures"] == 0


async def test_cold_start_cancel_failure_retains_intent_without_relaunch(backend):
    backend.update(launch_delay=True, starting=True, cancel_failures=1)
    async with connection(backend) as (agent, client, process):
        sid = await session(agent)
        prompt = asyncio.create_task(ask(agent, sid))
        for _ in range(200):
            launches = [
                json.loads(body)
                for method, _, body in backend["calls"]
                if method == "POST" and b'"message"' in body
            ]
            if launches:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("launch never reached backend")
        await agent.cancel(session_id=sid)
        assert (await asyncio.wait_for(prompt, 5)).stop_reason == "cancelled"
        posts = [json.loads(body) for method, _, body in backend["calls"] if method == "POST"]
        assert len([body for body in posts if "message" in body]) == 1
        cancels = [body for body in posts if body.get("action") == "cancel"]
        assert len(cancels) >= 2
        assert all(body["result_id"] == launches[0]["result_id"] for body in cancels)
        assert await run_receipts.read(
            backend["config"], {"sessionId": sid, "resultId": launches[0]["result_id"]}
        ) == {"status": "cancelled"}


@pytest.mark.parametrize("uncertain", [True, "starting"])
async def test_uncertain_acceptance_is_not_retried_and_requests_exact_cancel(backend, uncertain):
    backend["uncertain_launch"] = uncertain
    async with connection(backend) as (agent, client, process):
        sid = await session(agent)
        with pytest.raises(RequestError) as error:
            await asyncio.wait_for(ask(agent, sid), 10)
        assert error.value.code == -32603
        assert "PRIVATE" not in str(error.value.data)
        assert error.value.data.get("retryable") is not True
        assert "acceptance unknown" in error.value.data["reason"]
        posts = [json.loads(body) for method, _, body in backend["calls"] if method == "POST"]
        launches = [body for body in posts if "message" in body]
        assert len(launches) == 1
        assert any(
            body.get("action") == "cancel" and body["result_id"] == launches[0]["result_id"]
            for body in posts
        )
        payload = {"sessionId": sid, "resultId": launches[0]["result_id"]}
        for _ in range(200):
            if (await run_receipts.read(backend["config"], payload))["status"] == "cancelled":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("uncertain launch was not cancelled")


async def test_live_bridge_does_not_switch_identity_when_login_file_changes(backend):
    backend["released"].set()
    async with connection(backend) as (agent, client, process):
        sid = await session(agent)
        path = Path(backend["env"]["HOME"]) / AUTH_FILE
        login = json.loads(path.read_text())
        login.update(access_token="DIFFERENT USER SECRET", api_url="https://invalid.example")
        auth.save_auth(login, path)
        assert (await asyncio.wait_for(ask(agent, sid), 10)).stop_reason == "end_turn"
        new = (await agent.new_session(cwd="/")).session_id
        assert new != sid and new.rsplit("-", 1)[0] == sid.rsplit("-", 1)[0]
        assert (await asyncio.wait_for(ask(agent, new), 10)).stop_reason == "end_turn"
        assert "DIFFERENT USER SECRET" not in repr(backend["calls"])


@pytest.mark.parametrize("operation", ["list", "fork", "resume", "permission", "tool", "model"])
async def test_unsupported_operations_have_no_remote_side_effects(backend, operation):
    async with connection(backend) as (agent, client, process):
        sid = await session(agent)
        with pytest.raises(RequestError) as error:
            if operation == "list":
                await agent.list_sessions()
            elif operation == "fork":
                await agent.fork_session(session_id=sid, cwd="/")
            elif operation == "resume":
                await agent.resume_session(session_id=sid, cwd="/")
            else:
                # Exercise real method names, not ext_method's underscore-prefixed namespace.
                await agent._conn.send_request(
                    {"permission": "session/request_permission", "tool": "tools/call",
                     "model": "session/set_model"}[operation],
                    {"sessionId": sid, "modelId": "fake", "permission": "allow"},
                )
        assert error.value.code == -32601
        assert not any(method == "POST" for method, _, _ in backend["calls"])
        assert not client.updates


async def test_bridge_calls_deployed_api_chat_route_from_bare_login_origin(backend):
    """``strandly login`` stores the bare API origin; the only deployed route is ``/api/chat``."""
    path = Path(backend["env"]["HOME"]) / AUTH_FILE
    assert urlsplit(json.loads(path.read_text())["api_url"]).path == ""
    async with connection(backend) as (agent, client, _):
        sid = await session(agent)
        prompt = asyncio.create_task(ask(agent, sid))
        await asyncio.wait_for(client.arrived.wait(), 10)
        backend["released"].set()
        assert (await asyncio.wait_for(prompt, 10)).stop_reason == "end_turn"
    requested = {(method, urlsplit(target).path) for method, target, _ in backend["calls"]}
    assert requested == {("GET", "/api/chat"), ("POST", "/api/chat")}


@pytest.mark.parametrize("url", ["http://example.com", "https://user:SECRET@example.com",
                                 "https://example.com?token=SECRET"])
async def test_invalid_login_origins_fail_without_sending_tokens(backend, url):
    path = Path(backend["env"]["HOME"]) / AUTH_FILE
    login = json.loads(path.read_text())
    auth.save_auth({**login, "api_url": url}, path)
    async with connection(backend) as (agent, client, process):
        await agent.initialize(protocol_version=1)
        with pytest.raises(RequestError) as error:
            await agent.new_session(cwd="/")
        assert error.value.code == -32602
        assert "SECRET" not in str(error.value.data) and TOKEN not in str(error.value.data)
        assert not backend["calls"]


async def test_foreign_session_alias_cannot_cancel_or_inject_into_live_acp_run(backend):
    """The review's padded-ID exploit must not reach the owner's native run."""
    loop = asyncio.get_running_loop()
    forwarded = []

    class Invoker(handler.RuntimeInvoker):
        def __init__(self):
            pass

        def _invoke(self, session_id, payload):
            forwarded.append(payload)
            return asyncio.run_coroutine_threadsafe(backend["invoke"](payload), loop).result(10)

    invoker = Invoker()
    async with connection(backend) as (agent, _, _):
        sid = await session(agent)
        prompt = asyncio.create_task(ask(agent, sid))
        await asyncio.wait_for(backend["launched"].wait(), 10)
        launch = next(json.loads(body) for method, _, body in backend["calls"]
                      if method == "POST" and b'"message"' in body)
        payload = {"sessionId": sid, "resultId": launch["result_id"]}
        try:
            for raw in (sid, " \t" + sid, "/" + sid, "---\\" + sid):
                for field in ("session_id", "sessionId"):
                    for operation in ({"action": "cancel"}, {"message": "foreign injected task"}, {"task_id": "unknown"}):
                        method = "GET" if "task_id" in operation else "POST"
                        for receipt in (False, True):
                            values = {field: raw, **operation}
                            if receipt:
                                values.pop("task_id", None)
                                values["result_id"] = launch["result_id"]
                            event = {
                                "rawPath": "/api/chat",
                                "body": json.dumps(values) if method == "POST" else "",
                                "queryStringParameters": values if method == "GET" else {},
                                "requestContext": {
                                    "http": {"method": method},
                                    "authorizer": {"jwt": {"claims": {
                                        "sub": "23456789-1234-1234-1234-123456789abc",
                                        "iss": ISSUER, "token_use": "access",
                                    }}},
                                },
                            }
                            status, _ = await asyncio.to_thread(handler.route, event, None, invoker=invoker)
                            assert status == 400
            assert not forwarded
            assert (await run_receipts.read(backend["config"], payload))["status"] == "running"
            assert not await run_receipts.cancel_requested(backend["config"], payload)
        finally:
            backend["released"].set()
            response = await asyncio.wait_for(prompt, 10)
        assert response.stop_reason == "end_turn"
        assert (await run_receipts.read(backend["config"], payload))["status"] == "completed"


def public_text(client):
    return "".join(update["content"]["text"] for update in client.updates)


def launches(backend):
    return [json.loads(body) for method, _, body in backend["calls"]
            if method == "POST" and b'"message"' in body]


async def until(predicate):
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail("condition was not reached")


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("unicode", [False, True])
async def test_overflow_recovers_exact_answer_without_duplicate_text(backend, monkeypatch, partial, unicode):
    prefix = "already streamed: 🙂e\u0301\n" if partial else ""
    parts = [f"part-{i:04d}|" + ("🧑🏽‍💻λ\n" if unicode else "")
             for i in range(runtime._MAX_EVENTS_PER_TASK + 10)]
    expected = prefix + "".join(parts)

    async def burst(*args, **kwargs):
        if partial:
            yield HarnessEvent(kind="text", text=prefix)
            await backend["released"].wait()
        for part in parts:
            yield HarnessEvent(kind="text", text=part)
        yield HarnessEvent(kind="done", data={"stop_reason": "end_turn"})

    monkeypatch.setattr(runtime, "run_turn", burst)
    async with connection(backend) as (agent, client, _):
        sid = await session(agent)
        prompt = asyncio.create_task(ask(agent, sid))
        if partial:
            await asyncio.wait_for(client.arrived.wait(), 10)
            assert public_text(client) == prefix
            backend["released"].set()
        assert (await asyncio.wait_for(prompt, 10)).stop_reason == "end_turn"
        assert public_text(client) == expected
        assert len(launches(backend)) == 1
        receipt = await run_receipts.read(backend["config"], {
            "sessionId": sid, "resultId": launches(backend)[0]["result_id"],
        })
        assert receipt["result"] == expected


@pytest.mark.parametrize("damage", ["missing_floor", "stale", "duplicate", "out_of_order", "invalid_text"])
async def test_invalid_live_batch_recovers_only_exact_final_suffix(backend, monkeypatch, damage):
    original = handler.route
    damaged = False

    def route(event, *args, **kwargs):
        nonlocal damaged
        status, reply = original(event, *args, **kwargs)
        if reply.get("status") == "completed" and "events" in reply:
            damaged = True
            reply = dict(reply)
            if damage == "missing_floor":
                reply.pop("firstSeq")
            elif damage == "stale":
                reply["nextSeq"] = 0
            elif damage == "duplicate":
                reply["events"] = reply["events"] * 2
            elif damage == "out_of_order":
                reply["events"] = [{"seq": -1, "kind": "text", "text": "WRONG"}]
            else:
                reply["events"] = [{"seq": reply["nextSeq"] - 1, "kind": "text", "text": None}]
        return status, reply

    monkeypatch.setattr(handler, "route", route)
    async with connection(backend) as (agent, client, _):
        sid = await session(agent)
        prompt = asyncio.create_task(ask(agent, sid))
        await asyncio.wait_for(client.arrived.wait(), 10)
        backend["released"].set()
        assert (await asyncio.wait_for(prompt, 10)).stop_reason == "end_turn"
        assert damaged and public_text(client) == "Visible answer\nsecond line"
        assert len(launches(backend)) == 1


@pytest.mark.parametrize("answer", [None, "unrelated answer", "", "Visible"])
async def test_unprovable_final_answer_is_explicit_nonretryable_error(backend, monkeypatch, answer):
    original = handler.route

    def route(event, *args, **kwargs):
        status, reply = original(event, *args, **kwargs)
        if reply.get("status") == "completed" and "events" in reply:
            reply = {**reply, "result": answer, "firstSeq": reply["nextSeq"]}
        return status, reply

    monkeypatch.setattr(handler, "route", route)
    async with connection(backend) as (agent, client, _):
        sid = await session(agent)
        prompt = asyncio.create_task(ask(agent, sid))
        await asyncio.wait_for(client.arrived.wait(), 10)
        assert public_text(client) == "Visible "
        backend["released"].set()
        with pytest.raises(RequestError) as error:
            await asyncio.wait_for(prompt, 10)
        assert error.value.data["retryable"] is False
        assert "delivery incomplete/uncertain" in error.value.data["reason"]
        assert "do not repeat" in error.value.data["reason"]
        assert public_text(client) == "Visible "
        assert len(launches(backend)) == 1


async def test_gapless_but_inconsistent_final_answer_never_claims_success(backend, monkeypatch):
    original = handler.route

    def route(event, *args, **kwargs):
        status, reply = original(event, *args, **kwargs)
        if reply.get("status") == "completed" and "events" in reply:
            reply = {**reply, "result": "different durable answer"}
        return status, reply

    monkeypatch.setattr(handler, "route", route)
    backend["released"].set()
    async with connection(backend) as (agent, _, _):
        sid = await session(agent)
        with pytest.raises(RequestError) as error:
            await ask(agent, sid)
        assert error.value.data["retryable"] is False
        assert "incomplete/uncertain" in error.value.data["reason"]


async def test_cancellation_failure_is_bounded_and_explicitly_unconfirmed(backend):
    backend.update(cancel_failures=100, launch_gate=asyncio.Event(), starting=True)
    async with connection(backend) as (agent, client, _):
        sid = await session(agent)
        prompt = asyncio.create_task(ask(agent, sid))
        await until(lambda: launches(backend))
        await agent.cancel(session_id=sid)
        await until(lambda: backend["cancel_failures"] == 99)
        assert not backend["launched"].is_set()
        backend["launch_gate"].set()
        with pytest.raises(RequestError) as error:
            await asyncio.wait_for(prompt, 10)
        assert error.value.data["retryable"] is False
        assert "unconfirmed" in error.value.data["reason"]
        assert "may still be running" in error.value.data["cancellation"]
        assert backend["cancel_failures"] == 93  # notification + 3 loop + 3 cleanup attempts
        assert len(launches(backend)) == 1
        backend["released"].set()


async def test_transient_preack_cancel_and_late_task_receipt_reapply_intent(backend):
    backend.update(cancel_failures=1, launch_gate=asyncio.Event(), starting=True)
    async with connection(backend) as (agent, _, _):
        sid = await session(agent)
        prompt = asyncio.create_task(ask(agent, sid))
        await until(lambda: launches(backend))
        await agent.cancel(session_id=sid)
        await until(lambda: backend["cancel_failures"] == 0)
        assert not backend["launched"].is_set()
        backend["launch_gate"].set()
        assert (await asyncio.wait_for(prompt, 10)).stop_reason == "cancelled"
        assert len(launches(backend)) == 1
        cancels = [json.loads(body) for method, _, body in backend["calls"]
                   if method == "POST" and b'"cancel"' in body]
        assert len(cancels) >= 2
        assert all(c["result_id"] == launches(backend)[0]["result_id"] for c in cancels)


async def test_completed_before_cancel_preserves_legitimate_end_turn(backend, monkeypatch):
    original = handler.route
    complete = asyncio.Event()
    release = asyncio.Event()
    loop = asyncio.get_running_loop()

    def route(event, *args, **kwargs):
        status, reply = original(event, *args, **kwargs)
        if reply.get("status") == "completed" and "events" in reply:
            loop.call_soon_threadsafe(complete.set)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(10)
        return status, reply

    monkeypatch.setattr(handler, "route", route)
    backend["released"].set()
    async with connection(backend) as (agent, client, _):
        sid = await session(agent)
        prompt = asyncio.create_task(ask(agent, sid))
        await asyncio.wait_for(complete.wait(), 10)
        await agent.cancel(session_id=sid)
        await until(lambda: any(b'"cancel"' in body for _, _, body in backend["calls"]))
        release.set()
        assert (await asyncio.wait_for(prompt, 10)).stop_reason == "end_turn"
        assert public_text(client) == "Visible answer\nsecond line"
        assert len(launches(backend)) == 1


async def test_exhausted_receipt_retries_never_invite_relaunch(backend):
    backend.update(poll_failures=100)
    async with connection(backend) as (agent, _, _):
        sid = await session(agent)
        with pytest.raises(RequestError) as error:
            await asyncio.wait_for(ask(agent, sid), 10)
        assert error.value.data["retryable"] is False
        assert "uncertain" in error.value.data["reason"]
        assert "do not repeat" in error.value.data["reason"]
        assert backend["poll_failures"] == 97
        assert len(launches(backend)) == 1


@pytest.mark.parametrize("starting", [False, True])
async def test_acknowledged_early_cancel_is_reapplied_when_task_id_arrives(backend, monkeypatch, starting):
    # Delay durable-intent observation to prove the bridge reapplies it to a newly visible task.
    async def not_yet_observed(*args):
        return False

    async def delayed_watch(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(run_receipts, "cancel_requested", not_yet_observed)
    monkeypatch.setattr(run_receipts, "watch_cancel", delayed_watch)
    backend.update(launch_gate=asyncio.Event(), starting=starting)
    async with connection(backend) as (agent, _, _):
        sid = await session(agent)
        prompt = asyncio.create_task(ask(agent, sid))
        await until(lambda: launches(backend))
        await agent.cancel(session_id=sid)
        body = {"sessionId": sid, "resultId": launches(backend)[0]["result_id"]}
        for _ in range(500):
            raw = await run_receipts.build_storage(backend["config"]).read(run_receipts.key(body, cancel=True))
            if raw is not None:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("early intent was not persisted")
        assert not backend["launched"].is_set()
        backend["launch_gate"].set()
        assert (await asyncio.wait_for(prompt, 10)).stop_reason == "cancelled"
        cancels = [json.loads(body) for method, _, body in backend["calls"]
                   if method == "POST" and b'"cancel"' in body]
        assert len(cancels) >= 2
        assert all(c["result_id"] == launches(backend)[0]["result_id"] for c in cancels)
        assert len(launches(backend)) == 1
        assert (await run_receipts.read(backend["config"], body))["status"] == "cancelled"


async def test_completed_launch_with_lost_ack_is_never_retryable(backend, monkeypatch):
    original = handler.route
    completed = asyncio.Event()
    loop = asyncio.get_running_loop()
    real_write = run_receipts.write

    async def write(config, payload, result):
        await real_write(config, payload, result)
        if result["status"] == "completed":
            completed.set()

    def route(event, *args, **kwargs):
        status, reply = original(event, *args, **kwargs)
        if "message" in json.loads(event.get("body") or "{}"):
            asyncio.run_coroutine_threadsafe(completed.wait(), loop).result(10)
            return 503, {"error": "PRIVATE AMBIGUOUS ACCEPTANCE"}
        return status, reply

    monkeypatch.setattr(run_receipts, "write", write)
    monkeypatch.setattr(handler, "route", route)
    backend["released"].set()
    async with connection(backend) as (agent, _, _):
        sid = await session(agent)
        with pytest.raises(RequestError) as error:
            await ask(agent, sid)
        assert error.value.data["retryable"] is False
        assert error.value.data["cancellation"] == "completed"
        assert "acceptance unknown" in error.value.data["reason"]
        assert "PRIVATE" not in str(error.value.data)
        assert len(launches(backend)) == 1


async def test_end_turn_waits_for_final_receipt_and_checks_unicode_suffix(backend, monkeypatch):
    finalizing, release = asyncio.Event(), asyncio.Event()
    real_write = run_receipts.write

    async def write(config, payload, result):
        if result["status"] == "completed":
            finalizing.set()
            await release.wait()
        await real_write(config, payload, result)

    async def turn(*args, **kwargs):
        for text in ["λ", "🧑", "🏽", "\u200d", "💻", "e", "\u0301", "\n"]:
            yield HarnessEvent(kind="text", text=text)
        yield HarnessEvent(kind="done", data={"stop_reason": "end_turn"})

    monkeypatch.setattr(run_receipts, "write", write)
    monkeypatch.setattr(runtime, "run_turn", turn)
    async with connection(backend) as (agent, client, _):
        sid = await session(agent)
        prompt = asyncio.create_task(ask(agent, sid))
        await asyncio.wait_for(finalizing.wait(), 10)
        await until(lambda: any(method == "GET" and "result_id=" in path
                                for method, path, _ in backend["calls"]))
        assert not prompt.done()
        release.set()
        assert (await asyncio.wait_for(prompt, 10)).stop_reason == "end_turn"
        assert public_text(client) == "λ🧑🏽‍💻e\u0301\n"


def cold(backend, timeout="30", **kinds):
    """The session answers these control calls "starting" (the real handler's 504) N times."""
    backend["cold"].update(kinds)
    backend["env"]["STRANDLY_ACP_START_TIMEOUT"] = timeout


async def test_cold_session_probe_and_polls_are_retried_until_the_runtime_answers(backend):
    cold(backend, probe=3, poll=3)
    backend["released"].set()
    async with connection(backend) as (agent, client, _):
        sid = await session(agent)
        assert (await asyncio.wait_for(ask(agent, sid), 20)).stop_reason == "end_turn"
        assert "".join(u["content"]["text"] for u in client.updates) == "Visible answer\nsecond line"
        assert backend["cold"] == {"probe": 0, "poll": 0}
        probes = [path for method, path, _ in backend["calls"] if "capabilities=acp" in path]
        assert len(probes) == 4 and backend["invoked"].count("probe") == 1
        assert len(launches(backend)) == 1 and backend["invoked"].count("launch") == 1


async def test_cold_session_cancel_is_retried_then_acknowledged_and_recorded_once(backend):
    cold(backend, cancel=5)
    async with connection(backend) as (agent, client, _):
        sid = await session(agent)
        prompt = asyncio.create_task(ask(agent, sid))
        await until(lambda: backend["launched"].is_set() and "poll" in backend["invoked"])
        await agent.cancel(session_id=sid)
        assert (await asyncio.wait_for(prompt, 20)).stop_reason == "cancelled"
        result_id = launches(backend)[0]["result_id"]
        cancels = [json.loads(body) for method, _, body in backend["calls"]
                   if method == "POST" and b'"cancel"' in body]
        # The notification and four loop attempts were answered "starting"; one reached the runtime.
        assert len(cancels) == 6 and all(c["result_id"] == result_id for c in cancels)
        assert backend["cold"]["cancel"] == 0 and backend["invoked"].count("cancel") == 1
        payload = {"sessionId": sid, "resultId": result_id}
        assert await run_receipts.cancel_requested(backend["config"], payload)
        assert await run_receipts.read(backend["config"], payload) == {"status": "cancelled"}
        assert len(launches(backend)) == 1 and backend["invoked"].count("launch") == 1


async def test_cold_session_probe_gives_up_at_the_budget_and_stays_fail_closed(backend):
    cold(backend, timeout="1", probe=1000)
    async with connection(backend) as (agent, client, _):
        await agent.initialize(protocol_version=1)
        started = time.monotonic()
        with pytest.raises(RequestError) as error:
            await asyncio.wait_for(agent.authenticate(method_id="strandly-login"), 10)
        assert time.monotonic() - started < 5
        assert error.value.code == -32603
        assert error.value.data == {
            "reason": "Remote runtime did not start within 1s", "retryable": True, "startTimeout": True,
        }
        assert backend["cold"]["probe"] == 997  # 0.25 s + 0.5 s backoff fit the 1 s budget
        assert backend["invoked"] == [] and not launches(backend)
        backend["cold"]["probe"] = 0
        assert (await agent.new_session(cwd="/", mcp_servers=[])).session_id.startswith("acp-")


async def test_cold_session_poll_gives_up_at_the_budget_without_inviting_a_relaunch(backend):
    cold(backend, timeout="1", poll=1000)
    async with connection(backend) as (agent, client, _):
        sid = await session(agent)
        with pytest.raises(RequestError) as error:
            await asyncio.wait_for(ask(agent, sid), 10)
        assert error.value.data["retryable"] is False
        assert error.value.data["reason"] == (
            "Remote runtime did not start within 1s; completion/delivery uncertain; do not repeat task"
        )
        assert backend["cold"]["poll"] == 997  # one budget, not CONTROL_ATTEMPTS budgets
        assert len(launches(backend)) == 1 and backend["invoked"].count("launch") == 1
        assert backend["invoked"].count("cancel") == 1  # cleanup still stops the exact run
