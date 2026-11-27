"""Unit tests for system-prompt composition (`core.prompt.compose`)."""

from __future__ import annotations

from strandly.core.prompt.compose import compose, global_prompt

_EPHEMERAL_MARK = "Your sandbox is ephemeral"


def test_global_prompt_first_without_the_harness_contract():
    # The harness prepends its contract to `instructions`; compose must not repeat it.
    from strands_harness import HARNESS_CONTRACT

    out = compose()
    assert out.startswith(global_prompt())
    assert HARNESS_CONTRACT[:40] not in out


def test_ephemeral_sandbox_block_always_present():
    out = compose()
    assert _EPHEMERAL_MARK in out
    assert "does not persist across separate invocations" in out
    assert "WIP branch" in out
    # No memory write tool: durable facts go in the final reply, which extraction learns from.
    assert "durable facts plainly in your final reply" in out
    assert "record durable facts in long-term memory" not in out


def test_layer_order_role_last():
    # A subagent role layer stays last, after global + the ephemeral block.
    out = compose("You are a strict reviewer.", tool_names=["shell"])
    assert out.index("You are Strandly") < out.index(_EPHEMERAL_MARK) < out.index("strict reviewer")


def test_github_identity_gated_on_login():
    # Absent by default — the block is emitted only when a login actually resolved (never guess).
    assert "Your GitHub identity" not in compose()
    assert "Your GitHub identity" not in compose(tool_names=["use_github"])
    out = compose(tool_names=["use_github"], github_login="example-agent")
    assert "## Your GitHub identity" in out
    assert "`example-agent`" in out
    # The load-bearing guidance: don't @-mention yourself; recognize your own past comments.
    assert "never @-mention yourself" in out
    assert "your own earlier words" in out


def test_github_identity_placement_and_independence():
    # Right after the global prompt, before capabilities — and independent of the tool set (it's
    # an account property, not a tool capability).
    out = compose(tool_names=["use_github"], github_login="bot")
    assert (
        out.index("You are Strandly")
        < out.index("## Your GitHub identity")
        < out.index("## Capabilities")
    )
    assert "## Your GitHub identity" in compose(github_login="bot")  # no tool_names at all


def test_use_github_block_requires_pr_owner_line():
    # A PR opened under the bot login hides who asked for it. The use_github block demands a
    # leading "Requested by" line and an explicit no-human marker for scheduled/automated runs.
    out = compose(tool_names=["use_github"])
    assert "**Requested by:** @login" in out
    assert "no human requester" in out
    assert "Requested by" not in compose(tool_names=["shell"])


def test_use_github_block_keeps_requester_in_the_loop():
    out = compose(tool_names=["use_github"])
    assert "request their review" in out
    assert "assign them to the issue" in out
    assert "add them as a reviewer" in out
    assert "Keep the requester in the loop" not in compose(tool_names=["shell"])


def test_use_slack_capability_gated_on_tool_presence():
    with_slack = compose(tool_names=["shell", "use_slack"])
    without = compose(tool_names=["shell"])
    assert "use_slack" in with_slack
    assert "use_slack" not in without


def test_programmatic_tool_caller_capability_gated_on_tool_presence():
    # The harness's programmatic_tool_caller flows in with its default builtins (opt-out, see
    # build_agent); the prompt must describe it only when a scope actually kept it.
    with_ptc = compose(tool_names=["shell", "programmatic_tool_caller"])
    without = compose(tool_names=["shell"])
    assert "`programmatic_tool_caller`" in with_ptc
    assert "programmatic_tool_caller" not in without


def test_memory_guidance_says_memory_is_learned_from_the_reply():
    # There is no write tool: the agent is told to state what should be remembered.
    prompt = compose(tool_names=["shell", "search_memory"])
    assert "final reply** (the message with no tool calls)" in prompt
    assert "only if your final reply restates them" in prompt
    assert "Your conversation is not remembered" not in prompt
    assert "add_memory" not in prompt and "add_personal_memory" not in prompt
    assert "Long-term memory" not in compose(tool_names=["shell"])


def test_subagent_memory_guidance_points_at_its_final_result():
    # A subagent's conversation is never recorded, so it must not be told it is remembered.
    prompt = compose(tool_names=["shell", "search_memory"], spawn_depth=1)
    assert "**Search first**" in prompt
    assert "Your conversation is not remembered" in prompt
    assert "durable findings" in prompt and "final result" in prompt
    assert "Memory is learned automatically" not in prompt


def test_prompt_has_no_upload_store():
    out = compose(tool_names=["shell"])
    assert "Artifactory" not in out
    assert "aws s3 cp" not in out


def test_scheduling_block_only_with_the_schedule_tool():
    assert "**Scheduling**" not in compose(tool_names=["shell", "use_github"])
    out = compose(tool_names=["schedule"])
    block = out[out.index("**Scheduling**") :]
    for phrase in ("ISO date-time", "cron(...)", "`timezone`", "confirm", "read-only"):
        assert phrase in block


def test_home_channel_block_on_a_non_slack_run():
    out = compose(tool_names=["use_slack"], slack_reply=False, slack_home_channel="C0HOME")
    assert "Slack home channel" in out and "`C0HOME`" in out
    assert "goes only through `notify_team`" in out
    assert "invoked from Slack (a DM" not in out
    assert "Notifying maintainers" not in out


def test_notify_block_carries_the_anti_spam_guidance():
    out = compose(tool_names=["use_slack", "notify_team"], slack_reply=False, slack_home_channel="C0HOME")
    assert "Notifying maintainers" in out
    for phrase in ("need to know or act", "pass its `thread_ts`", "Batch minor updates", "no progress updates"):
        assert phrase in out


def test_home_channel_block_adds_to_the_slack_reply_block():
    out = compose(tool_names=["use_slack"], slack_reply=True, slack_home_channel="C0HOME")
    assert "invoked from Slack (a DM" in out and "Slack home channel" in out


def test_no_home_channel_block_without_a_home_channel():
    out = compose(tool_names=["use_slack"], slack_reply=True)
    assert "invoked from Slack (a DM" in out and "Slack home channel" not in out
