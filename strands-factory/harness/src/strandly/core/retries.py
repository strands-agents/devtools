"""The model-call retry: the SDK's ``ModelRetryStrategy`` widened to transient infrastructure errors.

botocore retries cover request-time failures, not a connection dropped mid-EventStream, where long
runs actually fail. The SDK event loop catches any exception from a model call, and this strategy
re-issues that call (history intact, no tool batch re-run) when the error is transient. Those
surface as many exception types, so errors are classified on their text; logic, validation and
access errors are never retried.
"""

from __future__ import annotations

import logging

from strands import ModelRetryStrategy

from strandly.core.constants import (
    MODEL_RETRY_INITIAL_DELAY_SECONDS,
    MODEL_RETRY_MAX_ATTEMPTS,
    MODEL_RETRY_MAX_DELAY_SECONDS,
)

logger = logging.getLogger(__name__)

#: Substrings (lowercased) identifying transient, retryable infrastructure errors. Includes both
#: Bedrock-shaped signatures and cross-provider ones (Anthropic direct 529 ``overloaded``, rate
#: limits, client-side connection/timeout errors) so a future provider swap doesn't silently lose
#: retry coverage.
TRANSIENT_ERROR_MARKERS = (
    # Connection-level
    "connection reset by peer",
    "connection broken",
    "connection aborted",
    "remotedisconnected",
    "protocolerror",
    "broken pipe",
    "brokenpipe",
    "chunkedencoding",
    "incomplete read",
    "(104,",
    # Timeouts
    "read timed out",
    "readtimeouterror",
    "connecttimeouterror",
    "apitimeouterror",
    "apiconnectionerror",
    # Throttling / capacity
    "throttlingexception",
    "too many requests",
    "rate limit",
    "rate_limit",
    "overloaded",
    "servicequotaexceeded",
    # Server-side 5xx
    "internalservererror",
    "internalserverexception",
    "serviceunavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "modelnotready",
    "modelerror",
    "modelstreamerror",
    "eventstream",
    "reached max retries",
)

def is_transient_error(exc: BaseException) -> bool:
    """True iff ``exc`` looks like a retryable infra/network blip (marker match on the repr)."""
    haystack = f"{type(exc).__name__}: {exc!r}".lower()
    return any(marker in haystack for marker in TRANSIENT_ERROR_MARKERS)


class TransientModelRetry(ModelRetryStrategy):
    """Retries a failed model call on throttling or a transient error, with exponential backoff."""

    def __init__(self) -> None:
        """Initialize with Strandly's attempt budget and backoff (8 s doubling to 120 s)."""
        super().__init__(
            max_attempts=MODEL_RETRY_MAX_ATTEMPTS,
            initial_delay=MODEL_RETRY_INITIAL_DELAY_SECONDS,
            max_delay=MODEL_RETRY_MAX_DELAY_SECONDS,
        )

    def is_retryable(self, exception: Exception) -> bool:
        """Whether ``exception`` is throttling or a transient error (the strategy bounds attempts)."""
        retryable = super().is_retryable(exception) or is_transient_error(exception)
        if retryable:
            logger.warning(
                "error=<%s: %s> | model call failed with a transient error",
                type(exception).__name__,
                exception,
            )
        return retryable
