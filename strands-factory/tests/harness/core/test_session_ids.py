"""Every session-id helper matches the frozen reference formats in ``fixtures/session_ids_reference.py``."""

from __future__ import annotations

import importlib
import inspect
import json
import random
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from strandly.core import constants, session_ids, session_store_reader
from strandly.core import session_ids as shared
from strandly.memory import scopes, session
from strandly_mcp import agentcore

_API_DIR = Path(__file__).resolve().parents[3] / "dashboard/api"
sys.path.insert(0, str(_API_DIR))
handler = importlib.import_module("handler")

# The reference holds its own copies of the algorithms, not imports of any current helper.
_REFERENCE = runpy.run_path(str(Path(__file__).resolve().parents[2] / "fixtures/session_ids_reference.py"))
_CONSTANTS = {
    "RUNTIME_SESSION_ID_MIN_LEN": 33,
    "SESSION_STORE_SESSION_PREFIX": "session",
    "SESSION_STORE_AGENT_ID": "default",
    "SESSION_STORE_LATEST_SNAPSHOT": "snapshot_latest.json",
    "SESSION_STORE_ACTOR_MARKER": "memory-actor.json",
    "SESSION_ID_MAX_LEN": 100,
    "_DIGEST_LEN": 16,
}


_RANDOM = random.Random(42)
_IDS = [
    "", "/", "---", " ", "\n\t", ".", "..", "...", "../", "_", "__", "-._-", "a/b",
    "a\\b", "some id", "é🤖中", "a\x00b", "a\r\nb", "slack-C01-173.123", "ABC_def-1.2",
    "gh-owner-repo-issue-1", "a" * 31, "a" * 32, "a" * 33, "a" * 34,
    "a" * 99, "a" * 100, "a" * 101, "." * 101, "_" * 1000, "a/b" * 1000,
] + ["".join(_RANDOM.choices("aZ01/-_. \n\x00é中🤖", k=_RANDOM.randrange(160))) for _ in range(100)]


@pytest.mark.parametrize("raw", _IDS)
def test_ids_match_the_reference_implementation(raw):
    for name, actuals in {
        "sanitize_session_id": [shared.sanitize_session_id, session_ids.sanitize_session_id, session.sanitize_session_id],
        "runtime_session_id": [shared.runtime_session_id, session_ids.runtime_session_id, session.runtime_session_id],
        "store_session_id": [shared.store_session_id, session_store_reader.store_session_id],
        "memory_session_id": [shared.memory_session_id, scopes.memory_session_id],
        "snapshot_key": [shared.snapshot_key, session_store_reader.snapshot_key],
        "actor_marker_key": [shared.actor_marker_key, session_store_reader.actor_marker_key],
    }.items():
        expected = _REFERENCE[name](raw)
        for actual in actuals:
            assert actual(raw).encode() == expected.encode()
    for name in ("sanitize_session_id", "runtime_session_id", "store_session_id", "memory_session_id"):
        assert getattr(handler, f"_{name}")(raw) == _REFERENCE[f"_{name}"](raw)
    for name in ("sanitize_session_id", "runtime_session_id"):
        assert getattr(agentcore, name)(raw) == _REFERENCE[f"mcp_{name}"](raw)


def test_layout_and_limit_values_remain_compatible():
    for name, expected in _CONSTANTS.items():
        assert getattr(shared, name) == expected
        for module in (constants, handler, agentcore, session_ids, scopes, session_store_reader):
            if hasattr(module, name):
                assert getattr(module, name) == expected


def _outcome(call):
    try:
        return ("ok", call())
    except (ValueError, AttributeError, TypeError, UnicodeError) as exc:
        return (type(exc).__name__, str(exc))


@pytest.mark.parametrize("raw", [None, 0, True, b"a", [], {}])
def test_invalid_id_types_keep_the_same_errors(raw):
    for name in ("sanitize_session_id", "runtime_session_id", "store_session_id", "memory_session_id"):
        assert _outcome(lambda name=name: getattr(shared, name)(raw)) == _outcome(lambda name=name: _REFERENCE[name](raw))
        assert _outcome(lambda name=name: getattr(handler, f"_{name}")(raw)) == _outcome(lambda name=name: _REFERENCE[f"_{name}"](raw))
    for name in ("sanitize_session_id", "runtime_session_id"):
        assert _outcome(lambda name=name: getattr(agentcore, name)(raw)) == _outcome(lambda name=name: _REFERENCE[f"mcp_{name}"](raw))


def test_helper_keyword_signatures_remain_compatible():
    for module, prefix in ((shared, ""), (handler, "_"), (agentcore, "mcp_")):
        names = ("sanitize_session_id", "runtime_session_id")
        if module is not agentcore:
            names += ("store_session_id", "memory_session_id")
        for name in names:
            before = _REFERENCE[prefix + name]
            after = getattr(module, "_" + name if module is handler else name)
            parameters = inspect.signature(before).parameters
            assert tuple(inspect.signature(after).parameters) == tuple(parameters)
            kwargs = {next(iter(parameters)): "a/b"}
            assert after(**kwargs) == before(**kwargs)


def test_historical_reexports_still_resolve():
    assert session_ids.sanitize_session_id is session_store_reader.sanitize_session_id
    assert session_ids.runtime_session_id is session.runtime_session_id
    assert session_store_reader.store_session_id is scopes.store_session_id
    assert scopes.memory_session_id is shared.memory_session_id


_SHAPES = [
    None, [], "text", 7, {}, {"data": None}, {"data": {}}, {"data": []},
    {"data": False}, {"data": 0}, {"data": ""}, {"data": [1]}, {"data": True},
    {"data": "bad"}, {"data": {"messages": None}}, {"data": {"messages": {}}},
    {"data": {"messages": "bad"}}, {"data": {"messages": []}},
    {"data": {"messages": [None, {"role": "USER", "content": [{"text": "hello"}]}, 1,
                           {}, {"role": "assistant", "content": [{"toolUse": {}}, {"text": "answer"}]}]}},
]
_RAW = [None, b"", b"{", b"\xff", b'{"data":{"messages":[{}]}}',
        *[json.dumps(shape).encode() for shape in _SHAPES]]


@pytest.mark.parametrize("raw", _RAW)
@pytest.mark.parametrize("sid", ["../", "a/b", ".", "_a", "a" * 101])
def test_snapshot_reads_preserve_missing_errors_filtering_positions_and_keys(raw, sid):
    def compare(old, new, method):
        old_keys, new_keys = [], []
        before = SimpleNamespace(**{method: lambda key: old_keys.append(key) or raw})
        after = SimpleNamespace(**{method: lambda key: new_keys.append(key) or raw})
        assert _outcome(lambda: new(after, sid)) == _outcome(lambda: old(before, sid))
        assert new_keys == old_keys
    compare(_REFERENCE["snapshot_messages"], session_store_reader.SessionStoreReader.snapshot_messages, "read")
    compare(_REFERENCE["_snapshot_items"], handler.SessionStoreReader._snapshot_items, "_get")


def test_dashboard_snapshot_identity_is_original_position():
    reader = SimpleNamespace(_get=lambda key: b'{"data":{"messages":[null,{},false,{}]}}')
    assert handler.SessionStoreReader._snapshot_items(reader, "s") == [
        ("snapshot:1", {}, None), ("snapshot:3", {}, None),
    ]


@pytest.mark.parametrize(("raw", "expected"), [
    ("Release prep", "Release prep"),
    ("  a\n\tb\r\n  c  ", "a b c"),
    ("x\x00y\x1b[31mz\u200b\u202e", "xy[31mz"),
    ("line\u2028break", "line break"),
    ("", ""),
    (" \n ", ""),
])
def test_session_titles_are_one_clean_line(raw, expected):
    assert shared.clean_session_title(raw) == expected


def test_session_title_bounds_and_types():
    limit = shared.SESSION_TITLE_MAX_LEN
    assert shared.clean_session_title("x" * limit) == "x" * limit
    with pytest.raises(ValueError):
        shared.clean_session_title("x" * (limit + 1))
    clipped = shared.clean_session_title("x" * 500, truncate=True)
    assert len(clipped) == limit and clipped.endswith("\u2026")
    with pytest.raises(TypeError):
        shared.clean_session_title(None)


def test_session_title_key_uses_the_sanitized_id():
    assert shared.session_title_key("a/b c") == shared.SESSION_TITLE_KEY_PREFIX + "a-b-c"


# ---- GitHub item ids ------------------------------------------------------------------------

_COLLIDING = [
    ("acme/labs-tool", "acme-labs/tool"),
    ("strands-agents/sdk-python", "strands-agents-sdk/python"),
    ("strands-agents/sdk-python", "strands/agents-sdk-python"),
]


def test_canonical_session_id_format():
    sid = shared.canonical_session_id("strands-agents/sdk-python", shared.KIND_ISSUE, 42)
    assert sid == "gh-strands-agents-sdk-python-b00b0499-issue-42"
    assert shared.repo_digest("Strands-Agents/SDK-Python") == "b00b0499"
    assert shared.parse_github_session_id(sid) == ("strands-agents-sdk-python", "b00b0499", "issue", 42)
    # Longest GitHub names (39-char owner, 100-char repo) stay well inside the runtime's 256.
    longest = shared.canonical_session_id("o" * 39 + "/" + "r" * 100, shared.KIND_DISCUSSION, 10**10 - 1)
    assert len(longest) < 200 and shared.runtime_session_id(longest) == longest


@pytest.mark.parametrize(("ours", "theirs"), _COLLIDING)
def test_repositories_whose_names_read_the_same_get_disjoint_state(ours, theirs):
    from strandly.sandbox.runtime import sandbox_session_id

    a = shared.canonical_session_id(ours, shared.KIND_ISSUE, 42)
    b = shared.canonical_session_id(theirs, shared.KIND_ISSUE, 42)
    assert shared.repo_slug(ours) == shared.repo_slug(theirs)  # the readable part still collides
    assert a != b
    for derive in (
        shared.snapshot_key, shared.memory_session_id, shared.runtime_session_id, shared.actor_marker_key,
        lambda s: shared.transcript_payload_key(s, "1"), shared.session_title_key, sandbox_session_id,
    ):
        assert derive(a) != derive(b)
    assert shared.github_session_belongs_to(a, ours) and shared.github_session_belongs_to(b, theirs)
    assert not shared.github_session_belongs_to(a, theirs)
    assert not shared.github_session_belongs_to(b, ours)


@pytest.mark.parametrize("cased", ["Acme/Labs-Tool", "ACME/LABS-TOOL", "/acme/labs-tool/"])
def test_one_repository_has_one_id_however_it_is_cased(cased):
    sid = shared.canonical_session_id("acme/labs-tool", shared.KIND_PR, 7)
    assert sid == "gh-acme-labs-tool-7d6b2707-pr-7"
    assert shared.canonical_session_id(cased, shared.KIND_PR, 7) == sid
    assert shared.repo_session_prefix(cased) == "gh-acme-labs-tool-7d6b2707"
    assert shared.github_session_belongs_to(sid, cased)
    # An id spelled with the original casing is not one any builder makes, so nobody owns it.
    assert not shared.github_session_belongs_to("gh-Acme-Labs-Tool-7d6b2707-pr-7", cased)


def test_item_numbers_and_kinds_stay_distinct():
    one = shared.canonical_session_id("o/r", shared.KIND_ISSUE, 1)
    twelve = shared.canonical_session_id("o/r", shared.KIND_ISSUE, 12)
    pr = shared.canonical_session_id("o/r", shared.KIND_PR, 1)
    assert len({one, twelve, pr}) == 3
    for bad in (one + "x", one.replace("-1", "-01"), "gh-o-r-issue-1", one.upper(), one + "\n"):
        assert not shared.github_session_belongs_to(bad, "o/r")


def test_sandbox_session_ids_are_digests_of_the_whole_key():
    from strandly.core.constants import RUNTIME_SESSION_ID_MIN_LEN
    from strandly.sandbox.runtime import sandbox_session_id

    # Keys that sanitize to the same text still get different sandboxes (and volumes).
    assert shared.runtime_session_id("a/b-" + "x" * 40) == shared.runtime_session_id("a-b-" + "x" * 40)
    assert sandbox_session_id("a/b-" + "x" * 40) != sandbox_session_id("a-b-" + "x" * 40)
    sid = sandbox_session_id("gh-o-r-96458419-issue-1")
    assert sid == sandbox_session_id("gh-o-r-96458419-issue-1")
    assert RUNTIME_SESSION_ID_MIN_LEN <= len(sid) <= 256
    assert len(sandbox_session_id("x" * 5000)) == len(sid) == 40
