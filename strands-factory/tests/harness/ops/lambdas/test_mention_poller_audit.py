"""Hermetic tests for the independent GitHub write-audit — no live network.

The only I/O seams are ``audit._graphql`` / ``audit._rest_get`` (and ``audit._request`` under them);
every test either monkeypatches those or exercises the pure extractors directly. Covers:
extraction from each contribution kind + the events backstop, the in/out-of-org violation logic
(incl. case-insensitivity + the empty-allow-list guard), fail-soft orchestration, SNS notify gating,
and the gated lambda handler.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from strandly.core.config import AuditSettings, Config
from strandly.ops.lambdas.mention_poller import audit

NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)


def _settings(**kw) -> AuditSettings:
    base = dict(allowed_owners=("example-maintainer", "strands-agents"), token="t", lookback_hours=24)
    base.update(kw)
    return AuditSettings(**base)


# ---------------------------------------------------------------------------
# Pure extractor: contributions
# ---------------------------------------------------------------------------
def _viewer(*, issues=(), prs=(), reviews=(), commits=()) -> dict:
    def repo_nodes(names, key):
        return {"nodes": [{key: {"repository": {"nameWithOwner": n}}} for n in names]}

    return {
        "login": "example-user",
        "contributionsCollection": {
            "issueContributions": repo_nodes(issues, "issue"),
            "pullRequestContributions": repo_nodes(prs, "pullRequest"),
            "pullRequestReviewContributions": repo_nodes(reviews, "pullRequestReview"),
            "commitContributionsByRepository": [
                {"repository": {"nameWithOwner": n}} for n in commits
            ],
        },
    }


def test_extract_repos_from_all_contribution_kinds():
    viewer = _viewer(
        issues=["example-maintainer/a"],
        prs=["strands-agents/b"],
        reviews=["example-maintainer/c"],
        commits=["example-maintainer/a", "evil/d"],
    )
    assert audit.extract_repos_from_contributions(viewer) == {
        "example-maintainer/a",
        "strands-agents/b",
        "example-maintainer/c",
        "evil/d",
    }


@pytest.mark.parametrize("viewer", [None, {}, {"contributionsCollection": None}, "nope", 5])
def test_extract_contributions_is_defensive(viewer):
    assert audit.extract_repos_from_contributions(viewer) == set()


def test_extract_contributions_skips_malformed_nodes():
    cc = {
        "contributionsCollection": {
            "issueContributions": {"nodes": [{"issue": None}, {}, {"issue": {"repository": {}}}]},
            "pullRequestContributions": {"nodes": "not-a-list"},
            "commitContributionsByRepository": [{"repository": {"nameWithOwner": "ok/x"}}, {}],
        }
    }
    assert audit.extract_repos_from_contributions(cc) == {"ok/x"}


# ---------------------------------------------------------------------------
# Pure extractor: events backstop
# ---------------------------------------------------------------------------
def test_extract_events_keeps_writes_in_window():
    events = [
        {"type": "IssueCommentEvent", "repo": {"name": "example-maintainer/a"}, "created_at": "2026-06-27T11:00:00Z"},
        {"type": "WatchEvent", "repo": {"name": "example-maintainer/star"}, "created_at": "2026-06-27T11:00:00Z"},
        {"type": "PushEvent", "repo": {"name": "evil/x"}, "created_at": "2026-06-20T00:00:00Z"},
    ]
    since = NOW.replace(hour=0)  # 2026-06-27T00:00
    repos = audit.extract_repos_from_events(events, since=since)
    assert repos == {"example-maintainer/a"}  # WatchEvent excluded (not a write), evil/x excluded (too old)


def test_extract_events_keeps_unparseable_timestamp_failopen():
    events = [{"type": "IssuesEvent", "repo": {"name": "evil/x"}, "created_at": "garbage"}]
    assert audit.extract_repos_from_events(events, since=NOW) == {"evil/x"}


@pytest.mark.parametrize("events", [None, "x", 7, [{"type": "PushEvent"}], [{"repo": {}}]])
def test_extract_events_defensive(events):
    assert audit.extract_repos_from_events(events, since=NOW) == set()


# ---------------------------------------------------------------------------
# Violation logic
# ---------------------------------------------------------------------------
def test_find_violations_flags_out_of_org_case_insensitive():
    repos = {"example-maintainer/a", "example-maintainer/b", "Strands-Agents/c", "evil/d", "other/e"}
    assert audit.find_violations(repos, {"example-maintainer", "strands-agents"}) == ["evil/d", "other/e"]


def test_find_violations_empty_allowlist_returns_empty_not_everything():
    # Documented guard: an empty allow-list flags nothing (the gate prevents this in practice).
    assert audit.find_violations({"evil/x"}, set()) == []


def test_find_violations_all_in_org():
    assert audit.find_violations({"example-maintainer/a"}, {"example-maintainer"}) == []


def test_find_violations_honours_repo_glob_entries():
    # an allowed repo glob must not be reported as an out-of-org write.
    allowed = {"strands-agents", "aws/bedrock-agentcore-*"}
    repos = {"aws/bedrock-agentcore-sdk-python", "AWS/Bedrock-AgentCore-Starter", "aws/other", "x/y"}
    assert audit.find_violations(repos, allowed) == ["aws/other", "x/y"]


@pytest.mark.parametrize(
    "repo",
    ["strands-agents/x", "STRANDS-AGENTS/y", "aws/bedrock-agentcore-a", "aws/other", "example-maintainer/z",
     "evil/bedrock-agentcore-a", "aws/bedrock-agentcore", "a/b"],
)
def test_repo_matching_has_parity_with_the_use_github_guardrail(repo):
    from strandly.tools.github import _target_allowed

    allowed = {"strands-agents", "aws/bedrock-agentcore-*", "example-maintainer"}
    assert audit.repo_allowed(repo, allowed) is _target_allowed(repo, allowed)


# ---------------------------------------------------------------------------
# Orchestration (monkeypatched network seams)
# ---------------------------------------------------------------------------
def test_audit_unions_sources_and_flags_violation(monkeypatch):
    monkeypatch.setattr(
        audit, "_graphql",
        lambda q, v, t: {"data": {"viewer": _viewer(prs=["example-maintainer/a", "evil/x"])}},
    )
    monkeypatch.setattr(
        audit, "_rest_get",
        lambda p, t: [
            {"type": "IssueCommentEvent", "repo": {"name": "other/y"}, "created_at": "2026-06-27T11:00:00Z"}
        ],
    )
    report = audit.audit(_settings(), now=NOW)
    assert report.login == "example-user"
    assert report.checked_repos == ["evil/x", "example-maintainer/a", "other/y"]
    assert report.violations == ["evil/x", "other/y"]
    assert report.errors == []
    assert report.ok is False


def test_audit_clean_when_all_in_org(monkeypatch):
    monkeypatch.setattr(audit, "_graphql", lambda q, v, t: {"data": {"viewer": _viewer(prs=["example-maintainer/a"])}})
    monkeypatch.setattr(audit, "_rest_get", lambda p, t: [])
    report = audit.audit(_settings(), now=NOW)
    assert report.violations == []
    assert report.ok is True


def test_audit_failsoft_on_graphql_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("graphql down")

    monkeypatch.setattr(audit, "_graphql", boom)
    monkeypatch.setattr(audit, "_rest_get", lambda p, t: [])
    report = audit.audit(_settings(), now=NOW)
    assert any("contributions fetch failed" in e for e in report.errors)
    assert report.checked_repos == []  # no crash, just no data from the failed source


def test_audit_records_graphql_field_errors(monkeypatch):
    monkeypatch.setattr(audit, "_graphql", lambda q, v, t: {"errors": [{"message": "bad"}], "data": {"viewer": None}})
    monkeypatch.setattr(audit, "_rest_get", lambda p, t: [])
    report = audit.audit(_settings(), now=NOW)
    assert any("GraphQL errors" in e for e in report.errors)


def test_audit_events_skipped_without_login(monkeypatch):
    # GraphQL returns no viewer/login → the events backstop can't run (needs the login).
    monkeypatch.setattr(audit, "_graphql", lambda q, v, t: {"data": {"viewer": None}})
    called = {"n": 0}

    def rest(*a, **k):
        called["n"] += 1
        return []

    monkeypatch.setattr(audit, "_rest_get", rest)
    report = audit.audit(_settings(), now=NOW)
    assert called["n"] == 0
    assert report.login is None


def test_audit_no_token_is_error():
    report = audit.audit(_settings(token=None), now=NOW)
    assert report.violations == []
    assert any("no audit token" in e for e in report.errors)


# ---------------------------------------------------------------------------
# notify (SNS) gating
# ---------------------------------------------------------------------------
def test_notify_noop_without_topic():
    report = audit.AuditReport(violations=["evil/x"])
    assert audit.notify(report, _settings(sns_topic_arn=None)) is False


def test_notify_noop_without_violations():
    report = audit.AuditReport(violations=[])
    assert audit.notify(report, _settings(sns_topic_arn="arn:aws:sns:::t")) is False


def test_notify_publishes_when_configured(monkeypatch):
    published = {}

    class FakeSNS:
        def publish(self, **kw):
            published.update(kw)
            return {"MessageId": "1"}

    import boto3

    monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeSNS())
    report = audit.AuditReport(login="agent", violations=["evil/x"], checked_repos=["evil/x"])
    assert audit.notify(report, _settings(sns_topic_arn="arn:aws:sns:::t", region="us-west-2")) is True
    assert published["TopicArn"] == "arn:aws:sns:::t"
    assert "evil/x" in published["Message"]
    assert len(published["Subject"]) <= 100


def test_notify_failsoft_on_publish_error(monkeypatch):
    class FakeSNS:
        def publish(self, **kw):
            raise RuntimeError("sns down")

    import boto3

    monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeSNS())
    report = audit.AuditReport(violations=["evil/x"])
    assert audit.notify(report, _settings(sns_topic_arn="arn:aws:sns:::t")) is False


# ---------------------------------------------------------------------------
# lambda_handler gating
# ---------------------------------------------------------------------------
def test_handler_disabled_without_config(monkeypatch):
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: Config(values={})))
    assert audit.lambda_handler({}) == {"status": "disabled"}


def test_handler_runs_when_enabled(monkeypatch):
    cfg = Config(values={"STRANDLY_AUDIT_ALLOWED_OWNERS": "example-maintainer", "STRANDLY_AUDIT_TOKEN": "t"})
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(audit, "_graphql", lambda q, v, t: {"data": {"viewer": _viewer(prs=["evil/x"])}})
    monkeypatch.setattr(audit, "_rest_get", lambda p, t: [])
    out = audit.lambda_handler({})
    assert out["status"] == "ok"
    assert out["report"]["violations"] == ["evil/x"]


def test_handler_warns_on_degraded_audit_not_clean(monkeypatch, caplog):
    # Security invariant: a fully-failed pass (both sources error -> 0 repos, 0 violations) must NOT
    # be reported as a silent "clean". The handler logs a WARNING ("degraded") so an operator/alarm
    # can tell "couldn't check" apart from "checked and found nothing".
    import logging

    cfg = Config(values={"STRANDLY_AUDIT_ALLOWED_OWNERS": "example-maintainer", "STRANDLY_AUDIT_TOKEN": "t"})
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(audit, "_graphql", boom)
    monkeypatch.setattr(audit, "_rest_get", boom)
    with caplog.at_level(logging.WARNING, logger="strandly.ops.lambdas.mention_poller.audit"):
        out = audit.lambda_handler({})
    assert out["status"] == "ok"
    assert out["report"]["violations"] == []  # nothing found...
    assert out["report"]["errors"]  # ...but only because the sources errored
    assert any("degraded" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Config wiring
# ---------------------------------------------------------------------------
def test_config_audit_settings_parsed():
    cfg = Config(
        values={
            "STRANDLY_AUDIT_ALLOWED_OWNERS": " example-maintainer , strands-agents ",
            "STRANDLY_AUDIT_TOKEN": "audit-tok",
            "STRANDLY_AUDIT_SNS_TOPIC_ARN": "arn:aws:sns:::t",
            "STRANDLY_AUDIT_LOOKBACK_HOURS": "6",
            "AWS_REGION": "us-west-2",
        }
    )
    s = cfg.audit
    assert s.allowed_owners == ("example-maintainer", "strands-agents")
    assert s.token == "audit-tok"
    assert s.sns_topic_arn == "arn:aws:sns:::t"
    assert s.lookback_hours == 6
    assert s.region == "us-west-2"
    assert cfg.audit_enabled is True


def test_config_audit_token_falls_back_to_github_token():
    cfg = Config(values={"STRANDLY_AUDIT_ALLOWED_OWNERS": "example-maintainer", "STRANDLY_GITHUB_TOKEN": "gh"})
    assert cfg.audit.token == "gh"
    assert cfg.audit_enabled is True


def test_config_audit_disabled_without_token():
    # allowed_owners can no longer be empty in practice: audit.allowed_owners falls back to
    # github.allowed_owners, which itself defaults to STRANDS_ORG_OWNERS — so the gate is
    # effectively token-only. No token -> disabled regardless of owners.
    assert Config(values={}).audit_enabled is False
    assert Config(values={"STRANDLY_AUDIT_ALLOWED_OWNERS": "example-maintainer"}).audit_enabled is False  # no token


def test_config_audit_allowed_owners_falls_back_to_github_allowed_owners():
    """One STRANDLY_ALLOWED_OWNERS edit must keep the write guardrail and the audit in sync.

    Regression: the audit used to read ONLY its own STRANDLY_AUDIT_ALLOWED_OWNERS key, which was
    never set for prod — the audit stack was effectively unusable there (an operator editing
    STRANDLY_ALLOWED_OWNERS, the write guardrail's list, would have no reason to expect a second,
    silently-unset key elsewhere needed updating too).
    """
    cfg = Config(
        values={
            "STRANDLY_ALLOWED_OWNERS": "strands-agents,strands-labs,aws/bedrock-agentcore-*",
            "STRANDLY_AUDIT_TOKEN": "t",
        }
    )
    assert cfg.audit.allowed_owners == cfg.github.allowed_owners
    assert cfg.audit.allowed_owners == ("strands-agents", "strands-labs", "aws/bedrock-agentcore-*")


def test_config_audit_allowed_owners_explicit_override_wins():
    cfg = Config(
        values={
            "STRANDLY_ALLOWED_OWNERS": "strands-agents",
            "STRANDLY_AUDIT_ALLOWED_OWNERS": "example-maintainer",
            "STRANDLY_AUDIT_TOKEN": "t",
        }
    )
    assert cfg.audit.allowed_owners == ("example-maintainer",)
    assert cfg.github.allowed_owners == ("strands-agents",)


def test_config_audit_lookback_defaults_on_garbage():
    cfg = Config(values={"STRANDLY_AUDIT_LOOKBACK_HOURS": "not-a-number"})
    assert cfg.audit.lookback_hours == 24


# ---------------------------------------------------------------------------
# Events-feed pagination + saturation (audit finding #7: single page was floodable)
# ---------------------------------------------------------------------------
def _write_event(repo: str, created: str = "2026-06-27T11:30:00Z") -> dict:
    return {"type": "IssueCommentEvent", "repo": {"name": repo}, "created_at": created}


def _paged_rest(pages: dict[int, object], calls: list[str]):
    """A ``_rest_get`` stub serving per-page payloads (raises when the value is an Exception)."""

    def rest(path: str, token: str):
        calls.append(path)
        page = int(path.rsplit("page=", 1)[1])
        payload = pages.get(page, [])
        if isinstance(payload, Exception):
            raise payload
        return payload

    return rest


def test_gather_events_unions_multiple_pages(monkeypatch):
    # A violation buried past page 1 (the flood-attack shape) must still be seen.
    page1 = [_write_event("example-maintainer/noise") for _ in range(100)]  # full page of innocuous noise
    page2 = [_write_event("evil/buried")]  # partial page → feed ends here
    calls: list[str] = []
    monkeypatch.setattr(audit, "_rest_get", _paged_rest({1: page1, 2: page2}, calls))
    errors: list[str] = []
    repos = audit.gather_events("agent", "t", since=NOW.replace(hour=0), errors=errors)
    assert repos == {"example-maintainer/noise", "evil/buried"}
    assert len(calls) == 2  # partial page 2 stops the walk
    assert errors == []  # feed end reached inside the cap → not saturated


def test_gather_events_early_stops_when_window_covered(monkeypatch):
    # Newest-first feed: an event older than `since` on page 1 proves the window is fully in
    # view — deeper pages are older still and must not be fetched (no wasted rate limit).
    page1 = [_write_event("example-maintainer/a") for _ in range(99)]
    page1.append(_write_event("example-maintainer/old", created="2026-06-20T00:00:00Z"))  # pre-window
    calls: list[str] = []
    monkeypatch.setattr(audit, "_rest_get", _paged_rest({1: page1}, calls))
    errors: list[str] = []
    repos = audit.gather_events("agent", "t", since=NOW.replace(hour=0), errors=errors)
    assert repos == {"example-maintainer/a"}
    assert len(calls) == 1  # early stop despite a full page
    assert errors == []


def test_gather_events_saturation_is_audit_error(monkeypatch):
    # 3 full pages all inside the window = the feed's hard cap reached without covering the
    # window. A flooded feed must surface as an ERROR (degraded pass), never a silent "clean".
    full = [_write_event("example-maintainer/flood") for _ in range(100)]
    calls: list[str] = []
    monkeypatch.setattr(audit, "_rest_get", _paged_rest({1: full, 2: full, 3: full}, calls))
    errors: list[str] = []
    repos = audit.gather_events("agent", "t", since=NOW.replace(hour=0), errors=errors)
    assert repos == {"example-maintainer/flood"}
    assert len(calls) == 3  # bounded at the API max — no hot loop past the cap
    assert any("saturated" in e for e in errors)


def test_gather_events_partial_failure_keeps_earlier_pages(monkeypatch):
    # Fail-soft: a page-2 failure records an error but keeps page 1's repos (partial > none).
    page1 = [_write_event("example-maintainer/kept") for _ in range(100)]
    calls: list[str] = []
    monkeypatch.setattr(
        audit, "_rest_get", _paged_rest({1: page1, 2: RuntimeError("boom")}, calls)
    )
    errors: list[str] = []
    repos = audit.gather_events("agent", "t", since=NOW.replace(hour=0), errors=errors)
    assert repos == {"example-maintainer/kept"}
    assert any("events fetch failed (page 2)" in e for e in errors)


def test_audit_flags_events_saturation_as_degraded(monkeypatch):
    # End-to-end: a saturated events feed lands in report.errors, so the lambda handler's
    # existing "degraded" warning path fires instead of reporting a clean pass.
    monkeypatch.setattr(
        audit, "_graphql", lambda q, v, t: {"data": {"viewer": _viewer(prs=["example-maintainer/a"])}}
    )
    full = [_write_event("example-maintainer/flood") for _ in range(100)]
    calls: list[str] = []
    monkeypatch.setattr(audit, "_rest_get", _paged_rest({1: full, 2: full, 3: full}, calls))
    report = audit.audit(_settings(), now=NOW)
    assert report.violations == []
    assert any("saturated" in e for e in report.errors)


# ---------------------------------------------------------------------------
# Contributions saturation (totalCount > fetched → truncated tail must be visible)
# ---------------------------------------------------------------------------
def test_contribution_saturation_flags_truncated_sections():
    viewer = _viewer(issues=["example-maintainer/a"], commits=["example-maintainer/b"])
    cc = viewer["contributionsCollection"]
    cc["issueContributions"]["totalCount"] = 150  # 150 total, 1 fetched → truncated
    cc["pullRequestContributions"]["totalCount"] = 0  # consistent → no error
    cc["totalRepositoriesWithContributedCommits"] = 120  # 120 repos, 1 fetched → truncated
    errors = audit.contribution_saturation_errors(viewer)
    assert len(errors) == 2
    assert any("issueContributions" in e for e in errors)
    assert any("commitContributionsByRepository" in e for e in errors)


def test_contribution_saturation_silent_without_totalcount():
    # Back-compat / defensive: payloads without totalCount (old fixtures, partial responses)
    # produce no saturation errors — the check is purely additive.
    assert audit.contribution_saturation_errors(_viewer(issues=["example-maintainer/a"])) == []
    assert audit.contribution_saturation_errors(None) == []
    assert audit.contribution_saturation_errors({"contributionsCollection": "nope"}) == []


def test_audit_records_contribution_saturation(monkeypatch):
    viewer = _viewer(prs=["example-maintainer/a"])
    viewer["contributionsCollection"]["pullRequestContributions"]["totalCount"] = 500
    monkeypatch.setattr(audit, "_graphql", lambda q, v, t: {"data": {"viewer": viewer}})
    monkeypatch.setattr(audit, "_rest_get", lambda p, t: [])
    report = audit.audit(_settings(), now=NOW)
    assert any("contributions saturated" in e for e in report.errors)
    assert report.checked_repos == ["example-maintainer/a"]  # extraction still works alongside the flag


def test_gather_events_non_list_payload_records_error_keeps_earlier_pages(monkeypatch):
    # A non-list payload (HTML error page decoded as dict, etc.) mid-walk records an error
    # and keeps earlier pages' repos — same fail-soft shape as a raised exception.
    page1 = [_write_event("example-maintainer/kept") for _ in range(100)]
    calls: list[str] = []
    monkeypatch.setattr(
        audit, "_rest_get", _paged_rest({1: page1, 2: {"message": "oops"}}, calls)
    )
    errors: list[str] = []
    repos = audit.gather_events("agent", "t", since=NOW.replace(hour=0), errors=errors)
    assert repos == {"example-maintainer/kept"}
    assert any("non-list payload (page 2)" in e for e in errors)


def test_gather_events_naive_since_does_not_raise(monkeypatch):
    # Contract: gather_events never raises (errors-list only). A naive `since` must be
    # normalized to UTC instead of raising TypeError on aware-vs-naive comparison against
    # GitHub's always-aware timestamps.
    from datetime import datetime as _datetime

    page1 = [_write_event("example-maintainer/a"), _write_event("example-maintainer/old", created="2026-06-20T00:00:00Z")]
    calls: list[str] = []
    monkeypatch.setattr(audit, "_rest_get", _paged_rest({1: page1}, calls))
    errors: list[str] = []
    repos = audit.gather_events("agent", "t", since=_datetime(2026, 6, 27, 0, 0), errors=errors)
    assert repos == {"example-maintainer/a"}
    assert errors == []


# ---------------------------------------------------------------------------
# Discussions (no contribution kind and no event for comments covers them)
# ---------------------------------------------------------------------------
IN_WINDOW = "2026-06-27T10:00:00Z"
BEFORE_WINDOW = "2026-06-20T10:00:00Z"
SINCE = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)


def _comment(repo, created=IN_WINDOW, updated=None):
    return {
        "createdAt": created,
        "updatedAt": updated or created,
        "discussion": {"repository": {"nameWithOwner": repo}},
    }


def _discussion(repo, created=IN_WINDOW, updated=None):
    return {"createdAt": created, "updatedAt": updated or created, "repository": {"nameWithOwner": repo}}


def _discussion_viewer(*, head=(), tail=(), total=None, discussions=(), discussions_total=None):
    viewer = _viewer()
    viewer["discussionCommentsHead"] = {
        "totalCount": len(head) if total is None else total,
        "nodes": list(head),
    }
    viewer["discussionCommentsTail"] = {"nodes": list(tail)}
    viewer["repositoryDiscussions"] = {
        "totalCount": len(discussions) if discussions_total is None else discussions_total,
        "nodes": list(discussions),
    }
    return viewer


def test_write_event_types_match_the_throttle():
    from strandly.tools import github_client

    assert set(audit.WRITE_EVENT_TYPES) == github_client._THROTTLED_EVENT_TYPES
    assert "DiscussionEvent" in audit.WRITE_EVENT_TYPES


def test_discussions_query_asks_for_discussion_writes():
    for name in ("repositoryDiscussionComments(first: 100)", "repositoryDiscussionComments(last: 100)",
                 "repositoryDiscussions(first: 100"):
        assert name in audit._DISCUSSIONS_QUERY
        # Kept out of the contributions request: a discussions failure must not null its viewer.
        assert name not in audit._CONTRIBUTIONS_QUERY


def test_discussions_failure_keeps_contributions_and_events(monkeypatch):
    """A token that cannot read discussions still gets the contributions and events audit."""

    def graphql(q, v, t):
        if q is audit._DISCUSSIONS_QUERY:
            return {"errors": [{"type": "FORBIDDEN", "message": "no discussions"}], "data": None}
        return {"data": {"viewer": _viewer(prs=["evil/x"])}}

    monkeypatch.setattr(audit, "_graphql", graphql)
    monkeypatch.setattr(
        audit, "_rest_get",
        lambda p, t: [
            {"type": "IssueCommentEvent", "repo": {"name": "other/y"}, "created_at": "2026-06-27T11:00:00Z"}
        ],
    )
    report = audit.audit(_settings(), now=NOW)
    assert report.login == "example-user"
    assert report.violations == ["evil/x", "other/y"]
    assert any("discussions GraphQL errors" in e for e in report.errors)
    assert any("discussion writes were not audited" in e for e in report.errors)
    assert report.ok is False


def test_discussion_comment_outside_allow_list_is_a_violation(monkeypatch):
    def graphql(q, v, t):
        if q is audit._DISCUSSIONS_QUERY:
            return {"data": {"viewer": _discussion_viewer(head=[_comment("evil/discuss", created=IN_WINDOW)])}}
        return {"data": {"viewer": _viewer(prs=["example-maintainer/a"])}}

    monkeypatch.setattr(audit, "_graphql", graphql)
    monkeypatch.setattr(audit, "_rest_get", lambda p, t: [])
    report = audit.audit(_settings(), now=NOW)
    assert report.violations == ["evil/discuss"]


def test_extract_discussion_repos_keeps_only_window_activity():
    viewer = _discussion_viewer(
        head=[_comment("evil/old", created=BEFORE_WINDOW), _comment("evil/edited", BEFORE_WINDOW, IN_WINDOW)],
        tail=[_comment("evil/new")],
        discussions=[_discussion("evil/started"), _discussion("evil/ancient", BEFORE_WINDOW)],
    )
    assert audit.extract_repos_from_discussions(viewer, since=SINCE) == {
        "evil/edited", "evil/new", "evil/started",
    }


def test_extract_discussion_repos_keeps_undated_nodes_and_tolerates_shapes():
    viewer = {
        "discussionCommentsHead": {"nodes": [{"discussion": {"repository": {"nameWithOwner": "evil/undated"}}}, None, {}]},
        "discussionCommentsTail": "garbage",
        "repositoryDiscussions": {"nodes": [{"repository": None}]},
    }
    assert audit.extract_repos_from_discussions(viewer, since=SINCE) == {"evil/undated"}
    assert audit.extract_repos_from_discussions(None, since=SINCE) == set()


def test_discussion_saturation_flags_hidden_in_window_middle():
    head = [_comment("a/a", BEFORE_WINDOW)] * 100
    tail = [_comment("a/a")] * 100
    viewer = _discussion_viewer(head=head, tail=tail, total=500)
    errors = audit.discussion_saturation_errors(viewer, since=SINCE)
    assert any("discussion comments saturated" in e for e in errors)


def test_discussion_saturation_silent_when_middle_is_outside_window():
    head = [_comment("a/a", BEFORE_WINDOW)] * 100
    tail = [_comment("a/a", BEFORE_WINDOW)] * 99 + [_comment("a/a")]
    viewer = _discussion_viewer(head=head, tail=tail, total=500)
    assert audit.discussion_saturation_errors(viewer, since=SINCE) == []


def test_discussion_saturation_silent_when_both_ends_cover_everything():
    viewer = _discussion_viewer(head=[_comment("a/a")] * 100, tail=[_comment("a/a")] * 100, total=150)
    assert audit.discussion_saturation_errors(viewer, since=SINCE) == []


def test_discussions_saturation_flags_truncated_tail_inside_window():
    viewer = _discussion_viewer(discussions=[_discussion("a/a")] * 100, discussions_total=101)
    errors = audit.discussion_saturation_errors(viewer, since=SINCE)
    assert any("discussions saturated" in e for e in errors)
    viewer = _discussion_viewer(
        discussions=[_discussion("a/a")] * 99 + [_discussion("a/a", BEFORE_WINDOW)], discussions_total=101
    )
    assert audit.discussion_saturation_errors(viewer, since=SINCE) == []


def test_audit_flags_out_of_org_discussion_comment(monkeypatch):
    viewer = _discussion_viewer(tail=[_comment("evil/discussions")])
    monkeypatch.setattr(audit, "_graphql", lambda q, v, t: {"data": {"viewer": viewer}})
    monkeypatch.setattr(audit, "_rest_get", lambda p, t: [])
    report = audit.audit(_settings(), now=NOW)
    assert report.violations == ["evil/discussions"]
    assert report.errors == []


def test_events_backstop_counts_discussion_creation():
    events = [{"type": "DiscussionEvent", "repo": {"name": "evil/d"}, "created_at": IN_WINDOW}]
    assert audit.extract_repos_from_events(events, since=SINCE) == {"evil/d"}
