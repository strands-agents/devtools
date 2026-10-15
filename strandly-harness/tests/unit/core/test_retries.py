"""Run-level retry primitives: transient-error classification + backoff (strandly_harness.core.retries)."""

from __future__ import annotations

import pytest

from strandly_harness.core.constants import (
    RUN_RETRY_BACKOFF_BASE_SECONDS,
    RUN_RETRY_BACKOFF_MAX_SECONDS,
)
from strandly_harness.core.retries import (
    CONTINUATION_PROMPT,
    backoff_seconds,
    is_transient_error,
)


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


def test_backoff_grows_exponentially_and_caps():
    # Jitter is [0, 3); strip it by comparing against the deterministic base envelope.
    for attempt, base in [(1, 8.0), (2, 16.0), (3, 32.0), (4, 64.0), (5, 120.0), (6, 120.0)]:
        expected = min(
            RUN_RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), RUN_RETRY_BACKOFF_MAX_SECONDS
        )
        assert expected == base
        got = backoff_seconds(attempt)
        assert base <= got < base + 3.0


def test_continuation_prompt_tells_agent_to_resume_not_restart():
    lower = CONTINUATION_PROMPT.lower()
    assert "do not repeat" in lower
    assert "preserved" in lower
    assert "continue" in lower
