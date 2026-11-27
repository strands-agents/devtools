"""Session ids for ``strandly invoke`` runs inside GitHub Actions.

A workflow triggered by an issue, pull request or discussion joins that item's canonical thread
(:func:`strandly.core.session_ids.canonical_session_id`), the same one the poller and the CLI use.
Triggers without an item get an ephemeral ``gh-<owner>-<repo>-<h8>-<run_id>`` id.
"""

from __future__ import annotations

import json
import os
from typing import Any

from strandly.core.session_ids import (
    KIND_DISCUSSION,
    KIND_ISSUE,
    KIND_PR,
    canonical_session_id,
    repo_session_prefix,
)

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


def _ephemeral(repo: str, run_id: str | None) -> str:
    repo = repo or os.environ.get("GITHUB_REPOSITORY") or "local/local"
    run_id = run_id or os.environ.get("GITHUB_RUN_ID") or "local"
    return f"{repo_session_prefix(repo)}-{run_id}"


def session_id_from_github_event(
    context: dict[str, Any] | None = None,
    *,
    repo: str | None = None,
    run_id: str | None = None,
) -> str:
    """Derive the canonical session id from a GitHub Actions ``github`` context.

    Resolution order: ``SESSION_ID`` env override → the triggering item (issue/PR/discussion) →
    an ephemeral ``gh-<slug>-<h8>-<run_id>`` for item-less events. ``context`` defaults to the JSON in
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
