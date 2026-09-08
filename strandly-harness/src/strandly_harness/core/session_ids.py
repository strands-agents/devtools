"""Session-id normalization — pure stdlib, strands-free on purpose.

These two helpers live in ``core`` (not ``memory``) so the strands-free zone — ``ops`` and the
trigger-Lambda bundle — can normalize session ids without importing ``memory.session`` (which pulls
the Strands SDK). ``ops.runtime_client`` calls ``runtime_session_id`` at dispatch time; keeping the
function here means ``ops`` never reaches across the boundary, so the import-hygiene contract holds
without special-casing. ``memory.session`` re-exports both names for back-compat.
"""

from __future__ import annotations

import re

from strandly_harness.core.constants import RUNTIME_SESSION_ID_MIN_LEN

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def sanitize_session_id(raw: str) -> str:
    """Make an id filesystem- and AgentCore-safe (no slashes/spaces)."""
    return _UNSAFE.sub("-", raw).strip("-") or "session"


def runtime_session_id(raw: str) -> str:
    """The AgentCore **Runtime** session id (instance affinity) for a user-supplied id.

    Distinct from the **Memory** session id (:func:`sanitize_session_id`, what we read back): the
    runtime id must be slash-free **and** at least ``RUNTIME_SESSION_ID_MIN_LEN`` (33) chars, or
    ``InvokeAgentRuntime`` throws an opaque ``ValidationException`` at call time. We sanitize the
    slashes the same way, then right-pad a short id deterministically (``-000…``) so a short
    ``--session-id`` doesn't blow up at invoke time. The padding is deterministic, so the same input
    always maps to the same affinity key (a later poll lands on the same instance).
    """
    sid = sanitize_session_id(raw)
    if len(sid) < RUNTIME_SESSION_ID_MIN_LEN:
        sid = sid + "-" + "0" * (RUNTIME_SESSION_ID_MIN_LEN - len(sid) - 1)
    return sid
