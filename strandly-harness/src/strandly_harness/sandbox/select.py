"""Sandbox selection — opinionated: AgentCore Code Interpreter if configured, else local.

The same ``strands.sandbox.Sandbox`` is injected into every file/exec tool, so all execution and
file access share one isolation boundary. ``local`` is named ``NotASandbox...`` upstream so "no
isolation" is never silent. ``agentcore`` is used only when ``AGENTCORE_CODE_INTERPRETER_ID`` is
configured (needs the ``agentcore`` extra) and shares the harness's AWS region.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from strandly_harness.core.config import Config

if TYPE_CHECKING:
    from strands.sandbox.base import Sandbox


def build_sandbox(config: Config) -> Sandbox:
    if config.use_agentcore_sandbox:
        from strandly_harness.core.constants import SANDBOX_SESSION_TIMEOUT_SECONDS
        from strandly_harness.sandbox.agentcore import (
            DEFAULT_IDENTIFIER,
            AgentCoreSandbox,
        )

        return AgentCoreSandbox(
            region=config.aws_region,
            identifier=config.code_interpreter_id or DEFAULT_IDENTIFIER,
            # Pin the session lifetime to the service max (8h) instead of the 900s default, so a
            # long autonomous run (explore → write → test → push a PR) isn't reaped mid-task — the
            # default cut a real run off at ~910s. Does not persist the FS across invokes.
            session_timeout_seconds=SANDBOX_SESSION_TIMEOUT_SECONDS,
            # Same token as the `use_github` tool: bootstraps the sandbox git client so native
            # `git clone`/`push` authenticates as the same identity as the tool's API writes.
            # (Interim: the plan is short-lived GitHub App installation tokens; the bootstrap is
            # token-type agnostic so only this value changes.)
            github_token=config.github_token,
        )
    from strands.sandbox.not_a_sandbox_local_environment import NotASandboxLocalEnvironment

    return NotASandboxLocalEnvironment()


__all__ = ["build_sandbox"]
