"""Tests for ``sandbox_main.py`` — the sandbox runtime's entrypoint.

Small file, load-bearing job: it writes the sandbox git credential and keeps that explicit
credential's delivery channel the wake payload rather than a
shell command (the service writes every executed command to CloudWatch; it does not log payloads).

Imported by path, not by import name: the module lives at the repo root because that is what the
AgentCore toolkit packages as the runtime entrypoint, and it deliberately imports nothing from the
harness (no Strands SDK in the box the model runs code in, and a sub-30s cold start).
"""

from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path

import pytest

pytest.importorskip("bedrock_agentcore", reason="the `agentcore` extra provides the runtime contract")

_ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("strandly_sandbox_main", _ROOT / "sandbox" / "sandbox_main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sandbox_main():
    return _load()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A private ``$HOME``, since the credential file's whole location is ``~``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_the_credential_is_written_where_git_will_look_for_it(sandbox_main, home):
    assert sandbox_main.configure_git_credential("ghs_secret") == "written"
    stored = (home / ".git-credentials").read_text()
    # x-access-token works for classic PATs, fine-grained PATs and App installation tokens alike.
    assert stored == "https://x-access-token:ghs_secret@github.com\n"


def test_the_credential_file_is_not_world_readable(sandbox_main, home):
    """0600. Model-authored code in this container can read it anyway (that is documented and
    accepted — the credential is scoped to ``Contents`` for exactly that reason), but the file should
    not be readable by anything *else* that ends up in the image."""
    sandbox_main.configure_git_credential("ghs_secret")
    mode = stat.S_IMODE((home / ".git-credentials").stat().st_mode)
    assert mode == 0o600, f"credential file mode is {mode:o}"


def test_a_rotated_credential_replaces_the_old_one(sandbox_main, home):
    """Rewritten whole, never appended: git tries stored credentials in order, so an appended
    rotation would leave the *revoked* one to be tried first and fail every push."""
    sandbox_main.configure_git_credential("ghs_old")
    sandbox_main.configure_git_credential("ghs_new")
    stored = (home / ".git-credentials").read_text()
    assert "ghs_new" in stored and "ghs_old" not in stored


def test_a_write_failure_is_reported_not_raised(sandbox_main, monkeypatch, tmp_path):
    """The entrypoint must never raise: to the harness that reads as "the sandbox is unavailable",
    when in fact the sandbox is fine and only ``git push`` is affected."""
    monkeypatch.setenv("HOME", str(tmp_path / "does" / "not" / "exist"))
    status = sandbox_main.configure_git_credential("ghs_secret")
    assert status.startswith("failed: ")
    assert "ghs_secret" not in status, "a failure message must not carry the credential"


def test_the_umask_is_restored(sandbox_main, home):
    """It is set to 077 to close the window between ``open`` and ``chmod``; leaving it set would
    change the permissions of everything this long-lived server process creates afterwards."""
    before = os.umask(0o022)
    os.umask(before)
    sandbox_main.configure_git_credential("ghs_secret")
    after = os.umask(0o022)
    os.umask(after)
    assert after == before


def test_the_wake_answers_with_the_credential_status_and_not_the_credential(sandbox_main, home, monkeypatch):
    monkeypatch.setattr(sandbox_main, "WORKSPACE", str(home / "workspace"))
    response = sandbox_main.invoke({"action": "ping", "gitCredential": "ghs_secret"})
    assert response["gitCredentialStatus"] == "written"
    assert "ghs_secret" not in repr(response), "the response must not echo the credential back"
    assert response["status"] == "ready"


def test_a_wake_without_a_credential_says_nothing_about_one(sandbox_main, home, monkeypatch):
    """Absence must be distinguishable from failure — the harness only warns when it *sent* one."""
    monkeypatch.setattr(sandbox_main, "WORKSPACE", str(home / "workspace"))
    response = sandbox_main.invoke({"action": "ping"})
    assert "gitCredentialStatus" not in response
    assert response["workspaceReady"] is True


@pytest.mark.parametrize("value", [None, "", 0, [], {"token": "x"}])
def test_a_non_credential_value_is_ignored(sandbox_main, home, monkeypatch, value):
    """Payloads come from the network. Anything that is not a non-empty string is not a credential."""
    monkeypatch.setattr(sandbox_main, "WORKSPACE", str(home / "workspace"))
    response = sandbox_main.invoke({"gitCredential": value})
    assert "gitCredentialStatus" not in response
    assert not (home / ".git-credentials").exists()


def test_an_unusable_workspace_is_reported_as_degraded(sandbox_main, monkeypatch, tmp_path):
    """The first-deploy check that no local test can make: a service-mounted volume the image's
    non-root user cannot write. It has to announce itself, or it surfaces later as a bare
    "cd: permission denied" on the agent's first command."""
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    monkeypatch.setattr(sandbox_main, "WORKSPACE", str(blocker / "workspace"))
    response = sandbox_main.invoke({"action": "ping"})
    assert response["status"] == "degraded"
    assert "Error" in response["error"] or "error" in response


def test_the_status_names_missing_tools(sandbox_main, home, monkeypatch):
    """``ping`` is the sandbox's self-description: a missing ``git`` should be one clear line at
    deploy time, not a "command not found" mid-turn."""
    monkeypatch.setattr(sandbox_main, "WORKSPACE", str(home / "workspace"))
    monkeypatch.setattr(sandbox_main.shutil, "which", lambda name: None if name == "git" else "/usr/bin/" + name)
    response = sandbox_main.invoke({"action": "ping"})
    assert response["missingTools"] == ["git"]
    assert response["tools"]["git"] is False


def test_ping_reports_the_workspace_volume_size(sandbox_main, tmp_path, monkeypatch):
    """The wake response carries the EBS volume's real size, so the harness can word its storage
    advice with it and the first wake after a deploy shows the ~40 GiB volume is attached."""
    monkeypatch.setattr(sandbox_main, "WORKSPACE", str(tmp_path))
    status = sandbox_main.workspace_status()
    assert status["workspaceCapacityBytes"] > 0
    assert 0 <= status["workspaceFreeBytes"] <= status["workspaceCapacityBytes"]


def test_an_unreadable_workspace_reports_no_size(sandbox_main, tmp_path):
    assert sandbox_main.workspace_capacity(str(tmp_path / "missing")) == {}
