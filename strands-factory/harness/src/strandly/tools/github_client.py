"""GitHub HTTP, token identity, repository resolution, and activity-feed reads."""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from strandly.ops.github import open_github

if TYPE_CHECKING:
    from strandly.core.config import GitHubSettings

logger = logging.getLogger("strandly.tools.github")


GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"


GITHUB_REST_URL = "https://api.github.com"


_USER_AGENT = "meta-harness-github-tool/1.0"


def _request(method: str, url: str, token: str | None, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", _USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    # An empty/None token makes this an *anonymous* request — GitHub's REST v3 serves public
    # issues/PRs that way (GraphQL has no anon tier), which is what lets the context injector enrich
    # public threads without requiring a token. ``open_github`` keeps the token on a same-origin
    # redirect (a renamed repository) and drops it on any other host: GitHub 302s Actions
    # logs/artifacts to pre-signed blob-storage URLs that reject a request carrying it.
    with open_github(req, token, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _graphql(query: str, variables: dict[str, Any], token: str | None) -> dict[str, Any]:
    return _request("POST", GITHUB_GRAPHQL_URL, token, {"query": query, "variables": variables})


def _rest_get(path: str, token: str | None = None) -> Any:
    return _request("GET", f"{GITHUB_REST_URL}{path}", token)


def _rest_call(
    method: str, path: str, token: str, *, body: dict[str, Any] | None = None, accept: str = ""
) -> Any:
    """One REST call against the fixed ``api.github.com`` base. Returns parsed JSON, else raw text.

    ``compare``/``.diff``/``.raw`` responses are not JSON, so a decode failure yields the text body
    rather than an error — that text *is* the useful payload for a diff.
    """
    req = urllib.request.Request(
        f"{GITHUB_REST_URL}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method.upper(),
    )
    req.add_header("Accept", accept or "application/vnd.github+json")
    req.add_header("User-Agent", _USER_AGENT)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    # The token stays on api.github.com, redirects included (see ``_request``).
    with open_github(req, token, timeout=30) as resp:
        text = resp.read().decode()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _get_token(gh: GitHubSettings, use_pat_token: bool) -> str | None:
    """Prefer the configured token, then non-empty environment tokens (PAT first if requested)."""
    if gh.token:
        return gh.token
    names = list(gh.token_env)
    if use_pat_token and "PAT_TOKEN" in names:
        names.remove("PAT_TOKEN")
        names.insert(0, "PAT_TOKEN")
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


_RESOLVE_QUERY = """
query($id: ID!) {
  node(id: $id) {
    __typename
    ... on PullRequest { repository { nameWithOwner } }
    ... on Issue { repository { nameWithOwner } }
    ... on IssueComment { repository { nameWithOwner } }
    ... on PullRequestReview { repository { nameWithOwner } }
    ... on PullRequestReviewComment { pullRequest { repository { nameWithOwner } } }
    ... on CommitComment { repository { nameWithOwner } }
    ... on Discussion { repository { nameWithOwner } }
    ... on DiscussionComment { discussion { repository { nameWithOwner } } }
    ... on Release { repository { nameWithOwner } }
    ... on Repository { nameWithOwner }
    ... on Label { repository { nameWithOwner } }
    ... on Milestone { repository { nameWithOwner } }
    ... on PullRequestReviewThread { repository { nameWithOwner } }
    ... on Ref { repository { nameWithOwner } }
    ... on DiscussionCategory { repository { nameWithOwner } }
  }
}
"""


def resolve_node_owner(node_id: str, token: str) -> str | None:
    """Resolve a node id to owner/repo; log and return None when it cannot be resolved."""
    try:
        data = _graphql(_RESOLVE_QUERY, {"id": node_id}, token)
    except Exception as e:  # noqa: BLE001 — resolution failure is non-fatal (caller decides)
        logger.warning("node id resolution failed for %s: %s", node_id, e)
        return None
    if "errors" in data:
        logger.warning("node id resolution errors for %s: %s", node_id, data["errors"])
        return None
    node = (data.get("data") or {}).get("node")
    if not node:
        # A missing or inaccessible node is unresolved, not an authorized target.
        logger.warning(
            "node id resolution returned no node for %s (unknown id, or not visible to this token)",
            node_id,
        )
        return None
    # ``__typename`` is always selected, so it is available to name an unhandled type below.
    typename = node.get("__typename") or "unknown"
    name_with_owner = None
    repo = node.get("repository")
    if isinstance(repo, dict):
        name_with_owner = repo.get("nameWithOwner")
    if not name_with_owner:
        for nested_key in ("pullRequest", "discussion"):
            nested = node.get(nested_key)
            if isinstance(nested, dict) and isinstance(nested.get("repository"), dict):
                name_with_owner = nested["repository"].get("nameWithOwner")
                break
    if not name_with_owner:
        name_with_owner = node.get("nameWithOwner")
    if isinstance(name_with_owner, str) and "/" in name_with_owner:
        return name_with_owner
    # Non-repo types and missing query fragments both leave ownership unresolved.
    # Include the type in the warning to distinguish them without changing strict checks.
    logger.warning(
        "node id %s resolved to type '%s' but no repository could be determined "
        "(not a repo-scoped type, or _RESOLVE_QUERY has no fragment for it) — "
        "this will block the mutation under strict mode",
        node_id,
        typename,
    )
    return None


def _query_viewer_login(token: str) -> str | None:
    """Query the token owner, returning None on API errors."""
    try:
        data = _graphql("query { viewer { login } }", {}, token)
        return ((data.get("data") or {}).get("viewer") or {}).get("login")
    except Exception:  # noqa: BLE001
        return None


def _viewer_login(token: str) -> str | None:
    """The authenticated user's login (the account whose events the throttle counts)."""
    env_owner = os.environ.get("GITHUB_REPOSITORY_OWNER")
    if env_owner:
        return env_owner
    return _query_viewer_login(token)


_self_login_cache: dict[str, str] = {}


def resolve_self_login(token: str | None) -> str | None:
    """Resolve prompt identity from the token, caching successes but not failures."""
    if not token:
        return None
    if token not in _self_login_cache:
        login = _query_viewer_login(token)
        if not login:
            return None
        _self_login_cache[token] = login
    return _self_login_cache[token]


# Mirrors ``ops/lambdas/mention_poller/audit.py::WRITE_EVENT_TYPES``; a parity test pins them.
# GitHub emits no event for discussion comments, so the throttle cannot count them.
_THROTTLED_EVENT_TYPES = {
    "IssueCommentEvent", "PullRequestReviewEvent", "PullRequestReviewCommentEvent",
    "IssuesEvent", "PullRequestEvent", "CommitCommentEvent", "CreateEvent",
    "DeleteEvent", "PushEvent", "DiscussionEvent", "ForkEvent",
}


_EVENTS_PER_PAGE = 100


_EVENTS_MAX_PAGES = 3


def count_external_writes(
    *, internal_owners: set[str], token: str, hours: int = 24
) -> int | None:
    """Count external writes in the last 24 hours from at most three event pages."""
    login = _viewer_login(token)
    if not login:
        return None
    threshold = datetime.now(timezone.utc) - timedelta(hours=hours)
    internal_lower = {o.lower() for o in internal_owners}
    count = 0
    crossed_threshold = False  # saw an event older than the window → feed fully covers it
    last_page_full = False
    for page in range(1, _EVENTS_MAX_PAGES + 1):
        try:
            events = _rest_get(
                f"/users/{login}/events?per_page={_EVENTS_PER_PAGE}&page={page}", token
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("throttle: events fetch failed (page %d): %s", page, e)
            if page == 1:
                return None  # nothing fetched → undetermined (caller fails open, as before)
            logger.warning(
                "throttle: partial events feed — external-write count %d is a floor", count
            )
            return count
        if not isinstance(events, list):
            if page == 1:
                return None
            logger.warning(
                "throttle: unexpected events payload (page %d) — count %d is a floor",
                page,
                count,
            )
            return count
        last_page_full = len(events) >= _EVENTS_PER_PAGE
        for event in events:
            if not isinstance(event, dict):
                continue
            created = event.get("created_at", "")
            try:
                when = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when < threshold:
                # Newest-first feed: this event (and everything deeper) predates the window.
                # Keep scanning the page (cheap, guards against slight ordering wobble at the
                # boundary) but don't fetch further pages.
                crossed_threshold = True
                continue
            if event.get("type") not in _THROTTLED_EVENT_TYPES:
                continue
            # Malformed events must not raise out of the fail-open throttle check.
            repo_obj = event.get("repo")
            repo = repo_obj.get("name") if isinstance(repo_obj, dict) else ""
            owner = repo.split("/")[0].lower() if isinstance(repo, str) and "/" in repo else ""
            if owner and owner not in internal_lower:
                count += 1
        if crossed_threshold or not last_page_full:
            break
    if last_page_full and not crossed_threshold:
        # Feed exhausted (3 full pages) while still inside the window: older write events are
        # invisible, so `count` is a lower bound. Surface the residual undercount instead of
        # letting the guardrail weaken silently.
        logger.warning(
            "throttle: events feed exhausted (%d pages / %d events) without crossing the "
            "%dh threshold — external-write count %d is a floor (real count may be higher)",
            _EVENTS_MAX_PAGES,
            _EVENTS_MAX_PAGES * _EVENTS_PER_PAGE,
            hours,
            count,
        )
    return count
