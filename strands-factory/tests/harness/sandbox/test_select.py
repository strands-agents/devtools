"""Tests for :func:`~strandly.sandbox.select.build_sandbox` — the backend choice + its wiring.

Small file, but two of these assertions are security assertions, and one of them is the reason the
``STRANDLY_SANDBOX_GIT_TOKEN`` key exists at all.
"""

from __future__ import annotations

import pytest

from strandly.core.config import Config
from strandly.core.context import RuntimeContext
from strandly.sandbox.runtime import RuntimeSandbox, sandbox_session_id
from strandly.sandbox.select import SandboxNotConfiguredError, build_sandbox

ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/strandly_sandbox-abc"


def test_no_sandbox_runtime_refuses_to_build():
    """Without a sandbox runtime the model's shell would run beside the agent's secrets."""
    with pytest.raises(SandboxNotConfiguredError) as excinfo:
        build_sandbox(Config(values={}))
    assert "STRANDLY_SANDBOX_RUNTIME_ARN" in str(excinfo.value)


def test_a_configured_arn_selects_the_sandbox_runtime():
    sandbox = build_sandbox(Config(values={"STRANDLY_SANDBOX_RUNTIME_ARN": ARN}))
    assert isinstance(sandbox, RuntimeSandbox)
    assert sandbox.runtime_arn == ARN


def test_the_session_key_drives_the_sandbox_session():
    """The sandbox filesystem must follow the same boundary as the conversation.

    ``session_key`` is what identifies "this conversation" everywhere else in the harness
    (``serve.cache.session_key``), so deriving the sandbox session from it is what makes a follow-up
    comment on an issue land in the same workspace as the first one.
    """
    config = Config(values={"STRANDLY_SANDBOX_RUNTIME_ARN": ARN})
    ctx = RuntimeContext(session_key="gh-repo-issue-42", session_id="ignored-when-key-is-set")
    assert build_sandbox(config, ctx).session_id == sandbox_session_id("gh-repo-issue-42")


def test_the_session_id_is_used_when_there_is_no_key():
    """Some ingresses set only ``session_id``; the sandbox must still be stable for them."""
    config = Config(values={"STRANDLY_SANDBOX_RUNTIME_ARN": ARN})
    ctx = RuntimeContext(session_id="gh-repo-issue-7")
    assert build_sandbox(config, ctx).session_id == sandbox_session_id("gh-repo-issue-7")


def test_a_sessionless_context_gets_its_own_sandbox_session():
    """Two sessionless runs must not share a workspace (or each other's half-finished clones)."""
    config = Config(values={"STRANDLY_SANDBOX_RUNTIME_ARN": ARN})
    first = build_sandbox(config, RuntimeContext())
    second = build_sandbox(config, RuntimeContext())
    assert first.session_id != second.session_id  # type: ignore[union-attr]


def test_the_scoped_sandbox_git_token_is_the_one_that_goes_in():
    """Only ``STRANDLY_SANDBOX_GIT_TOKEN`` reaches the sandbox — scoped to ``Contents``."""
    config = Config(
        values={"STRANDLY_SANDBOX_RUNTIME_ARN": ARN, "STRANDLY_SANDBOX_GIT_TOKEN": "ghs_scoped"}
    )
    sandbox = build_sandbox(config)
    assert sandbox._github_token == "ghs_scoped"  # type: ignore[union-attr]


def test_the_use_github_token_never_leaks_into_the_sandbox():
    """SECURITY: the agent's GitHub token must never be readable by code the model runs.

    Anything running in the sandbox can read the git credential (``git credential fill`` was observed
    doing exactly that in production). The ``use_github`` token can comment, review and approve as the
    agent; the sandbox's own token cannot. A fallback from one to the other would silently restore the
    exposure this split exists to remove, so it is asserted rather than assumed.
    """
    config = Config(values={"STRANDLY_SANDBOX_RUNTIME_ARN": ARN, "STRANDLY_GITHUB_TOKEN": "ghp_full"})
    sandbox = build_sandbox(config)
    assert sandbox._github_token is None  # type: ignore[union-attr]
