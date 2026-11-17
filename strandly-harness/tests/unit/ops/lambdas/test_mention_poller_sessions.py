"""The canonical, ingress-agnostic session-id scheme (strandly_harness.ops.lambdas.mention_poller.sessions)."""

from __future__ import annotations

import json

import pytest

from strandly_harness.ops.lambdas.mention_poller import handler as mentions
from strandly_harness.ops.lambdas.mention_poller.sessions import (
    KIND_DISCUSSION,
    KIND_ISSUE,
    KIND_PR,
    canonical_session_id,
    kind_for_subject_type,
    session_id_from_github_event,
)

_ENV = ("SESSION_ID", "GITHUB_CONTEXT", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)


def test_canonical_format_per_kind():
    assert canonical_session_id("o/r", KIND_ISSUE, 7) == "gh-o-r-issue-7"
    assert canonical_session_id("o/r", KIND_PR, 42) == "gh-o-r-pr-42"
    assert canonical_session_id("mkmeral/strandly-harness", KIND_DISCUSSION, 9) == (
        "gh-mkmeral-strandly-harness-disc-9"
    )


def test_subject_type_mapping_with_issue_default():
    assert kind_for_subject_type("PullRequest") == KIND_PR
    assert kind_for_subject_type("Issue") == KIND_ISSUE
    assert kind_for_subject_type("Discussion") == KIND_DISCUSSION
    assert kind_for_subject_type(None) == KIND_ISSUE  # unknown/None -> issue


@pytest.mark.parametrize(
    "event_name,key,expected",
    [
        ("issue_comment", "issue", "gh-o-r-issue-5"),
        ("issues", "issue", "gh-o-r-issue-5"),
        ("pull_request", "pull_request", "gh-o-r-pr-5"),
        ("pull_request_review_comment", "pull_request", "gh-o-r-pr-5"),
        ("discussion_comment", "discussion", "gh-o-r-disc-5"),
    ],
)
def test_event_scopes_to_item(event_name, key, expected):
    ctx = {"repository": "o/r", "event_name": event_name, "event": {key: {"number": 5}}}
    assert session_id_from_github_event(ctx) == expected


def test_pr_comment_via_issue_comment_event_scopes_to_pr():
    # A comment on a PR arrives as issue_comment with event.issue.pull_request set; it must thread
    # with the PR (gh-...-pr-N), matching the mention poller — not split off as gh-...-issue-N.
    ctx = {
        "repository": "o/r",
        "event_name": "issue_comment",
        "event": {"issue": {"number": 42, "pull_request": {"url": "..."}}},
    }
    assert session_id_from_github_event(ctx) == "gh-o-r-pr-42"


def test_explicit_session_id_env_overrides_everything(monkeypatch):
    monkeypatch.setenv("SESSION_ID", "manual-override")
    ctx = {"repository": "o/r", "event_name": "issues", "event": {"issue": {"number": 5}}}
    assert session_id_from_github_event(ctx) == "manual-override"


def test_itemless_event_falls_back_to_ephemeral_run_id():
    ctx = {"repository": "o/r", "event_name": "workflow_dispatch", "run_id": "999", "event": {}}
    assert session_id_from_github_event(ctx) == "gh-o-r-999"


def test_reads_github_context_env_when_no_arg(monkeypatch):
    ctx = {"repository": "o/r", "event_name": "issues", "event": {"issue": {"number": 8}}}
    monkeypatch.setenv("GITHUB_CONTEXT", json.dumps(ctx))
    assert session_id_from_github_event() == "gh-o-r-issue-8"


def test_malformed_context_degrades_to_ephemeral(monkeypatch):
    monkeypatch.setenv("GITHUB_CONTEXT", "{not json")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "7")
    assert session_id_from_github_event() == "gh-o-r-7"


def test_mention_poller_uses_the_same_canonical_scheme():
    # The poller's id and an Actions invoke's id must be identical for the same item.
    assert mentions.build_session_id("o/r", True, 42) == canonical_session_id("o/r", KIND_PR, 42)
    assert mentions.build_session_id("o/r", False, 7) == canonical_session_id("o/r", KIND_ISSUE, 7)
