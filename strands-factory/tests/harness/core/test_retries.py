"""The model-call retry (strandly.core.retries): transient-error classification and the strategy."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import strands.event_loop._retry as strands_retry
from strands import Agent, tool
from strands.types.exceptions import ModelThrottledException

import strandly.core.retries as retries
from scripted_model import ScriptedModel, text_events, tool_use_events
from strandly.core.constants import MODEL_RETRY_MAX_ATTEMPTS
from strandly.core.events import translate
from strandly.core.retries import TransientModelRetry, is_transient_error


@pytest.mark.parametrize(
    "exc",
    [
        # Bedrock / botocore shapes (where mid-stream deaths actually surface)
        ConnectionResetError(104, "Connection reset by peer"),
        Exception("ProtocolError('Connection aborted.', RemoteDisconnected(...))"),
        Exception("ReadTimeoutError: Read timed out. (read timeout=300)"),
        Exception("ThrottlingException: Too many requests"),
        Exception("ServiceUnavailableException: Bedrock is unable to process your request"),
        Exception("ModelStreamErrorException: An error occurred during streaming"),
        Exception("EventStreamError: An error occurred (modelStreamErrorException)"),
        Exception("ServiceQuotaExceededException: quota exceeded"),
        Exception("botocore retries exhausted: reached max retries: 9"),
        # Cross-provider shapes (Anthropic direct / OpenAI / proxies)
        Exception(
            'APIStatusError: Error code: 529 - {"type": "error", '
            '"error": {"type": "overloaded_error", "message": "Overloaded"}}'
        ),
        Exception("RateLimitError: Rate limit reached for model"),
        Exception('{"type": "error", "error": {"type": "rate_limit_error"}}'),
        Exception("APIConnectionError: Connection error."),
        Exception("APITimeoutError: Request timed out."),
        Exception("502 Bad Gateway"),
        Exception("504 Gateway Timeout"),
        BrokenPipeError(32, "Broken pipe"),
        Exception("ChunkedEncodingError: Connection broken: IncompleteRead(0 bytes read)"),
    ],
)
def test_transient_errors_detected(exc):
    assert is_transient_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        # Real bugs / permanent conditions must fail loudly, not retry silently.
        KeyError("usage"),
        ValueError("unknown model tier 'nope'"),
        AssertionError("invariant violated"),
        Exception("AccessDeniedException: not authorized to invoke this model"),
        Exception("ResourceNotFoundException: model does not exist"),
        Exception("ValidationException: max_tokens too large"),
        TypeError("'NoneType' object is not iterable"),
    ],
)
def test_permanent_errors_not_detected(exc):
    assert is_transient_error(exc) is False


class _Flaky(ScriptedModel):
    """Fails the first ``failures`` calls mid-stream (after some text), then replays its script."""

    def __init__(self, failures: int, error: Exception, *responses: list[dict[str, Any]]) -> None:
        super().__init__(*responses)
        self.failures = failures
        self.error = error
        self.calls: list[list[dict[str, Any]]] = []

    async def stream(self, messages, *args, **kwargs):  # type: ignore[override]
        self.calls.append(list(messages))
        if len(self.calls) <= self.failures:
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": "partial "}}}
            raise self.error
        async for ev in super().stream(messages, *args, **kwargs):
            yield ev


tool_runs: list[str] = []


@tool
def act() -> str:
    """Record one run of a side-effecting tool."""
    tool_runs.append("ran")
    return "done"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    # Only the strategy's module: patching asyncio.sleep itself would reach every coroutine.
    monkeypatch.setattr(strands_retry, "asyncio", SimpleNamespace(sleep=sleep))
    tool_runs.clear()
    return delays


def _agent(model: ScriptedModel) -> Agent:
    return Agent(model=model, tools=[act], retry_strategy=TransientModelRetry(), callback_handler=None)


@pytest.mark.asyncio
async def test_a_dropped_stream_retries_the_model_call_with_history_intact(_no_sleep):
    error = RuntimeError("EventStreamError: connection reset by peer")
    model = _Flaky(1, error, tool_use_events("act", "t1"), text_events("all done"))
    agent = _agent(model)

    events = [ev async for ev in translate(agent.stream_async("go"))]

    retry = [ev for ev in events if ev.kind == "retry"]
    assert [ev.data for ev in retry] == [{"delaySeconds": 8}]
    assert _no_sleep == [8]
    # Same request re-sent: no continuation prompt, and the tool ran once.
    assert model.calls[0] == model.calls[1]
    assert tool_runs == ["ran"]
    assert agent.messages[-1]["content"][0]["text"] == "all done"
    assert "partial" not in str(agent.messages)


@pytest.mark.asyncio
async def test_throttling_is_retried_and_backoff_doubles_to_the_cap(_no_sleep):
    model = _Flaky(5, ModelThrottledException("slow down"), text_events("ok"))
    await _agent(model).invoke_async("go")
    assert _no_sleep == [8, 16, 32, 64, 120]


@pytest.mark.asyncio
async def test_attempts_are_bounded_then_the_error_escapes(_no_sleep):
    error = RuntimeError("ServiceUnavailableException: try again")
    model = _Flaky(99, error)
    with pytest.raises(RuntimeError, match="ServiceUnavailable"):
        await _agent(model).invoke_async("go")
    assert len(model.calls) == MODEL_RETRY_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_a_permanent_error_is_not_retried(_no_sleep):
    model = _Flaky(1, ValueError("ValidationException: messages: bad shape"))
    with pytest.raises(ValueError):
        await _agent(model).invoke_async("go")
    assert len(model.calls) == 1 and _no_sleep == []


@pytest.mark.asyncio
async def test_build_agent_wires_one_strategy_per_agent(fake_model, tmp_path):
    from strandly.core.agent import build_agent
    from strandly.core.config import Config
    from strandly.core.context import RuntimeContext

    first, second = [
        await build_agent(Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model)
        for _ in range(2)
    ]
    assert isinstance(first._retry_strategy, TransientModelRetry)
    assert first._retry_strategy is not second._retry_strategy


def test_retryable_errors_are_logged(caplog):
    with caplog.at_level("WARNING", logger=retries.__name__):
        assert TransientModelRetry().is_retryable(RuntimeError("read timed out"))
    assert "transient error" in caplog.text
