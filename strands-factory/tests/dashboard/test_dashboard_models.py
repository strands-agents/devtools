"""Choosing the model per chat: POST /api/chat checks type and size, the runtime decides."""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_dashboard_api import FakeReader, _evt, handler

OPUS = "bedrock/global.anthropic.claude-opus-5-5"


class _Capture(handler.RuntimeInvoker):
    """The real invoker's payload building, with the runtime call captured and its answer canned."""

    def __init__(self, answer: dict[str, Any] | None = None) -> None:
        super().__init__("arn:aws:bedrock-agentcore:us-west-2:111111111111:runtime/example")
        self.payloads: list[dict[str, Any]] = []
        self.answer = answer or {"status": "accepted", "taskId": "deadbeef"}

    def _invoke(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return self.answer


def _post(body: dict[str, Any]) -> dict[str, Any]:
    event = _evt("/api/chat", method="POST")
    event["body"] = json.dumps({"session_id": "s1", "message": "hello", **body})
    return event


def _launch(
    body: dict[str, Any], answer: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    invoker = _Capture(answer)
    status, response = handler.route(_post(body), FakeReader([]), invoker)
    return status, response, invoker.payloads


def test_no_model_runs_the_deployment_default():
    status, _, [payload] = _launch({})
    assert status == 200
    assert "model" not in payload and "effort" not in payload


@pytest.mark.parametrize(
    "body",
    [
        {"model": OPUS},
        {"model": OPUS, "effort": "high"},
        {"effort": "low"},
        {"model": "openai/gpt-5", "effort": "xhigh"},
        {"model": "not-a-provider/anything at all", "effort": "whatever"},
        {"model": "x" * 256, "effort": "e" * 32},
    ],
)
def test_any_short_string_reaches_the_runtime_unchanged(body):
    status, response, [payload] = _launch(body)
    assert status == 200 and response["taskId"] == "deadbeef"
    assert {key: payload[key] for key in ("model", "effort") if key in payload} == body


def test_an_empty_model_and_effort_send_nothing():
    status, _, [payload] = _launch({"model": "", "effort": ""})
    assert status == 200
    assert "model" not in payload and "effort" not in payload


@pytest.mark.parametrize(
    "body",
    [
        {"model": "x" * 257},
        {"effort": "e" * 33},
        {"model": "bedrock/model\nid"},
        {"model": "bedrock/model\x00"},
        {"effort": "high\t"},
        {"model": 5},
        {"model": ["bedrock/x"]},
        {"effort": {"level": "high"}},
        {"model": OPUS, "effort": True},
    ],
)
def test_a_non_string_oversize_or_control_character_choice_is_a_400_and_never_launches(body):
    status, response, payloads = _launch(body)
    assert status == 400 and "printable characters" in response["error"]
    assert payloads == []


def test_the_runtimes_refusal_is_a_400_with_its_reason():
    reason = "Unknown model provider 'nope' in 'nope/x'. Supported providers: anthropic, bedrock"
    status, response, [payload] = _launch(
        {"model": "nope/x"}, answer={"status": "error", "error": reason}
    )
    assert payload["model"] == "nope/x"
    assert (status, response) == (400, {"error": reason})


def test_a_launch_without_a_choice_keeps_the_runtimes_answer():
    status, response, _ = _launch({}, answer={"status": "error", "error": "busy"})
    assert (status, response) == (200, {"status": "error", "error": "busy"})


def test_whoami_lists_no_models():
    _, body = handler.route(_evt("/api/chat", query={"whoami": "1"}), None, invoker=None)
    assert body == {"github": None, "person": None}


def test_session_turns_carry_the_model_each_run_recorded():
    rows = [
        {"task_id": "t1", "session_id": "s1", "status": "completed",
         "started_at": "2999-01-01T00:00:00+00:00", "model": OPUS, "effort": "medium"},
        {"task_id": "t2", "session_id": "s1", "status": "completed",
         "started_at": "2999-01-01T00:01:00+00:00"},
    ]
    detail = handler._session_detail(rows, "s1")
    assert [(t.get("model"), t.get("effort")) for t in detail["turns"]] == [
        (OPUS, "medium"),
        (None, None),
    ]
