"""Canonical, GitHub-item-scoped session ids — one scheme for every ingress.

A conversation is keyed on the GitHub item it happens on (issue / PR / discussion), so the *same*
agent + AgentCore Memory thread is reused no matter how a run was triggered: the mention poller,
``strandly invoke`` from a GitHub Action, or a manual CLI call all derive the same id. Format::

    gh-<owner>-<repo>-<kind>-<number>      kind in {issue, pr, disc}

Non-item triggers (workflow_dispatch, schedule, push) have nothing to key on and fall back to an
ephemeral per-run id. An explicit ``SESSION_ID`` env var overrides everything.

This mirrors the sibling agent's ``generate_stable_session_id`` so a thread is portable across the
two harnesses.
"""

from __future__ import annotations

import json
import os
from typing import Any

KIND_ISSUE = "issue"
KIND_PR = "pr"
KIND_DISCUSSION = "disc"

# GitHub Actions event_name -> session kind.
_EVENT_KIND = {
    "issues": KIND_ISSUE,
    "issue_comment": KIND_ISSUE,
    "pull_request": KIND_PR,
    "pull_request_target": KIND_PR,
    "pull_request_review": KIND_PR,
    "pull_request_review_comment": KIND_PR,
    "discussion": KIND_DISCUSSION,
    "discussion_comment": KIND_DISCUSSION,
}

# GitHub notification subject "type" -> session kind (mention poller).
_SUBJECT_KIND = {
    "Issue": KIND_ISSUE,
    "PullRequest": KIND_PR,
    "Discussion": KIND_DISCUSSION,
}


def _slug(repo: str) -> str:
    return (repo or "").strip("/").replace("/", "-")


def canonical_session_id(repo: str, kind: str, number: int | str) -> str:
    """``gh-<owner>-<repo>-<kind>-<number>`` — the one scoped id every ingress builds."""
    return f"gh-{_slug(repo)}-{kind}-{number}"


def kind_for_subject_type(subject_type: str | None) -> str:
    """Map a GitHub notification subject ``type`` to a session kind (default: issue)."""
    return _SUBJECT_KIND.get(subject_type or "", KIND_ISSUE)


def _ephemeral(repo: str, run_id: str | None) -> str:
    repo = repo or os.environ.get("GITHUB_REPOSITORY") or "local/local"
    run_id = run_id or os.environ.get("GITHUB_RUN_ID") or "local"
    return f"gh-{_slug(repo)}-{run_id}"


def session_id_from_github_event(
    context: dict[str, Any] | None = None,
    *,
    repo: str | None = None,
    run_id: str | None = None,
) -> str:
    """Derive the canonical session id from a GitHub Actions ``github`` context.

    Resolution order: ``SESSION_ID`` env override → the triggering item (issue/PR/discussion) →
    an ephemeral ``gh-<slug>-<run_id>`` for item-less events. ``context`` defaults to the JSON in
    ``$GITHUB_CONTEXT`` (the ``toJSON(github)`` object an Actions step exports).
    """
    override = os.environ.get("SESSION_ID")
    if override:
        return override

    if context is None:
        raw = os.environ.get("GITHUB_CONTEXT")
        if raw:
            try:
                context = json.loads(raw)
            except (TypeError, ValueError):
                context = None
    context = context or {}

    repo = repo or context.get("repository") or os.environ.get("GITHUB_REPOSITORY") or ""
    run_id = run_id or context.get("run_id")
    kind = _EVENT_KIND.get(context.get("event_name", ""))
    if repo and kind:
        event = context.get("event") or {}
        item = event.get("issue") or event.get("pull_request") or event.get("discussion") or {}
        # A comment on a PR is delivered as an `issue_comment` whose issue payload carries a
        # `pull_request` key. Scope it to `pr` so it threads with the poller (subject=PullRequest)
        # and with native pull_request events — same item, same session, regardless of ingress.
        if kind == KIND_ISSUE and item.get("pull_request"):
            kind = KIND_PR
        number = item.get("number")
        if number is not None:
            return canonical_session_id(repo, kind, number)
    return _ephemeral(repo, run_id)
