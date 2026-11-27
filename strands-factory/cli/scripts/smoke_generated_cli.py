"""Smoke-test an installed generated CLI against a stub dashboard API, over real stdio.

Run with a Python that has ``mcp`` and ``agent-client-protocol`` (the clients):

    python cli/scripts/smoke_generated_cli.py /path/to/venv/bin/<command>

Checks the help screens, that the CLI's environment has no harness, ``whoami``/``sessions``/
``chat`` over the login, the ``identity`` header, MCP ``ask``/``session`` over stdio, and that ACP initializes and needs
a login in a clean home. No network beyond 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from acp import RequestError, spawn_agent_process
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOKEN = "smoke-access-token-not-a-secret"
SESSION = {"session_id": "cli-smoke", "description": "smoke session", "last_status": "completed", "runs": 1}


class StubApi(BaseHTTPRequestHandler):
    """The dashboard API routes a client uses: chat launch/poll and the session reads."""

    prompts: list[str] = []
    persons: list[str] = []

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {TOKEN}":
            return True
        self._send(401, {"error": "unauthorized"})
        return False

    def do_POST(self) -> None:
        if not self._authorized():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if urlsplit(self.path).path == "/api/chat" and body.get("message"):
            self.prompts.append(body["message"])
            self.persons.append(self.headers.get("X-Strandly-Person") or "")
            self._send(200, {"status": "accepted", "taskId": f"task-{len(self.prompts)}"})
            return
        self._send(404, {"error": "not found"})

    def do_GET(self) -> None:
        if not self._authorized():
            return
        url = urlsplit(self.path)
        if url.path == "/api/chat" and parse_qs(url.query).get("whoami") == ["1"]:
            person = self.headers.get("X-Strandly-Person") or ""
            self._send(200, {"github": person.removeprefix("github:") or None, "person": None})
        elif url.path == "/api/chat":
            task = parse_qs(url.query).get("task_id", [""])[0]
            prompt = self.prompts[int(task.removeprefix("task-")) - 1]
            self._send(200, {"status": "completed", "lastTurnResult": f"stub answer: {prompt}", "nextSeq": 1})
        elif url.path == "/api/sessions":
            self._send(200, {"sessions": [SESSION]})
        elif url.path == f"/api/sessions/{SESSION['session_id']}":
            self._send(200, {"session_id": SESSION["session_id"], "turns": []})
        else:
            self._send(404, {"error": "not found"})


def run(command: list[str], env: dict[str, str], *, expect: int = 0) -> str:
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == expect, f"{command}: exit {result.returncode}\n{result.stdout}\n{result.stderr}"
    return result.stdout


async def mcp_smoke(cli: str, env: dict[str, str]) -> None:
    params = StdioServerParameters(command=cli, args=["mcp"], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = sorted(tool.name for tool in (await session.list_tools()).tools)
        assert tools == ["ask", "session"], tools
        answer = await session.call_tool("ask", {"prompt": "ping over mcp"})
        text = "".join(getattr(block, "text", "") for block in answer.content)
        assert "stub answer: ping over mcp" in text, text
        listed = await session.call_tool("session", {})
        assert "cli-smoke" in "".join(getattr(block, "text", "") for block in listed.content)


class AcpClient:
    async def session_update(self, **kwargs: object) -> None:
        raise AssertionError("no prompt was sent")


async def acp_smoke(cli: str, env: dict[str, str], cwd: str) -> None:
    async with spawn_agent_process(AcpClient(), cli, "acp", cwd=cwd, env=env) as (agent, _):
        result = await agent.initialize(protocol_version=1)
        assert result.protocol_version == 1 and not result.agent_capabilities.load_session
        assert result.agent_info.name == "strandly-remote"
        try:
            await agent.new_session(cwd=cwd)
        except RequestError as exc:
            assert exc.code == -32000, exc
        else:
            raise AssertionError("a clean home must require a login")


def main() -> int:
    cli = str(Path(sys.argv[1]).resolve())
    name = Path(cli).name
    python = str(Path(cli).parent / "python")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        clean = Path(tmp) / "clean"
        home.mkdir()
        clean.mkdir()
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "PYTHONUNBUFFERED": "1"}
        for args in ([], ["login"], ["acp"], ["mcp"], ["chat"], ["sessions"]):
            assert "usage:" in run([cli, *args, "--help"], env)
        run([cli, "--version"], env)
        isolation = "import importlib.util as u; print([m for m in ('strandly', 'strands', 'strands_harness', 'strandly_cli', 'strandly_mcp') if u.find_spec(m)])"
        assert run([python, "-c", isolation], env).strip() == "[]", "the CLI's environment must not have the harness"
        run([cli, "whoami"], env, expect=1)

        server = ThreadingHTTPServer(("127.0.0.1", 0), StubApi)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            auth_file = home / ".strandly" / "clients" / name / "auth.json"
            auth_file.parent.mkdir(parents=True)
            auth_file.write_text(
                json.dumps(
                    {
                        "api_url": f"http://127.0.0.1:{server.server_port}",
                        "cognito_domain": "https://stub.auth.us-west-2.amazoncognito.com",
                        "client_id": "stubclient",
                        "access_token": TOKEN,
                        "expires_at": time.time() + 3600,
                    }
                )
            )
            assert "stubclient" in run([cli, "whoami"], env)
            assert TOKEN not in run([cli, "whoami"], env)
            assert json.loads(run([cli, "sessions", "--json"], env))[0]["session"] == "cli-smoke"
            assert "stub answer: hi" in run([cli, "chat", "--prompt", "hi"], env)
            assert "set by hand" in run([cli, "identity", "--github", "smoke-user"], env)
            assert "person: not in the people directory" in run([cli, "whoami"], env)
            run([cli, "chat", "--prompt", "again"], env)
            assert StubApi.persons[-2:] == ["", "github:smoke-user"], StubApi.persons
            asyncio.run(mcp_smoke(cli, env))
        finally:
            server.shutdown()
            server.server_close()
        asyncio.run(acp_smoke(cli, {**env, "HOME": str(clean)}, str(clean)))
    print(f"generated CLI {name}: help, isolation, whoami/sessions/chat/identity, MCP and ACP over stdio: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
