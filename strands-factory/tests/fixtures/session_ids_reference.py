"""Frozen reference implementations of the persisted session-id and session-store key formats.

Stored sessions, snapshots and Memory threads are addressed by these ids, so every live helper must
match them byte for byte. ``mcp_`` names are the thin package's own copies. Never edit this file to
follow the code.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import re
from typing import Any

RUNTIME_SESSION_ID_MIN_LEN = 33
SESSION_STORE_SESSION_PREFIX = "session"
SESSION_STORE_AGENT_ID = "default"
SESSION_STORE_LATEST_SNAPSHOT = "snapshot_latest.json"
SESSION_STORE_ACTOR_MARKER = "memory-actor.json"
SESSION_ID_MAX_LEN = 100
_DIGEST_LEN = 16
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_SESSION_ID_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_SESSION_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
_MEMORY_SESSION_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_MEMORY_SESSION_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


# harness/src/strandly/core/session_ids.py
def sanitize_session_id(raw: str) -> str:
    return _UNSAFE.sub('-', raw).strip('-') or 'session'

def runtime_session_id(raw: str) -> str:
    sid = sanitize_session_id(raw)
    if len(sid) < RUNTIME_SESSION_ID_MIN_LEN:
        sid = sid + '-' + '0' * (RUNTIME_SESSION_ID_MIN_LEN - len(sid) - 1)
    return sid


# harness/src/strandly/core/session_store_reader.py
def store_session_id(raw: str) -> str:
    sid = sanitize_session_id(raw)
    return 'session' if not sid.strip('.') else sid

def snapshot_key(session_id: str) -> str:
    sid = store_session_id(session_id)
    return f'{SESSION_STORE_SESSION_PREFIX}/{sid}/scopes/agent/{SESSION_STORE_AGENT_ID}/snapshots/{SESSION_STORE_LATEST_SNAPSHOT}'

def actor_marker_key(session_id: str) -> str:
    return f'{SESSION_STORE_SESSION_PREFIX}/{store_session_id(session_id)}/{SESSION_STORE_ACTOR_MARKER}'

def snapshot_messages(self, session_id: str) -> builtins.list[dict[str, Any]] | None:
    raw = self.read(snapshot_key(session_id))
    if raw is None:
        return None
    doc = json.loads(raw)
    messages = (doc.get('data') or {}).get('messages') if isinstance(doc, dict) else None
    return [m for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []


# harness/src/strandly/memory/scopes.py
def memory_session_id(session_id: str) -> str:
    sid = store_session_id(session_id)
    if len(sid) <= SESSION_ID_MAX_LEN and _SESSION_ID_OK.match(sid):
        return sid
    digest = hashlib.sha256(sid.encode('utf-8')).hexdigest()[:_DIGEST_LEN]
    base = _SESSION_ID_UNSAFE.sub('-', sid).lstrip('-_') or 's'
    return f'{base[:SESSION_ID_MAX_LEN - _DIGEST_LEN - 1]}-{digest}'


# dashboard/api/handler.py
def _sanitize_session_id(raw: str) -> str:
    return _UNSAFE.sub('-', raw).strip('-') or 'session'

def _store_session_id(raw: str) -> str:
    sid = _sanitize_session_id(raw)
    return 'session' if not sid.strip('.') else sid

def _memory_session_id(raw: str) -> str:
    sid = _store_session_id(raw)
    if len(sid) <= 100 and _MEMORY_SESSION_OK.match(sid):
        return sid
    digest = hashlib.sha256(sid.encode('utf-8')).hexdigest()[:16]
    base = _MEMORY_SESSION_UNSAFE.sub('-', sid).lstrip('-_') or 's'
    return f'{base[:100 - 16 - 1]}-{digest}'

def _runtime_session_id(raw: str) -> str:
    sid = _sanitize_session_id(raw)
    if len(sid) < RUNTIME_SESSION_ID_MIN_LEN:
        sid = sid + '-' + '0' * (RUNTIME_SESSION_ID_MIN_LEN - len(sid) - 1)
    return sid

def _snapshot_items(self, session_id: str) -> list[tuple[str, dict[str, Any], Any]]:
    sid = _store_session_id(session_id)
    raw = self._get(f'{SESSION_STORE_SESSION_PREFIX}/{sid}/scopes/agent/{SESSION_STORE_AGENT_ID}/snapshots/{SESSION_STORE_LATEST_SNAPSHOT}')
    if raw is None:
        return []
    doc = json.loads(raw)
    messages = (doc.get('data') or {}).get('messages') if isinstance(doc, dict) else None
    if not isinstance(messages, list):
        return []
    return [(f'snapshot:{i}', m, None) for i, m in enumerate(messages) if isinstance(m, dict)]


# cli/strandly-mcp/src/strandly_mcp/agentcore.py
def mcp_sanitize_session_id(raw: str) -> str:
    return _UNSAFE.sub('-', raw).strip('-') or 'session'

def mcp_runtime_session_id(raw: str) -> str:
    sid = mcp_sanitize_session_id(raw)
    if len(sid) < RUNTIME_SESSION_ID_MIN_LEN:
        sid = sid + '-' + '0' * (RUNTIME_SESSION_ID_MIN_LEN - len(sid) - 1)
    return sid
