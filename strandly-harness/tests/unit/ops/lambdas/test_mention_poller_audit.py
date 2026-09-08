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

from strandly_harness.core.config import AuditSettings, Config
from strandly_harness.ops.lambdas.mention_poller import audit

NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)


def _settings(**kw) -> AuditSettings:
    base = dict(allowed_owners=("mkmeral", "strands-agents"), token="t", lookback_hours=24)
    base.update(kw)
    return AuditSettings(**base)


# ---------------------------------------------------------------------------
# Pure extractor: contributions
# ---------------------------------------------------------------------------
def _viewer(*, issues=(), prs=(), reviews=(), commits=()) -> dict:
    def repo_nodes(names, key):
        return {"nodes": [{key: {"repository": {"nameWithOwner": n}}} for n in names]}

    return {
        "login": "agent-of-mkmeral",
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
        issues=["mkmeral/a"],
        prs=["strands-agents/b"],
        reviews=["mkmeral/c"],
        commits=["mkmeral/a", "evil/d"],
    )
    assert audit.extract_repos_from_contributions(viewer) == {
        "mkmeral/a",
        "strands-agents/b",
        "mkmeral/c",
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
        {"type": "IssueCommentEvent", "repo": {"name": "mkmeral/a"}, "created_at": "2026-06-27T11:00:00Z"},
        {"type": "WatchEvent", "repo": {"name": "mkmeral/star"}, "created_at": "2026-06-27T11:00:00Z"},
        {"type": "PushEvent", "repo": {"name": "evil/x"}, "created_at": "2026-06-20T00:00:00Z"},
    ]
    since = NOW.replace(hour=0)  # 2026-06-27T00:00
    repos = audit.extract_repos_from_events(events, since=since)
    assert repos == {"mkmeral/a"}  # WatchEvent excluded (not a write), evil/x excluded (too old)


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
    repos = {"mkmeral/a", "MKMERAL/b", "Strands-Agents/c", "evil/d", "other/e"}
    assert audit.find_violations(repos, {"mkmeral", "strands-agents"}) == ["evil/d", "other/e"]


def test_find_violations_empty_allowlist_returns_empty_not_everything():
    # Documented guard: an empty allow-list flags nothing (the gate prevents this in practice).
    assert audit.find_violations({"evil/x"}, set()) == []


def test_find_violations_all_in_org():
    assert audit.find_violations({"mkmeral/a"}, {"mkmeral"}) == []


# ---------------------------------------------------------------------------
# Orchestration (monkeypatched network seams)
# ---------------------------------------------------------------------------
def test_audit_unions_sources_and_flags_violation(monkeypatch):
    monkeypatch.setattr(
        audit, "_graphql",
        lambda q, v, t: {"data": {"viewer": _viewer(prs=["mkmeral/a", "evil/x"])}},
    )
    monkeypatch.setattr(
        audit, "_rest_get",
        lambda p, t: [
            {"type": "IssueCommentEvent", "repo": {"name": "other/y"}, "created_at": "2026-06-27T11:00:00Z"}
        ],
    )
    report = audit.audit(_settings(), now=NOW)
    assert report.login == "agent-of-mkmeral"
    assert report.checked_repos == ["evil/x", "mkmeral/a", "other/y"]
    assert report.violations == ["evil/x", "other/y"]
    assert report.errors == []
    assert report.ok is False


def test_audit_clean_when_all_in_org(monkeypatch):
    monkeypatch.setattr(audit, "_graphql", lambda q, v, t: {"data": {"viewer": _viewer(prs=["mkmeral/a"])}})
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
    cfg = Config(values={"STRANDLY_AUDIT_ALLOWED_OWNERS": "mkmeral", "STRANDLY_AUDIT_TOKEN": "t"})
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

    cfg = Config(values={"STRANDLY_AUDIT_ALLOWED_OWNERS": "mkmeral", "STRANDLY_AUDIT_TOKEN": "t"})
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(audit, "_graphql", boom)
    monkeypatch.setattr(audit, "_rest_get", boom)
    with caplog.at_level(logging.WARNING, logger="strandly_harness.ops.lambdas.mention_poller.audit"):
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
            "STRANDLY_AUDIT_ALLOWED_OWNERS": " mkmeral , strands-agents ",
            "STRANDLY_AUDIT_TOKEN": "audit-tok",
            "STRANDLY_AUDIT_SNS_TOPIC_ARN": "arn:aws:sns:::t",
            "STRANDLY_AUDIT_LOOKBACK_HOURS": "6",
            "AWS_REGION": "us-west-2",
        }
    )
    s = cfg.audit
    assert s.allowed_owners == ("mkmeral", "strands-agents")
    assert s.token == "audit-tok"
    assert s.sns_topic_arn == "arn:aws:sns:::t"
    assert s.lookback_hours == 6
    assert s.region == "us-west-2"
    assert cfg.audit_enabled is True


def test_config_audit_token_falls_back_to_github_token():
    cfg = Config(values={"STRANDLY_AUDIT_ALLOWED_OWNERS": "mkmeral", "STRANDLY_GITHUB_TOKEN": "gh"})
    assert cfg.audit.token == "gh"
    assert cfg.audit_enabled is True


def test_config_audit_disabled_without_owners_or_token():
    assert Config(values={"STRANDLY_AUDIT_TOKEN": "t"}).audit_enabled is False  # no owners
    assert Config(values={"STRANDLY_AUDIT_ALLOWED_OWNERS": "mkmeral"}).audit_enabled is False  # no token


def test_config_audit_lookback_defaults_on_garbage():
    cfg = Config(values={"STRANDLY_AUDIT_LOOKBACK_HOURS": "not-a-number"})
    assert cfg.audit.lookback_hours == 24
