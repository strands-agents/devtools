"""The dashboard's transcript read through a REAL botocore parser — the SDK-version failure mode.

The ReadApi Lambda once ran on the Lambda runtime's bundled botocore, which predates the
``json`` member of bedrock-agentcore's ``PayloadType`` union: ``ListEvents`` handed back every
tool-call / tool-result message as ``{"SDK_UNKNOWN_MEMBER": {"name": "json"}}`` and the dashboard
silently showed conversations without a single tool call. The fix ships the ``uv.lock``-pinned SDK
as a layer (infra/stacks/lambda_sdk.py). These tests pin both halves of that:

* the pinned botocore (this venv's — the layer installs the same ``uv.lock`` pins, asserted below)
  parses recorder-written events into tool / result blocks on both ``/api/sessions/{id}`` paths;
* an SDK without the member yields unknown members, and the handler says so in ONE warning per
  invocation instead of dropping them silently.

The fake AgentCore client's ``ListEvents`` output is serialized to the wire (rest-json) and parsed
back with botocore's own parser, so the shapes are exactly what a live client returns.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import importlib.util
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import botocore
import botocore.session
import pytest
from botocore.model import ServiceModel
from botocore.parsers import create_parser

from fake_agentcore import MEMORY_ID, FakeAgentCoreClient
from fake_s3 import BUCKET, FakeS3Client
from strandly.memory.transcript import TranscriptRecorder

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "dashboard" / "api"))
handler = importlib.import_module("handler")


def _lambda_sdk() -> Any:
    """infra/stacks/lambda_sdk.py — stdlib-only, so loadable without the CDK venv."""
    spec = importlib.util.spec_from_file_location("_lambda_sdk", _REPO / "infra" / "stacks" / "lambda_sdk.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raw_model() -> dict[str, Any]:
    loader = botocore.session.get_session().get_component("data_loader")
    return copy.deepcopy(loader.load_service_model("bedrock-agentcore", "service-2"))


def _old_sdk_model() -> ServiceModel:
    """The service model as an SDK that predates ``PayloadType.json`` sees it."""
    raw = _raw_model()
    del raw["shapes"]["PayloadType"]["members"]["json"]
    return ServiceModel(raw, "bedrock-agentcore")


class _WireClient:
    """``list_events`` = the fake's response → rest-json wire bytes → botocore's parser."""

    def __init__(self, fake: FakeAgentCoreClient, model: ServiceModel) -> None:
        self._fake = fake
        self._op = model.operation_model("ListEvents")
        self._parser = create_parser(model.metadata["protocol"])

    def list_events(self, **params: Any) -> dict[str, Any]:
        resp = self._fake.list_events(**params)

        def wire(value: Any) -> Any:
            return value.timestamp() if isinstance(value, datetime) else str(value)

        body = json.dumps({k: v for k, v in resp.items() if k != "ResponseMetadata"}, default=wire)
        parsed = self._parser.parse(
            {"status_code": 200, "headers": {}, "body": body.encode()}, self._op.output_shape
        )
        parsed.pop("ResponseMetadata", None)
        return parsed


class _Added:
    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message


class _Store:
    def __init__(self, fake: FakeS3Client) -> None:
        self.fake = fake

    async def write(self, key: str, data: bytes) -> None:
        self.fake.put(key, data)


SID = "gh-o-r-issue-7"
MESSAGES = [
    {"role": "user", "content": [{"text": "what is x?"}]},
    {"role": "assistant", "content": [{"text": "checking"}, {"toolUse": {"toolUseId": "t1", "name": "lookup", "input": {"q": "x"}}}]},
    {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "status": "success", "content": [{"text": "x=1"}]}}]},
    {"role": "assistant", "content": [{"text": "x is 1"}]},
]


def _reader(model: ServiceModel) -> Any:
    memory, s3 = FakeAgentCoreClient(), FakeS3Client()
    recorder = TranscriptRecorder(memory, memory_id=MEMORY_ID, actor_id="team", session_id=SID, storage=_Store(s3))
    for message in MESSAGES:
        asyncio.run(recorder._on_message_added(_Added(message)))  # type: ignore[arg-type]
    mem = handler.MemoryReader(MEMORY_ID, "us-west-2", client=_WireClient(memory, model))
    return handler.SessionStoreReader(BUCKET, client=s3, fallback=mem)


def _kinds(messages: list[dict[str, Any]]) -> list[list[str]]:
    return [[b["kind"] for b in m.get("blocks") or []] for m in messages]


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="infra/stacks/lambda_sdk.py reads uv.lock with tomllib (3.11+); the CDK app builds on 3.12",
)
def test_the_layer_pins_are_this_venvs_sdk():
    """The layer installs boto3's closure at its uv.lock pins — the same SDK the root venv runs,
    which is what makes the parsing tests below stand for the deployed Lambda."""
    pins = dict(
        line.split(" ", 1)[0].split("==") for line in _lambda_sdk().lock_requirements().splitlines()
    )
    assert pins["botocore"] == botocore.__version__
    assert {"boto3", "botocore", "jmespath", "s3transfer", "urllib3"} <= set(pins)


def test_the_pinned_botocore_knows_payload_type_json():
    """Guard: the SDK the dashboard Lambda ships can read the recorder's ``json`` payload items."""
    model = botocore.session.get_session().get_service_model("bedrock-agentcore")
    assert "json" in model.shape_for("PayloadType").members


def test_tool_messages_survive_a_real_botocore_parse_on_both_session_paths():
    model = botocore.session.get_session().get_service_model("bedrock-agentcore")
    reader = _reader(model)
    page, _ = reader.pagination_transcript(SID)  # GET /api/sessions/{id}?limit=…
    full = reader.transcript(SID)  # GET /api/sessions/{id}
    for messages in (page, full):
        assert _kinds(messages) == [["text"], ["text", "tool"], ["result"], ["text"]]
        assert messages[1]["blocks"][1]["name"] == "lookup"
    assert handler._report_unknown_payload_members() == {}


def test_an_sdk_without_the_json_member_is_reported_not_silent(caplog):
    reader = _reader(_old_sdk_model())
    full = reader.transcript(SID)
    # The failure being guarded: only the plain-text messages survive an old SDK.
    assert _kinds(full) == [["text"], ["text"]]
    with caplog.at_level(logging.WARNING, logger=handler.logger.name):
        assert handler._report_unknown_payload_members() == {"json": 2}
    warnings = [r for r in caplog.records if "SDK_UNKNOWN_MEMBER" in r.getMessage()]
    assert len(warnings) == 1 and "json=2" in warnings[0].getMessage()
    assert handler._report_unknown_payload_members() == {}  # reset for the next invocation


def test_the_unknown_member_warning_fires_once_per_invocation(monkeypatch, caplog):
    events = [{"payload": [{"SDK_UNKNOWN_MEMBER": {"name": "json"}}]}, {"payload": [{"conversational": {}}]}]

    def fake_route(*_args: Any) -> tuple[int, dict[str, Any]]:
        for _ in range(3):  # three ListEvents pages in one request
            handler._count_unknown_payload_members(events)
        return 200, {}

    monkeypatch.setattr(handler, "route", fake_route)
    with caplog.at_level(logging.WARNING, logger=handler.logger.name):
        handler.lambda_handler({"rawPath": "/api/sessions/x"})
        handler.lambda_handler({"rawPath": "/api/sessions/x"})
    warnings = [r.getMessage() for r in caplog.records if "SDK_UNKNOWN_MEMBER" in r.getMessage()]
    assert len(warnings) == 2  # one per invocation, not one per page or item
    assert all("json=3" in w for w in warnings)
