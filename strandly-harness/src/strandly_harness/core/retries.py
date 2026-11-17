"""Transient-error classification + backoff for the run-level retry (mid-stream failures).

botocore retries (see ``model.build_model``) cover failures *at request time*; they do **not**
cover a connection dropped **mid-EventStream**, which is where long streaming runs (minutes to
hours on the default tier) actually die. Those surface as many different exception types
(``urllib3`` ProtocolError, botocore EventStreamError, ``ConnectionResetError``, wrapped
ClientErrors, SDK-specific classes), so we classify on the stringified error rather than the type.

Genuine logic bugs (KeyError, ValueError, assertion failures, access-denied, validation errors)
match none of these markers and must fail loudly — the classifier is deliberately a *conservative
allow-list of retryable blips*, not a catch-all.
"""

from __future__ import annotations

import random

from strandly_harness.core.constants import (
    RUN_RETRY_BACKOFF_BASE_SECONDS,
    RUN_RETRY_BACKOFF_MAX_SECONDS,
)

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

#: Sent on a retry instead of the original prompt. The per-session agent cache preserves the full
#: message history across the re-invoke, so the agent *resumes* from where the stream dropped
#: instead of re-running completed work (no duplicate merges/comments/PRs).
CONTINUATION_PROMPT = (
    "The previous response was interrupted by a transient infrastructure error "
    "(connection reset / timeout) on the model endpoint. Your tool calls and their "
    "results so far are preserved in this conversation. Do NOT repeat any action you "
    "have already completed (e.g. merges, comments, PRs, file writes) — first briefly "
    "verify what was already done if unsure, then continue from where you left off "
    "and produce your final response."
)


def is_transient_error(exc: BaseException) -> bool:
    """True iff ``exc`` looks like a retryable infra/network blip (marker match on the repr)."""
    haystack = f"{type(exc).__name__}: {exc!r}".lower()
    return any(marker in haystack for marker in TRANSIENT_ERROR_MARKERS)


def backoff_seconds(attempt: int) -> float:
    """Exponential backoff with jitter for retry ``attempt`` (1-based): ~8s, 16s, 32s, 64s, 120s."""
    base = min(RUN_RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), RUN_RETRY_BACKOFF_MAX_SECONDS)
    return base + random.uniform(0, 3.0)
