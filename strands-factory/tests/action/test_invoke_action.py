"""The public composite action's script (``action/invoke.py``) against a mock deployment.

One local HTTP server plays both the Actions OIDC token endpoint and the deployment's
``/api/actions/invoke``; the script runs as a subprocess with the environment the composite action
gives it, exactly as on a runner.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

_ACTION = Path(__file__).resolve().parents[2] / "action"
SCRIPT = _ACTION / "invoke.py"
TOKEN = "eyJ.fake.token"


class Deployment:
    """Scripted answers per request; records what the script sent."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.launches: list[tuple[int, dict[str, Any]]] = [(200, {"status": "accepted", "taskId": "t-1",
                                                                   "sessionId": "gh-o-r-issue-5"})]
        self.polls: list[tuple[int, dict[str, Any]]] = [(200, {"status": "completed", "result": "all good"})]

    def answer(self, method: str, path: str, query: dict[str, list[str]], headers: Any,
               body: bytes) -> tuple[int, dict[str, Any]]:
        if path == "/token":
            assert headers.get("Authorization") == "bearer request-token"
            return 200, {"value": TOKEN, "audience": query.get("audience", [""])[0]}
        self.requests.append({"method": method, "path": path, "query": query,
                              "auth": headers.get("Authorization"),
                              "body": json.loads(body) if body else None})
        queue = self.launches if method == "POST" else self.polls
        return queue.pop(0) if len(queue) > 1 else queue[0]


@pytest.fixture
def server():
    deployment = Deployment()
    tokens: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            query = parse_qs(url.query)
            if url.path == "/token":
                tokens.append(query.get("audience", [""])[0])
            status, payload = deployment.answer(self.command, url.path, query, self.headers, body)
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = _serve

        def log_message(self, *args: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    deployment.base = f"http://127.0.0.1:{httpd.server_address[1]}"  # type: ignore[attr-defined]
    deployment.tokens = tokens  # type: ignore[attr-defined]
    yield deployment
    httpd.shutdown()


def _run(server: Deployment, tmp_path: Path, *, event: dict | None = None, event_name: str = "issues",
         with_token: bool = True, **inputs: str) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
    output, summary, event_path = tmp_path / "out", tmp_path / "summary", tmp_path / "event.json"
    event_path.write_text(json.dumps(event or {}))
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_EVENT_NAME": event_name,
        "STRANDLY_ALLOW_HTTP": "1",
        "STRANDLY_POLL_INTERVAL": "0",
        "STRANDLY_URL": server.base + "/",  # type: ignore[attr-defined]
        "STRANDLY_PROMPT": "Triage this issue.",
        "STRANDLY_AUDIENCE": "strandly",
        **{f"STRANDLY_{k.upper()}": v for k, v in inputs.items()},
    }
    if with_token:
        env["ACTIONS_ID_TOKEN_REQUEST_URL"] = server.base + "/token?api-version=2.0"  # type: ignore[attr-defined]
        env["ACTIONS_ID_TOKEN_REQUEST_TOKEN"] = "request-token"
    proc = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    outputs: dict[str, str] = {}
    if output.exists():
        lines = output.read_text().splitlines()
        i = 0
        while i < len(lines):
            key, delimiter = lines[i].split("<<", 1)
            end = lines.index(delimiter, i + 1)
            outputs[key] = "\n".join(lines[i + 1:end])
            i = end + 1
    return proc, outputs


def test_fire_and_forget_launch_derives_the_issue_session(server, tmp_path):
    proc, out = _run(server, tmp_path, event={"issue": {"number": 5}})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out == {"status": "accepted", "session_id": "gh-o-r-issue-5", "task_id": "t-1", "result": ""}
    (launch,) = server.requests
    assert launch["method"] == "POST" and launch["path"] == "/api/actions/invoke"
    assert launch["auth"] == f"Bearer {TOKEN}"
    assert launch["body"] == {"prompt": "Triage this issue.", "session": "issue:5"}
    assert server.tokens == ["strandly"]  # type: ignore[attr-defined]
    assert f"::add-mask::{TOKEN}" in proc.stdout


@pytest.mark.parametrize(
    ("event_name", "event", "session"),
    [
        ("issue_comment", {"issue": {"number": 9, "pull_request": {"url": "x"}}}, "pr:9"),
        ("discussion", {"discussion": {"number": 3}}, "discussion:3"),
        ("workflow_dispatch", {"inputs": {}}, ""),
        ("push", {}, ""),
    ],
)
def test_session_comes_from_the_event(server, tmp_path, event_name, event, session):
    proc, _ = _run(server, tmp_path, event=event, event_name=event_name)
    assert proc.returncode == 0, proc.stdout
    assert server.requests[0]["body"]["session"] == session


def test_an_explicit_session_wins(server, tmp_path):
    _run(server, tmp_path, event={"issue": {"number": 5}}, session="port-harness-sdk-42")
    assert server.requests[0]["body"]["session"] == "port-harness-sdk-42"


def test_wait_polls_to_completion_and_outputs_the_result(server, tmp_path):
    server.polls = [(504, {"status": "starting", "retryable": True}), (200, {"status": "running"}),
                    (200, {"status": "completed", "result": "line one\nline two"})]
    proc, out = _run(server, tmp_path, wait="true")
    assert proc.returncode == 0, proc.stdout
    assert out["status"] == "completed" and out["result"] == "line one\nline two"
    polls = [r for r in server.requests if r["method"] == "GET"]
    assert len(polls) == 3
    assert polls[0]["query"] == {"session_id": ["gh-o-r-issue-5"], "task_id": ["t-1"]}
    assert all(r["auth"] == f"Bearer {TOKEN}" for r in polls)
    assert "line two" in (tmp_path / "summary").read_text()


def test_a_cold_launch_is_repeated_until_it_has_a_task(server, tmp_path):
    server.launches = [(200, {"status": "starting", "sessionId": "gh-o-r-issue-5"}), (502, {"message": "x"}),
                       (200, {"status": "accepted", "taskId": "t-9", "sessionId": "gh-o-r-issue-5",
                              "duplicate": True})]
    proc, out = _run(server, tmp_path)
    assert proc.returncode == 0, proc.stdout
    assert out["task_id"] == "t-9"
    bodies = [r["body"] for r in server.requests]
    assert len(bodies) == 3 and all(b == bodies[0] for b in bodies)


def test_a_failed_run_fails_the_step(server, tmp_path):
    server.polls = [(200, {"status": "failed", "error": "boom"})]
    proc, out = _run(server, tmp_path, wait="true")
    assert proc.returncode == 1
    assert out["status"] == "failed" and out["result"] == "boom"
    assert "::error::run failed" in proc.stdout


def test_a_denied_call_fails_with_the_reason(server, tmp_path):
    server.launches = [(403, {"error": "only workflows running on refs/heads/main are allowed"})]
    proc, out = _run(server, tmp_path)
    assert proc.returncode == 1 and out["status"] == "error"
    assert "HTTP 403" in proc.stdout and "refs/heads/main" in proc.stdout


def test_wait_timeout_warns_with_the_last_status(server, tmp_path):
    server.polls = [(200, {"status": "running"})]
    proc, out = _run(server, tmp_path, wait="true", timeout="0.5")
    assert proc.returncode == 0
    assert out["status"] == "running" and "::warning::run did not finish" in proc.stdout


def test_missing_id_token_permission_is_explained(server, tmp_path):
    proc, out = _run(server, tmp_path, with_token=False)
    assert proc.returncode == 1 and "id-token: write" in proc.stdout
    assert server.requests == []


def test_plain_http_urls_are_refused_outside_tests(server, tmp_path):
    env = {"PATH": os.environ.get("PATH", ""), "STRANDLY_URL": "http://example.com", "STRANDLY_PROMPT": "x"}
    proc = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 1 and "https://" in proc.stdout


def test_action_metadata_wires_inputs_to_the_script():
    text = (_ACTION / "action.yml").read_text()
    inputs = ("url", "prompt", "session", "wait", "timeout", "audience")
    section = text.split("\ninputs:\n", 1)[1].split("\noutputs:\n", 1)[0]
    assert re.findall(r"^  ([a-z_]+):$", section, re.M) == list(inputs)
    outputs = text.split("\noutputs:\n", 1)[1].split("\nruns:\n", 1)[0]
    assert re.findall(r"^  ([a-z_]+):$", outputs, re.M) == ["status", "session_id", "task_id", "result"]
    assert "using: composite" in text
    # Inputs reach the script only through env, never interpolated into the shell command.
    run = re.search(r"^      run: (.+)$", text, re.M)
    assert run and "${{" not in run.group(1)
    for name in inputs:
        assert f"        STRANDLY_{name.upper()}: ${{{{ inputs.{name} }}}}\n" in text
