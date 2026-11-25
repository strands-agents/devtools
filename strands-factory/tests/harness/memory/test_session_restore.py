"""A session is restored from its snapshot alone; AgentCore Memory is never read to rebuild one.

The Memory side is the real ``bedrock_agentcore`` ``MemoryClient`` whose data-plane client is a
real botocore ``bedrock-agentcore`` client under ``Stubber`` with nothing queued, so any ``ListEvents``
raises; the store side is the SDK's real ``S3Storage`` over ``fake_s3.FakeS3Client``.
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.config import Config as BotocoreConfig
from botocore.stub import Stubber
from strands import Agent

from fake_agentcore import MEMORY_ID as RECORDER_MEMORY_ID
from fake_agentcore import FakeAgentCoreClient
from fake_s3 import BUCKET, FakeS3Client, s3_storage
from scripted_model import ScriptedModel, text_events
from strandly.core.config import Config
from strandly.core.context import RuntimeContext
from strandly.core.session_store_reader import snapshot_key
from strandly.memory.session import build_session_manager
from strandly.memory.session_store import FailClosedStorage
from strandly.memory.transcript import TranscriptRecorder

CONFIG = Config(values={"AGENTCORE_MEMORY_ID": "strandly_dev_memory-abc123", "AWS_REGION": "us-east-1"})


@pytest.fixture
def memory(monkeypatch) -> Stubber:
    """Any ``MemoryClient`` built during the test talks to a stub with no responses queued."""
    import bedrock_agentcore.memory as memory_pkg

    client = memory_pkg.MemoryClient(region_name="us-east-1")
    client.gmdp_client = boto3.client(
        "bedrock-agentcore", region_name="us-east-1", aws_access_key_id="testing",
        aws_secret_access_key="testing", config=BotocoreConfig(retries={"max_attempts": 1}),
    )
    monkeypatch.setattr(memory_pkg, "MemoryClient", lambda **kw: client)
    stub = Stubber(client.gmdp_client)
    stub.activate()
    yield stub
    stub.deactivate()


def _agent(fake: FakeS3Client, sid: str, model: Any, recorder_memory: Any = None) -> Agent:
    storage = FailClosedStorage(s3_storage(fake), backend=f"s3://{BUCKET}")
    mgr = build_session_manager(CONFIG, RuntimeContext(session_id=sid), storage)
    recorder = TranscriptRecorder(
        recorder_memory if recorder_memory is not None else FakeAgentCoreClient(),
        memory_id=RECORDER_MEMORY_ID, actor_id="team", session_id=sid, storage=storage,
    )
    return Agent(model=model, session_manager=mgr, storage=storage, context_manager="auto",
                 plugins=[recorder], callback_handler=None)


def test_a_new_session_starts_empty_and_is_restored_from_its_snapshot(memory):
    sid = "gh-o-r-issue-12"
    fake = FakeS3Client()
    agent = _agent(fake, sid, ScriptedModel(text_events("hi")))
    assert agent.messages == []
    assert snapshot_key(sid) not in fake.objects  # nothing seeded or saved before the first turn

    agent("hello")
    rebuilt = _agent(fake, sid, ScriptedModel())
    assert [m["role"] for m in rebuilt.messages] == ["user", "assistant"]
    memory.assert_no_pending_responses()

