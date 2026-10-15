"""Unit tests for system-prompt composition (`core.prompt.compose`)."""

from __future__ import annotations

from strandly_harness.core.prompt.compose import compose, global_prompt

_EPHEMERAL_MARK = "Your sandbox is ephemeral"


def test_global_prompt_always_present():
    assert "You are Strandly" in compose()
    assert compose().startswith(global_prompt())


def test_ephemeral_sandbox_block_gated_on_flag():
    # Off by default (local sandbox = the user's real disk, which persists).
    assert _EPHEMERAL_MARK not in compose(tool_names=["bash", "file_editor"])
    assert _EPHEMERAL_MARK not in compose(tool_names=["bash"], ephemeral_sandbox=False)
    # On only when the sandbox is the non-local (AgentCore) one.
    out = compose(tool_names=["bash", "file_editor"], ephemeral_sandbox=True)
    assert _EPHEMERAL_MARK in out
    # It carries the load-bearing guidance: persist out of the sandbox + resume from the remote.
    assert "does not persist across separate invocations" in out
    assert "WIP branch" in out


def test_ephemeral_sandbox_block_independent_of_tools():
    # It's a sandbox property, not a tool capability — present even with no optional tools, and
    # it is NOT part of the tool-keyed capabilities section.
    assert _EPHEMERAL_MARK in compose(ephemeral_sandbox=True)
    # Without the capabilities section (tool_names=None) it can still appear (sandbox is separate).
    assert _EPHEMERAL_MARK in compose(tool_names=None, ephemeral_sandbox=True)


def test_layer_order_role_last():
    # A subagent role layer stays last, after global + the ephemeral block.
    out = compose("You are a strict reviewer.", tool_names=["bash"], ephemeral_sandbox=True)
    assert out.index("You are Strandly") < out.index(_EPHEMERAL_MARK) < out.index("strict reviewer")
