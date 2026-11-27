"""GitHub client behavior and security regression tests."""

from __future__ import annotations

import logging as _logging
from datetime import datetime as _dt
from datetime import timedelta as _td
from datetime import timezone as _tz

import pytest

from strandly.core.config import GitHubSettings
from strandly.tools import github_client as client


def test_get_token_prefers_config_resolved_token(monkeypatch):
    # Regression (prod): on the deployed runtime the token lives ONLY in the config secret and never
    # reaches os.environ, so use_github must read the token GitHubSettings carries from the loaded
    # Config. With no token env vars set, the config token must still be used.
    for name in ("STRANDLY_GITHUB_TOKEN", "GITHUB_TOKEN", "PAT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    settings = GitHubSettings(token="ghp_fromsecret")
    assert client._get_token(settings, use_pat_token=False) == "ghp_fromsecret"


def test_get_token_falls_back_to_environ_when_no_config_token(monkeypatch):
    # Local / GitHub-Actions: no config token → scan the env var names (the original behavior).
    for name in ("STRANDLY_GITHUB_TOKEN", "GITHUB_TOKEN", "PAT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fromenv")
    settings = GitHubSettings()  # token=None
    assert client._get_token(settings, use_pat_token=False) == "ghp_fromenv"


def _ts(minutes_ago: int) -> str:
    return (_dt.now(_tz.utc) - _td(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ext_write(minutes_ago: int = 5) -> dict:
    return {
        "type": "IssueCommentEvent",
        "repo": {"name": "external-org/x"},
        "created_at": _ts(minutes_ago),
    }


def _throttle_paged(monkeypatch, pages: dict[int, object]) -> list[str]:
    """Wire _viewer_login + a per-page _rest_get stub; returns the call log."""
    calls: list[str] = []
    monkeypatch.setattr(client, "_viewer_login", lambda token: "agent")

    def rest(path: str, token: str):
        calls.append(path)
        payload = pages.get(int(path.rsplit("page=", 1)[1]), [])
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(client, "_rest_get", rest)
    return calls


def test_count_external_writes_spans_multiple_pages(monkeypatch):
    # The undercount scenario: a full page 1 (write events mixed with non-write noise) used to
    # be the ONLY page read — writes on page 2 were invisible. They must now be counted.
    page1 = [_ext_write() for _ in range(60)] + [
        {"type": "WatchEvent", "repo": {"name": "external-org/x"}, "created_at": _ts(5)}
        for _ in range(40)
    ]  # full page: 60 external writes + 40 non-write noise events
    page2 = [_ext_write() for _ in range(7)]  # partial page → feed ends
    calls = _throttle_paged(monkeypatch, {1: page1, 2: page2})
    count = client.count_external_writes(internal_owners={"strands-agents"}, token="t")
    assert count == 67
    assert len(calls) == 2


def test_count_external_writes_early_stops_on_old_event(monkeypatch):
    # Cheapest correct stop: once an event predates the 24h window, deeper pages are older
    # still — no further fetches even though the page was full.
    page1 = [_ext_write() for _ in range(50)] + [
        _ext_write(minutes_ago=60 * 25) for _ in range(50)  # 25h ago: outside the window
    ]
    calls = _throttle_paged(monkeypatch, {1: page1, 2: [_ext_write() for _ in range(100)]})
    count = client.count_external_writes(internal_owners={"strands-agents"}, token="t")
    assert count == 50  # in-window writes only
    assert len(calls) == 1  # early stop: page 2 never fetched


def test_count_external_writes_floor_warning_on_feed_exhaustion(monkeypatch, caplog):
    # 3 full in-window pages = the feed's hard cap without covering the window: the count is
    # only a FLOOR. The residual undercount must be logged, not silent — and the walk must be
    # bounded at the API max (no hot loop).
    full = [_ext_write() for _ in range(100)]
    calls = _throttle_paged(monkeypatch, {1: full, 2: full, 3: full, 4: full})
    with caplog.at_level(_logging.WARNING, logger="strandly.tools.github"):
        count = client.count_external_writes(internal_owners={"strands-agents"}, token="t")
    assert count == 300
    assert len(calls) == 3  # hard-bounded at the API max
    assert any("floor" in r.message for r in caplog.records)


def test_count_external_writes_page1_failure_still_fails_open(monkeypatch):
    # Unchanged fail-open contract: nothing fetched at all → None (caller never blocks on a blip).
    _throttle_paged(monkeypatch, {1: RuntimeError("api down")})
    assert client.count_external_writes(internal_owners=set(), token="t") is None


def test_count_external_writes_later_page_failure_returns_floor(monkeypatch, caplog):
    # A mid-walk failure degrades to the floor-so-far (with a warning), NOT to None — partial
    # data still enforces the throttle better than disabling it.
    page1 = [_ext_write() for _ in range(100)]
    _throttle_paged(monkeypatch, {1: page1, 2: RuntimeError("api down")})
    with caplog.at_level(_logging.WARNING, logger="strandly.tools.github"):
        count = client.count_external_writes(internal_owners={"strands-agents"}, token="t")
    assert count == 100
    assert any("floor" in r.message for r in caplog.records)


def test_count_external_writes_internal_and_unparseable_skipped(monkeypatch):
    # Parity with the pre-pagination behavior: internal-owner writes and events with
    # unparseable timestamps are not counted.
    page1 = [
        _ext_write(),
        {"type": "PushEvent", "repo": {"name": "strands-agents/sdk"}, "created_at": _ts(5)},
        {"type": "IssuesEvent", "repo": {"name": "external-org/x"}, "created_at": "garbage"},
    ]
    _throttle_paged(monkeypatch, {1: page1})
    assert client.count_external_writes(internal_owners={"strands-agents"}, token="t") == 1


def test_count_external_writes_page1_non_list_fails_open(monkeypatch):
    # Preserved contract: a non-list page-1 payload (nothing usable fetched) → None, i.e.
    # fail OPEN — never 0, which would mean "0 writes used" and re-arm the full budget.
    _throttle_paged(monkeypatch, {1: {"message": "bad gateway"}})
    assert client.count_external_writes(internal_owners=set(), token="t") is None


def test_count_external_writes_later_page_non_list_returns_floor(monkeypatch, caplog):
    # A non-list payload mid-walk degrades to the floor-so-far with a warning (same shape
    # as a raised exception on a later page).
    page1 = [_ext_write() for _ in range(100)]
    _throttle_paged(monkeypatch, {1: page1, 2: {"message": "bad gateway"}})
    with caplog.at_level(_logging.WARNING, logger="strandly.tools.github"):
        count = client.count_external_writes(internal_owners={"strands-agents"}, token="t")
    assert count == 100
    assert any("floor" in r.message for r in caplog.records)


def test_count_external_writes_malformed_repo_shapes_do_not_raise(monkeypatch):
    # Fail-open means never crash: non-dict `repo`, null `name`, and non-dict events are
    # all skipped instead of raising out of the throttle check (review finding).
    page1 = [
        _ext_write(),
        {"type": "IssuesEvent", "repo": "not-a-dict", "created_at": _ts(5)},
        {"type": "IssuesEvent", "repo": {"name": None}, "created_at": _ts(5)},
        "not-a-dict-event",
    ]
    _throttle_paged(monkeypatch, {1: page1})
    assert client.count_external_writes(internal_owners={"strands-agents"}, token="t") == 1


@pytest.mark.parametrize(
    "type_name",
    ["Label", "Milestone", "PullRequestReviewThread"],
)
def test_resolve_query_has_fragment_for_repo_scoped_type(type_name):
    """``LA_``/``MI_``/``PRRT_`` targets resolve, rather than always being blocked."""
    assert f"... on {type_name} {{ repository {{ nameWithOwner }} }}" in client._RESOLVE_QUERY


def test_resolve_query_requests_typename_for_diagnosability():
    """``__typename`` makes a future missing fragment self-diagnosing in the logs."""
    assert "__typename" in client._RESOLVE_QUERY


@pytest.mark.parametrize(
    ("node_id", "typename"),
    [
        ("LA_kwDOAllowedRealLabel012345", "Label"),
        ("MI_kwDOAllowedRealMilestone67", "Milestone"),
        ("PRRT_kwDOAllowedRealThread012", "PullRequestReviewThread"),
    ],
)
def test_label_milestone_review_thread_now_resolve(monkeypatch, node_id, typename):
    """With the fragments present these types resolve to their repo via ``repository``."""
    monkeypatch.setattr(
        client, "_graphql",
        lambda q, v, t: {
            "data": {
                "node": {
                    "__typename": typename,
                    "repository": {"nameWithOwner": "strands-agents/harness-sdk"},
                }
            }
        },
    )
    assert client.resolve_node_owner(node_id, "t") == "strands-agents/harness-sdk"


def test_resolve_logs_warning_for_null_node(monkeypatch, caplog):
    """A null ``node`` (bad or inaccessible id) is logged, not a silent None."""
    monkeypatch.setattr(client, "_graphql", lambda q, v, t: {"data": {"node": None}})
    with caplog.at_level(_logging.WARNING, logger="strandly.tools.github"):
        assert client.resolve_node_owner("PR_kwDOSomethingUnknown01", "t") is None
    assert any("PR_kwDOSomethingUnknown01" in r.getMessage() for r in caplog.records)


def test_resolve_logs_warning_and_typename_for_unhandled_type(monkeypatch, caplog):
    """A non-null node with no repo path must log — and name the type.

    ``__typename`` turns "something was blocked" into "``ProjectV2`` has no fragment", which is the
    whole point: every future missing fragment diagnoses itself from one log line.
    """
    monkeypatch.setattr(
        client, "_graphql", lambda q, v, t: {"data": {"node": {"__typename": "ProjectV2"}}}
    )
    with caplog.at_level(_logging.WARNING, logger="strandly.tools.github"):
        assert client.resolve_node_owner("PVT_kwHOSomeProject0001", "t") is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("PVT_kwHOSomeProject0001" in m and "ProjectV2" in m for m in messages)


class _FakeResp:
    def __init__(self, body: bytes = b"{}") -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture_request(monkeypatch) -> list:
    from strandly.ops import github

    seen: list = []

    def _fake_open(req, timeout=None):
        seen.append(req)
        return _FakeResp()

    monkeypatch.setattr(github._OPENER, "open", _fake_open)
    return seen


@pytest.mark.parametrize(
    "call",
    [
        lambda: client._rest_call("GET", "/repos/o/r/actions/runs/1/logs", "ghp_secret"),
        lambda: client._request("GET", "https://api.github.com/repos/o/r", "ghp_secret"),
    ],
    ids=["rest_call", "request"],
)
def test_requests_are_authenticated(monkeypatch, call):
    seen = _capture_request(monkeypatch)
    call()
    (req,) = seen
    assert req.get_header("Authorization") == "Bearer ghp_secret"
    assert req.get_header("User-agent")


def test_anonymous_request_sends_no_authorization(monkeypatch):
    seen = _capture_request(monkeypatch)
    client._request("GET", "https://api.github.com/repos/o/r", None)
    assert seen[0].get_header("Authorization") is None
