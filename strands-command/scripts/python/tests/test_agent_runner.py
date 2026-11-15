"""Tests for the harness wiring in agent_runner."""
import json
import os
import re
from collections.abc import AsyncGenerator
from typing import Any

import boto3
import pytest
from moto import mock_aws
from strands import tool
from strands.models import BedrockModel, Model
from strands.session import SnapshotSessionManager
from strands.storage import S3Storage
from strands.vended_plugins.skills import AgentSkills, Skill
from strands_harness import defaults
from strands_harness.prompt import HARNESS_CONTRACT

from .. import agent_runner

BUCKET = "sessions-bucket"
PREFIX = "owner/repo"


class ScriptedModel(Model):
    """Replays scripted assistant messages, one per model call."""

    def __init__(self, responses: list[dict]):
        self.responses = responses

    def get_config(self) -> Any:
        return {}

    def update_config(self, **model_config: Any) -> None:
        pass

    async def structured_output(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        yield {}

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        message = self.responses.pop(0)
        yield {"messageStart": {"role": "assistant"}}
        stop_reason = "end_turn"
        for content in message["content"]:
            if "text" in content:
                yield {"contentBlockStart": {"start": {}}}
                yield {"contentBlockDelta": {"delta": {"text": content["text"]}}}
            else:
                stop_reason = "tool_use"
                use = content["toolUse"]
                yield {"contentBlockStart": {"start": {"toolUse": {"name": use["name"], "toolUseId": use["toolUseId"]}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(use["input"])}}}}
            yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": stop_reason}}


@tool
def big_diff() -> str:
    """Return an oversized tool result."""
    return "\n".join(f"+line {i} of a very large pull request diff" for i in range(2000))


@pytest.fixture
def s3():
    os.environ.update(AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test", AWS_DEFAULT_REGION="us-west-2")
    with mock_aws():
        client = boto3.client("s3", region_name="us-west-2")
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
        yield client


def _keys(s3) -> list[str]:
    return [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])]



def test_create_agent_wires_harness_for_ci(s3):
    tools = agent_runner._get_tools_for_mode("reviewer")

    agent = agent_runner._create_agent(
        None, "auto", "task-reviewer", "REPO EXTRA", tools, "reviewer-1", BUCKET, PREFIX, {"session.id": "x"}
    )

    # No model given: the harness default model and its recommended effort.
    assert isinstance(agent.model, BedrockModel)
    assert agent.model.config["model_id"] == defaults.DEFAULT_MODEL.removeprefix("bedrock/")
    assert agent.system_prompt.startswith(HARNESS_CONTRACT)
    assert "`task-reviewer` skill" in agent.system_prompt
    assert agent.system_prompt.endswith("\n\nREPO EXTRA")
    assert f"Working directory: {os.getcwd()}" in agent.system_prompt
    assert agent.trace_attributes == {"session.id": "x"}
    # Only the mode's skill is offered, read from the devtools copy rather than the checkout.
    skills = next(p for p in agent._plugin_registry._plugins.values() if isinstance(p, AgentSkills))
    assert [s.name for s in skills.get_available_skills(agent)] == ["task-reviewer"]
    # The harness's default built-in tools, plus the GitHub tools minus the mode's denylist.
    assert {
        "shell", "read", "write", "edit", "web_fetch", "programmatic_tool_caller", "subagent",
        "skills", "retrieve_context", "http_request", "get_pr_files",
    } <= set(agent.tool_names)
    assert {"create_issue", "create_pull_request", "todo_write"}.isdisjoint(agent.tool_names)
    assert not any(name.startswith("strands:environment") for name in agent._plugin_registry._plugins)
    assert isinstance(agent._session_manager, SnapshotSessionManager)
    assert isinstance(agent.storage, S3Storage)
    assert agent.context_manager.stash_is_durable


def test_create_agent_passes_model_and_effort_overrides(s3):
    agent = agent_runner._create_agent(
        "us.anthropic.claude-sonnet-4-6", "low", "task-refiner", "", [], "refiner-1", BUCKET, PREFIX
    )

    assert agent.model.config["model_id"] == "us.anthropic.claude-sonnet-4-6"
    assert agent.model.config["additional_request_fields"]["output_config"] == {"effort": "low"}


def test_create_agent_rejects_unknown_skill(s3):
    with pytest.raises(ValueError, match="Unknown skill 'task-nope'"):
        agent_runner._create_agent(None, "auto", "task-nope", "", [], "x-1", BUCKET, PREFIX)


@pytest.mark.parametrize("mode", ["implementer", "refiner", "reviewer", "release-notes", "bug-verifier"])
def test_every_mode_has_a_valid_skill(mode):
    skill = Skill.from_file(agent_runner.SKILLS_DIR / f"task-{mode}", strict=True)
    assert skill.name == f"task-{mode}"
    assert skill.instructions.lstrip().startswith("# ")


def test_resolve_model_config_defaults_to_harness(monkeypatch):
    monkeypatch.setenv("STRANDS_MODEL_ID", " ")
    monkeypatch.delenv("STRANDS_REASONING_EFFORT", raising=False)
    assert agent_runner._resolve_model_config() == (None, "auto")
    monkeypatch.setenv("STRANDS_MODEL_ID", "m")
    monkeypatch.setenv("STRANDS_REASONING_EFFORT", "max")
    assert agent_runner._resolve_model_config() == ("m", "max")


def test_offloaded_tool_results_survive_a_resumed_session(s3):
    """A second process on the same session id restores the conversation and can still read
    the tool results the context manager offloaded to S3 in the first one."""
    first_run = ScriptedModel([
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "big_diff", "input": {}}}]},
        {"role": "assistant", "content": [{"text": "reviewed"}]},
    ])
    agent = agent_runner._create_agent(first_run, "auto", "task-reviewer", "", [big_diff], "reviewer-7", BUCKET, PREFIX)
    agent("review PR 7")

    tool_result = agent.messages[2]["content"][0]["toolResult"]
    offloaded = json.dumps(tool_result)
    assert "line 1000 of" not in offloaded, "the oversized result should have been truncated to a preview"
    reference = re.search(r"\[ref: ([^\]]+)\]", offloaded).group(1)
    keys = _keys(s3)
    assert any(k.startswith(f"{PREFIX}/session/reviewer-7/") for k in keys)
    assert any(k.startswith(f"{PREFIX}/context/") for k in keys)

    resumed = agent_runner._create_agent(
        ScriptedModel([]), "auto", "task-reviewer", "", [big_diff], "reviewer-7", BUCKET, PREFIX
    )

    assert resumed.messages == agent.messages
    retrieved = resumed.tool.retrieve_context(reference=reference, pattern="line 1000 of", record_direct_tool_call=False)
    assert "line 1000 of" in json.dumps(retrieved["content"])


def test_activated_skill_reaches_the_model_whole_across_turns(s3):
    """The SOP arrives as the skills tool result; the context manager must not cut it to a preview."""
    sop_lines = [l for l in (agent_runner.SKILLS_DIR / "task-implementer" / "SKILL.md").read_text().splitlines() if l.strip()]
    sop_middle = sop_lines[len(sop_lines) // 2]  # a head-and-tail preview drops the middle
    model = ScriptedModel([
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "s1", "name": "skills", "input": {"skill_name": "task-implementer"}}}]},
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "big_diff", "input": {}}}]},
        {"role": "assistant", "content": [{"text": "done"}]},
    ])
    agent = agent_runner._create_agent(model, "auto", "task-implementer", "", [big_diff], "implementer-1", BUCKET, PREFIX)
    agent("implement issue 1")

    assert json.dumps(sop_middle)[1:-1] in json.dumps(agent.messages[2]), "the skill result was truncated"
    assert "line 1000 of" not in json.dumps(agent.messages[4]), "other large results are still offloaded"


def test_resumed_session_uses_this_runs_instructions(s3):
    first = agent_runner._create_agent(
        ScriptedModel([{"role": "assistant", "content": [{"text": "ok"}]}]),
        "auto", "task-reviewer", "SUFFIX-OLD", [], "reviewer-9", BUCKET, PREFIX,
    )
    first("review PR 9")

    resumed = agent_runner._create_agent(ScriptedModel([]), "auto", "task-reviewer", "SUFFIX-NEW", [], "reviewer-9", BUCKET, PREFIX)

    assert resumed.messages == first.messages
    assert resumed.system_prompt.endswith("\n\nSUFFIX-NEW")
    assert "SUFFIX-OLD" not in resumed.system_prompt
