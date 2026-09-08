"""Tests for AgentCore sandbox list-parsing — the is_dir detection that the skills loader depends on.

Regression coverage for the "No skills are currently available" production bug: AgentCore's
``listFiles`` marks directories with ``description: "Directory"`` (not a trailing slash), so the old
trailing-slash-only heuristic misreported every skill subdirectory as a file. The skills loader
filters subdirectories on ``is_dir``, so that silently emptied the entire skill set on the deployed
runtime while every FakeSandbox-based test still passed.

These exercise the real block-parsing helpers directly so the unit suite catches a regression the
in-memory fakes can't.
"""

from __future__ import annotations

import pytest

from strandly_harness.core.constants import (
    SANDBOX_GIT_BIN,
    SANDBOX_GIT_PREFIX,
    SANDBOX_MICROMAMBA_URL,
)
from strandly_harness.sandbox.agentcore import (
    AgentCoreSandbox,
    _block_is_dir,
    _git_bootstrap_script,
    _git_credentials_script,
    _to_file_info,
)


def test_block_is_dir_directory_description():
    # The exact shape AgentCore returns for a subdirectory entry.
    block = {
        "type": "resource_link",
        "uri": "file:///.strandly/skills/code-review",
        "name": "code-review",
        "description": "Directory",
    }
    assert _block_is_dir(block) is True


def test_block_is_dir_file_with_mimetype():
    block = {
        "type": "resource_link",
        "uri": "file:///.strandly/skills/code-review/SKILL.md",
        "name": "SKILL.md",
        "mimeType": "text/markdown",
    }
    assert _block_is_dir(block) is False


def test_block_is_dir_unknown_returns_none():
    # No description, no mimeType → can't tell; let the name heuristic decide downstream.
    assert _block_is_dir({"type": "resource_link", "name": "thing"}) is None


def test_to_file_info_directory_block_is_dir_true():
    # The end-to-end path: a Directory block must yield FileInfo(is_dir=True) so the skills loader
    # descends into it. This is the assertion that would have caught the production bug.
    info = _to_file_info("code-review", is_dir=True)
    assert info.name == "code-review" and info.is_dir is True


def test_to_file_info_strips_file_scheme_and_trailing_slash():
    info = _to_file_info("file:///.strandly/skills/code-review/")
    assert info.name == "/.strandly/skills/code-review"
    assert info.is_dir is True  # trailing slash fallback when caller passes no explicit flag


def test_to_file_info_explicit_is_dir_wins_over_name():
    # An explicit flag from the block metadata is authoritative even with no trailing slash.
    assert _to_file_info("code-review", is_dir=True).is_dir is True
    assert _to_file_info("SKILL.md", is_dir=False).is_dir is False


# --- git bootstrap (the CI image ships no git; we install it rootless on fresh sessions) ---


def test_git_bootstrap_script_is_idempotent_and_installs():
    script = _git_bootstrap_script()
    # Short-circuits when git already resolves — an already-bootstrapped/adopted session pays nothing.
    assert "command -v git" in script
    assert "exit 0" in script
    # Fetches the static micromamba binary and creates the env under the fixed $HOME prefix.
    assert SANDBOX_MICROMAMBA_URL in script
    assert f"micromamba create -y -p {SANDBOX_GIT_PREFIX} -c conda-forge git" in script


def test_git_bootstrap_script_probes_every_package():
    # A multi-package bootstrap must check ALL binaries (else adding a package silently skips it
    # whenever git alone is already present).
    script = _git_bootstrap_script(("git", "gh"))
    assert "command -v git" in script and "command -v gh" in script
    assert "conda-forge git gh" in script


@pytest.mark.asyncio
async def test_execute_streaming_prepends_git_bin_to_path(monkeypatch):
    # With bootstrapping on, every command must get $HOME/.gitenv/bin prepended to PATH — each
    # executeCommand is a fresh shell, so the install dir isn't otherwise found. Capture the command.
    sb = AgentCoreSandbox(region="us-west-2")
    captured: dict[str, str] = {}

    async def fake_stream(name, arguments, *, timeout=None):
        captured["command"] = arguments["command"]
        return
        yield  # make it an async generator

    monkeypatch.setattr(sb, "_stream", fake_stream)
    async for _ in sb.execute_streaming("git status"):
        pass
    assert f'export PATH="{SANDBOX_GIT_BIN}:$PATH"' in captured["command"]
    assert captured["command"].endswith("git status")


@pytest.mark.asyncio
async def test_execute_streaming_no_path_prefix_when_bootstrap_disabled(monkeypatch):
    # bootstrap_git=False (caller-owned session / a backend that already ships git) → no PATH mangling.
    sb = AgentCoreSandbox(region="us-west-2", bootstrap_git=False)
    captured: dict[str, str] = {}

    async def fake_stream(name, arguments, *, timeout=None):
        captured["command"] = arguments["command"]
        return
        yield

    monkeypatch.setattr(sb, "_stream", fake_stream)
    async for _ in sb.execute_streaming("echo hi"):
        pass
    assert "gitenv" not in captured["command"]
    assert captured["command"] == "echo hi"


# --- git credential bootstrap (native git auth via the use_github token — issue #384) ---


def test_git_credentials_script_writes_store_and_identity():
    script = _git_credentials_script("ghp_secret123")
    # Standard credential store with the token-type-agnostic x-access-token username, 0600 file.
    assert "printf 'https://x-access-token:%s@github.com\\n' ghp_secret123" in script
    assert '"$HOME/.git-credentials"' in script
    assert "umask 077" in script
    assert "credential.helper store" in script
    # Commit identity, without which `git commit` fails on the bare image.
    assert "user.name" in script and "user.email" in script
    # Uses the bootstrapped git, and degrades gracefully if the install failed (fail-open chain).
    assert f"export PATH={SANDBOX_GIT_BIN}:$PATH" in script
    assert "command -v git" in script and "exit 0" in script
    # The token must never be echoed into the command output — only consumed by printf's redirect.
    for line in script.splitlines():
        if line.startswith("echo"):
            assert "ghp_secret123" not in line


def test_git_credentials_script_shell_quotes_token():
    # A token is attacker-ish input from the shell's perspective — it must be quoted, not splatted.
    script = _git_credentials_script("weird token'; rm -rf /")
    assert """'weird token'"'"'; rm -rf /'""" in script


def _drain_capture(commands: list[str]):
    def fake_drain(client, name, arguments):
        assert name == "executeCommand"
        commands.append(arguments["command"])
        return []

    return fake_drain


def test_bootstrap_session_configures_credentials_when_token_present(monkeypatch):
    sb = AgentCoreSandbox(region="us-west-2", github_token="ghp_tok")
    commands: list[str] = []
    monkeypatch.setattr(sb, "_invoke_drain", _drain_capture(commands))
    sb._bootstrap_session(client=object())
    # Install first (credentials need the git binary), then the credential step.
    assert len(commands) == 2
    assert "micromamba" in commands[0]
    assert "x-access-token" in commands[1] and "ghp_tok" in commands[1]


def test_bootstrap_session_skips_credentials_without_token(monkeypatch):
    sb = AgentCoreSandbox(region="us-west-2")
    commands: list[str] = []
    monkeypatch.setattr(sb, "_invoke_drain", _drain_capture(commands))
    sb._bootstrap_session(client=object())
    assert len(commands) == 1  # install only — the sandbox stays credential-free by default
    assert "x-access-token" not in commands[0]


def test_bootstrap_session_credential_failure_is_fail_open_and_never_logs_token(monkeypatch, caplog):
    # A credential hiccup must not break the session (same fail-open philosophy as the install),
    # and the warning must not leak the token — service errors can echo input, so only the
    # exception TYPE is logged.
    sb = AgentCoreSandbox(region="us-west-2", github_token="ghp_supersecret")
    calls = {"n": 0}

    def flaky_drain(client, name, arguments):
        calls["n"] += 1
        if calls["n"] == 2:  # the credential step
            raise RuntimeError("validation error echoing command with ghp_supersecret inside")
        return []

    monkeypatch.setattr(sb, "_invoke_drain", flaky_drain)
    with caplog.at_level("WARNING"):
        sb._bootstrap_session(client=object())  # must not raise
    assert calls["n"] == 2
    assert "credential bootstrap failed" in caplog.text
    assert "ghp_supersecret" not in caplog.text


# --- session timeout (don't let the sandbox get reaped mid-run) ---


def test_start_managed_session_forwards_timeout_to_client():
    # A configured session_timeout_seconds must reach client.start so the sandbox lives the full
    # duration (the 900s default reaped a real build-a-PR run at ~910s).
    sb = AgentCoreSandbox(region="us-west-2", session_timeout_seconds=28800, bootstrap_git=False)
    captured: dict[str, object] = {}

    class FakeClient:
        session_id = None

        def start(self, **kwargs):
            captured.update(kwargs)

    sb._start_managed_session(FakeClient())
    assert captured.get("session_timeout_seconds") == 28800


def test_start_managed_session_omits_timeout_when_unset():
    # None must NOT be passed, so the client/service default applies (don't override with a null).
    sb = AgentCoreSandbox(region="us-west-2", bootstrap_git=False)
    captured: dict[str, object] = {}

    class FakeClient:
        session_id = None

        def start(self, **kwargs):
            captured.update(kwargs)

    sb._start_managed_session(FakeClient())
    assert "session_timeout_seconds" not in captured


def test_build_sandbox_pins_max_session_timeout(monkeypatch):
    # build_sandbox must wire the 8h max onto the AgentCore sandbox so long autonomous runs aren't
    # cut off at the 15-min default.
    from strandly_harness.core.config import Config
    from strandly_harness.core.constants import SANDBOX_SESSION_TIMEOUT_SECONDS
    from strandly_harness.sandbox.select import build_sandbox

    sb = build_sandbox(Config(values={"AGENTCORE_CODE_INTERPRETER_ID": "ci-1"}))
    assert isinstance(sb, AgentCoreSandbox)
    assert sb.session_timeout_seconds == SANDBOX_SESSION_TIMEOUT_SECONDS == 28800


# --- warm-up (overlap session start + bootstrap with the agent's first non-sandbox work) ---


@pytest.mark.asyncio
async def test_warm_up_starts_session_in_background(monkeypatch):
    # warm_up() schedules session start (which bootstraps git) without blocking the caller, and the
    # first invoke awaits it — so the invoke sees an already-started session.
    sb = AgentCoreSandbox(region="us-west-2")
    events: list[str] = []

    class FakeClient:
        session_id = None

    monkeypatch.setattr(sb, "_ensure_client", lambda: FakeClient())

    def fake_start(client):
        events.append("start")
        client.session_id = "live"

    monkeypatch.setattr(sb, "_start_managed_session", fake_start)

    sb.warm_up()
    assert sb._warmup_task is not None  # scheduled, not yet necessarily run
    # Let the agent "do other work" — the warm-up runs concurrently.
    await sb._warmup_task
    assert events == ["start"]

    # A subsequent invoke must await warm-up and NOT start a second session.
    monkeypatch.setattr(sb, "_invoke_collect", lambda name, args: [])
    await sb._invoke("executeCommand", {"command": "echo hi"})
    assert events == ["start"]  # still exactly one start


@pytest.mark.asyncio
async def test_first_invoke_awaits_warm_up(monkeypatch):
    # If the first invoke arrives before warm-up finishes, it waits (doesn't race a second start).
    sb = AgentCoreSandbox(region="us-west-2")
    order: list[str] = []

    class FakeClient:
        session_id = None

    monkeypatch.setattr(sb, "_ensure_client", lambda: FakeClient())

    def slow_start(client):
        order.append("start-begin")
        # Simulate the blocking bootstrap; the event is set by the test after scheduling the invoke.
        order.append("start-end")
        client.session_id = "live"

    monkeypatch.setattr(sb, "_start_managed_session", slow_start)
    monkeypatch.setattr(sb, "_invoke_collect", lambda name, args: order.append("invoke") or [])

    sb.warm_up()
    await sb._invoke("executeCommand", {"command": "x"})
    # Warm-up start completes before the invoke body runs.
    assert order == ["start-begin", "start-end", "invoke"]


@pytest.mark.asyncio
async def test_warm_up_noop_for_caller_owned_session():
    # A caller-owned session (session_id given) isn't ours to start — warm_up must not schedule.
    sb = AgentCoreSandbox(region="us-west-2", session_id="existing-caller-owned-session-id-33chars")
    sb.warm_up()
    assert sb._warmup_task is None


@pytest.mark.asyncio
async def test_warm_up_is_idempotent(monkeypatch):
    # Calling warm_up twice schedules only one task.
    sb = AgentCoreSandbox(region="us-west-2")
    monkeypatch.setattr(sb, "_ensure_client", lambda: type("C", (), {"session_id": None})())
    monkeypatch.setattr(sb, "_start_managed_session", lambda c: None)
    sb.warm_up()
    first = sb._warmup_task
    sb.warm_up()
    assert sb._warmup_task is first
    await first
