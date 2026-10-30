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


def test_create_agent_wires_harness_defaults_for_ci(s3):
    model = BedrockModel(model_id="global.anthropic.claude-opus-4-8", region_name="us-west-2")
    tools = agent_runner._get_tools_for_mode("reviewer")

    agent = agent_runner._create_agent(model, "SOP TEXT", tools, "reviewer-1", BUCKET, PREFIX, {"session.id": "x"})

    assert agent.model is model
    assert agent.system_prompt.startswith(HARNESS_CONTRACT)
    assert "\n\nSOP TEXT\n\n" in agent.system_prompt
    assert f"Working directory: {os.getcwd()}" in agent.system_prompt
    assert agent.trace_attributes == {"session.id": "x"}
    # Harness built-ins replace the local file editor; GitHub tools and the mode denylist still apply.
    assert {"shell", "read", "write", "edit", "retrieve_context", "http_request", "get_pr_files"} <= set(agent.tool_names)
    assert {"create_issue", "create_pull_request", "web_fetch", "subagent", "todo_write"}.isdisjoint(agent.tool_names)
    # Nothing from the untrusted checkout is loaded or injected (no skills, memory, or environment plugin).
    assert not any(name.startswith("strands:environment") for name in agent._plugin_registry._plugins)
    # The old plan's failure mode: a context manager paired with a per-message session manager.
    assert agent.context_manager is not None
    assert isinstance(agent._session_manager, SnapshotSessionManager)
    assert isinstance(agent.storage, S3Storage)
    assert agent.context_manager.stash_is_durable


def test_offloaded_tool_results_survive_a_resumed_session(s3):
    """A second process on the same session id restores the conversation and can still read
    the tool results the context manager offloaded to S3 in the first one."""
    first_run = ScriptedModel([
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "big_diff", "input": {}}}]},
        {"role": "assistant", "content": [{"text": "reviewed"}]},
    ])
    agent = agent_runner._create_agent(first_run, "SOP", [big_diff], "reviewer-7", BUCKET, PREFIX)
    agent("review PR 7")

    tool_result = agent.messages[2]["content"][0]["toolResult"]
    offloaded = json.dumps(tool_result)
    assert "line 1000 of" not in offloaded, "the oversized result should have been truncated to a preview"
    reference = re.search(r"\[ref: ([^\]]+)\]", offloaded).group(1)
    keys = _keys(s3)
    assert any(k.startswith(f"{PREFIX}/session/reviewer-7/") for k in keys)
    assert any(k.startswith(f"{PREFIX}/context/") for k in keys)

    resumed = agent_runner._create_agent(ScriptedModel([]), "SOP", [big_diff], "reviewer-7", BUCKET, PREFIX)

    assert resumed.messages == agent.messages
    retrieved = resumed.tool.retrieve_context(reference=reference, pattern="line 1000 of", record_direct_tool_call=False)
    assert "line 1000 of" in json.dumps(retrieved["content"])
