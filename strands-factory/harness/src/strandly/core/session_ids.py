"""Session ids, session-store keys and session titles shared by every deployable.

Standard library only: ``strandly-mcp`` vendors this file as ``strandly_mcp._session_primitives``
and the dashboard Lambda asset stages it at this import path without the rest of the package.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

RUNTIME_SESSION_ID_MIN_LEN = 33
SESSION_STORE_SESSION_PREFIX = "session"
SESSION_STORE_CONTEXT_PREFIX = "context"
SESSION_STORE_AGENT_ID = "default"
SESSION_STORE_LATEST_SNAPSHOT = "snapshot_latest.json"
SESSION_STORE_ACTOR_MARKER = "memory-actor.json"
SESSION_STORE_TRANSCRIPT_DIR = "transcript"
SESSION_ID_MAX_LEN = 100
_DIGEST_LEN = 16
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_SESSION_ID_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_SESSION_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


KIND_ISSUE = "issue"
KIND_PR = "pr"
KIND_DISCUSSION = "disc"
REPO_DIGEST_LEN = 8
# Owner and repository names both allow hyphens, so the readable slug alone is ambiguous
# (``acme/labs-tool`` and ``acme-labs/tool``); the digest of the exact name tells them apart.
_GITHUB_ITEM_ID = re.compile(
    r"gh-(?P<slug>.+)-(?P<digest>[0-9a-f]{8})-(?P<kind>issue|pr|disc)-(?P<number>[1-9][0-9]{0,9})"
)


def _repo_name(repo: str) -> str:
    # GitHub names are case-insensitive, so one repository has exactly one id however it is cased.
    return (repo or "").strip("/").lower()


def repo_slug(repo: str) -> str:
    """The lowercased ``owner/repo`` as the readable ``owner-repo`` part of a session id (not unique)."""
    return _repo_name(repo).replace("/", "-")


def repo_digest(repo: str) -> str:
    """The first 8 hex characters of the SHA-256 of the lowercased ``owner/repo``."""
    return hashlib.sha256(_repo_name(repo).encode("utf-8")).hexdigest()[:REPO_DIGEST_LEN]


def repo_session_prefix(repo: str) -> str:
    """``gh-<owner>-<repo>-<h8>``: the prefix of every GitHub session id for ``repo``."""
    return f"gh-{repo_slug(repo)}-{repo_digest(repo)}"


def canonical_session_id(repo: str, kind: str, number: int | str) -> str:
    """The id every ingress uses for a GitHub item: ``gh-<owner>-<repo>-<h8>-<kind>-<number>``.

    ``kind`` is one of ``KIND_ISSUE``, ``KIND_PR`` or ``KIND_DISCUSSION``; ``<owner>-<repo>`` is
    :func:`repo_slug` (lowercased) and ``h8`` is :func:`repo_digest`.
    """
    return f"{repo_session_prefix(repo)}-{kind}-{number}"


def parse_github_session_id(session_id: str) -> tuple[str, str, str, int] | None:
    """``(slug, digest, kind, number)`` of a :func:`canonical_session_id`, else ``None``."""
    m = _GITHUB_ITEM_ID.fullmatch(session_id or "")
    if m is None:
        return None
    return m["slug"], m["digest"], m["kind"], int(m["number"])


def github_session_belongs_to(session_id: str, repo: str) -> bool:
    """Whether ``session_id`` is exactly the canonical id of one of ``repo``'s items."""
    parsed = parse_github_session_id(session_id)
    if parsed is None:
        return False
    _, digest, kind, number = parsed
    return digest == repo_digest(repo) and canonical_session_id(repo, kind, number) == session_id


def sanitize_session_id(raw: str) -> str:
    """Make an id filesystem- and AgentCore-safe (no slashes/spaces)."""
    return _UNSAFE.sub("-", raw).strip("-") or "session"


def runtime_session_id(raw: str) -> str:
    """Deterministically pad a sanitized ID for AgentCore Runtime affinity."""
    sid = sanitize_session_id(raw)
    if len(sid) < RUNTIME_SESSION_ID_MIN_LEN:
        sid = sid + "-" + "0" * (RUNTIME_SESSION_ID_MIN_LEN - len(sid) - 1)
    return sid


def store_session_id(raw: str) -> str:
    """SnapshotSessionManager also rejects dots-only IDs; use the empty-ID fallback."""
    sid = sanitize_session_id(raw)
    return "session" if not sid.strip(".") else sid


def memory_session_id(session_id: str) -> str:
    """Keep IDs accepted by Memory, otherwise hash the full store ID with its prefix."""
    sid = store_session_id(session_id)
    if len(sid) <= SESSION_ID_MAX_LEN and _SESSION_ID_OK.match(sid):
        return sid
    digest = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
    base = _SESSION_ID_UNSAFE.sub("-", sid).lstrip("-_") or "s"
    return f"{base[: SESSION_ID_MAX_LEN - _DIGEST_LEN - 1]}-{digest}"


def snapshot_key(session_id: str) -> str:
    """The latest snapshot key in the SDK's session-store layout."""
    sid = store_session_id(session_id)
    return (
        f"{SESSION_STORE_SESSION_PREFIX}/{sid}/scopes/agent/{SESSION_STORE_AGENT_ID}/snapshots/"
        f"{SESSION_STORE_LATEST_SNAPSHOT}"
    )


def actor_marker_key(session_id: str) -> str:
    """The object naming the Memory actor a session's transcript is under."""
    return f"{SESSION_STORE_SESSION_PREFIX}/{store_session_id(session_id)}/{SESSION_STORE_ACTOR_MARKER}"


def transcript_payload_key(session_id: str, seq: str) -> str:
    """Where the full payload of a transcript event too large for Memory lives, by its ``seq``.

    Under the session's context stash prefix, so it shares the stash's retention and grants.
    """
    sid = store_session_id(session_id)
    return f"{SESSION_STORE_CONTEXT_PREFIX}/{sid}/{SESSION_STORE_TRANSCRIPT_DIR}/{seq}.json"


def snapshot_messages(raw: bytes) -> list[Any]:
    """Decode the managed history, retaining positions (including malformed entries).

    Callers filter message dicts *after* enumeration to preserve dashboard cursor IDs.
    Invalid JSON and truthy non-object ``data`` retain their existing exceptions.
    """
    doc = json.loads(raw)
    messages = (doc.get("data") or {}).get("messages") if isinstance(doc, dict) else None
    return messages if isinstance(messages, list) else []


# Session titles live as one run-ledger row per session, outside the "recent" GSI (no gsi_pk),
# so run listings never see them. The dashboard's PutItem grant is scoped to this key prefix
# (mirrored as SESSION_TITLE_KEY_PREFIX in infra/stacks/common.py).
SESSION_TITLE_KEY_PREFIX = "session-title#"
SESSION_TITLE_MAX_LEN = 80
_TITLE_SPACE = re.compile(r"\s+")


def session_title_key(session_id: str) -> str:
    """The run-ledger ``task_id`` of a session's title row."""
    return SESSION_TITLE_KEY_PREFIX + sanitize_session_id(session_id)


def clean_session_title(raw: Any, *, truncate: bool = False) -> str:
    """One display line: control/format characters dropped, whitespace collapsed, length bounded.

    An empty result means "no title". A non-string raises ``TypeError``; an over-long title raises
    ``ValueError`` unless ``truncate`` shortens it to :data:`SESSION_TITLE_MAX_LEN` with an ellipsis.
    """
    if not isinstance(raw, str):
        raise TypeError("title must be a string")
    kept = "".join(" " if ch.isspace() else ch for ch in raw if ch.isspace() or ch.isprintable())
    text = _TITLE_SPACE.sub(" ", kept).strip()
    if len(text) > SESSION_TITLE_MAX_LEN:
        if not truncate:
            raise ValueError(f"title must be at most {SESSION_TITLE_MAX_LEN} characters")
        text = text[: SESSION_TITLE_MAX_LEN - 1].rstrip() + "\u2026"
    return text
