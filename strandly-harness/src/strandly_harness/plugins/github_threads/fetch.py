"""GitHub URL → full enriched thread enrichment (the pure core behind the injector plugin).

The deployed runtime / mention poller hands the agent only a `prompt` + a *parent* issue/PR link
(and, for discussions, a wrong `/issues/N` URL). The thread itself — comments, reviews, review
threads with file:line, linked issues, discussion replies — is missing. This module closes that gap:
given one or more GitHub URLs it returns one markdown block per URL with the *full* enriched context,
matching the shape of the strands-coder reference (`strands_coder/context.py::fetch_github_event_context()`),
and **works for issues, PRs, AND discussions** (a discussion is never treated as an issue).

Surface — a pure core consumed by the GitHubContextInjector plugin (the only surface).
--------------------------------------------------------------------------------------
The enrichment lives in the pure function :func:`build_github_context`, which takes its ``graphql``
callable as a parameter, so any caller can reuse the exact same enrichment without going through a
tool (tests pass a fake ``graphql`` and stay network-free).

The **only** surface is the :class:`~strandly_harness.plugins.github_threads.plugin.
GitHubContextInjector` *plugin*, which **auto-injects** the enriched thread into the model's input
ephemerally (two hooks) at the turn boundary — no tool call required. This mirrors the TS
``ContextInjector`` vended plugin (issue #346 owner feedback: "why is this a tool?"). It reuses
:func:`build_github_context` unchanged.

There is deliberately **no dedicated ``inject_github_context`` tool**: the agent already has
``use_github`` (universal GraphQL access) for any URL it wants to fetch on demand mid-turn, so a
context-fetch tool would just duplicate it and burn a model turn (issue #346 owner feedback again:
"why the fuck do we still have a github context tool? we already have use_github").

Token-optional (issue #346 owner feedback: "why do we require a github token to see a public
issue/pr/discussion?"). GitHub's GraphQL API has **no anonymous tier** (it 403s even for public
data), so a token unlocks the *full* enrichment. Without one we **fall back to the REST v3 API**,
which serves public issues/PRs anonymously: body + comments (+ PR reviews & review comments with
file:line), plus a short note that the view is reduced. Discussions are GraphQL-only (absent from
REST), so without a token a discussion URL injects a short "needs a token" note. A token, when
present, is always used and nothing about the GraphQL path changes.

Robustness: every URL is handled independently and **fail-soft** — a malformed/non-GitHub URL or an
HTTP/GraphQL error injects a short note for that URL and never raises, so one bad link can't crash
the turn. Long bodies are truncated (`MAX_BODY_CHARS`) and the per-thread fan-out is capped.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

# A callable with the same shape as ``github._graphql(query, variables, token) -> dict``.
GraphQLFn = Callable[[str, dict[str, Any], str], dict[str, Any]]

# A callable with the same shape as ``github._rest_get(path, token) -> parsed JSON``. Used for the
# anonymous REST fallback when no token is configured (``token`` is passed as ``""``).
RestFn = Callable[[str, str], Any]

# --- size caps (mirror the harness's existing truncation discipline) ---------------------
MAX_BODY_CHARS = 2000  # cap each body/comment, like the existing prompt cap
MAX_COMMENTS = 100
MAX_REVIEWS = 50
MAX_REVIEW_THREADS = 50
MAX_THREAD_COMMENTS = 10
MAX_LINKED = 25
MAX_REPLIES = 20

# Notes used by the anonymous REST fallback (no token configured).
_REST_FALLBACK_NOTE = (
    "> ℹ️ Enriched anonymously via GitHub REST (no token configured) — a *reduced* view: "
    "cross-referenced/closing issues and review-thread *resolution* state are omitted. Configure "
    "a GitHub token for the full GraphQL enrichment."
)
_REST_NOT_FOUND = (
    "not found via anonymous REST (private repo, missing, or rate-limited) — configure a GitHub "
    "token for full enrichment"
)

# Path segment → canonical kind. Anything else is not an enrichable thread URL.
_SEGMENT_KIND = {
    "issues": "issue",
    "pull": "pull",
    "discussions": "discussion",
}

# URL fragments that pin a *specific* comment/review inside the thread, so we can deep-link the
# exact item that triggered the turn. Maps the fragment to a logical fragment kind.
_FRAGMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # PR/issue comment:               …#issuecomment-123456
    ("issuecomment", re.compile(r"issuecomment-(\d+)")),
    # PR review (the whole review):   …#pullrequestreview-123456
    ("review", re.compile(r"pullrequestreview-(\d+)")),
    # PR review *comment* (file:line): …#discussion_r123456
    ("review_comment", re.compile(r"discussion_r(\d+)")),
    # Discussion reply:               …#discussioncomment-123456
    ("discussion_comment", re.compile(r"discussioncomment-(\d+)")),
)


@dataclass(frozen=True)
class GitHubRef:
    """A parsed GitHub thread URL: ``{kind, owner, repo, number}`` (+ optional deep-link fragment)."""

    kind: str  # "issue" | "pull" | "discussion"
    owner: str
    repo: str
    number: int
    fragment_kind: str | None = None  # one of _FRAGMENT_PATTERNS keys, when present
    fragment_id: int | None = None  # the databaseId the fragment points at
    url: str = ""


def parse_github_url(url: str) -> GitHubRef | None:
    """Parse a GitHub issue/PR/discussion URL into a :class:`GitHubRef`, else ``None``.

    Accepts the canonical forms (and tolerates trailing path like ``/files``)::

        https://github.com/{owner}/{repo}/issues/{n}
        https://github.com/{owner}/{repo}/pull/{n}[#issuecomment-…|#pullrequestreview-…|#discussion_r…]
        https://github.com/{owner}/{repo}/discussions/{n}[#discussioncomment-…]

    Returns ``None`` for non-GitHub hosts, non-thread URLs, or anything malformed — callers treat a
    ``None`` as a fail-soft "skip this URL with a note".
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url.strip())
    except (ValueError, AttributeError):
        return None

    host = (parsed.netloc or "").lower()
    # Accept github.com and www.github.com only — never a look-alike host.
    if host not in ("github.com", "www.github.com"):
        return None

    parts = [p for p in (parsed.path or "").split("/") if p]
    if len(parts) < 4:
        return None
    owner, repo, segment, raw_number = parts[0], parts[1], parts[2], parts[3]
    kind = _SEGMENT_KIND.get(segment)
    if kind is None:
        return None
    try:
        number = int(raw_number)
    except ValueError:
        return None
    if number <= 0:
        return None

    fragment_kind, fragment_id = _parse_fragment(parsed.fragment or "")
    return GitHubRef(
        kind=kind,
        owner=owner,
        repo=repo,
        number=number,
        fragment_kind=fragment_kind,
        fragment_id=fragment_id,
        url=url.strip(),
    )


def _parse_fragment(fragment: str) -> tuple[str | None, int | None]:
    """Map a URL fragment to ``(fragment_kind, databaseId)`` if it pins a comment/review."""
    if not fragment:
        return None, None
    for kind, pattern in _FRAGMENT_PATTERNS:
        m = pattern.search(fragment)
        if m:
            try:
                return kind, int(m.group(1))
            except ValueError:  # pragma: no cover — regex guarantees digits
                return None, None
    return None, None


def _truncate(text: str | None, limit: int = MAX_BODY_CHARS) -> str:
    """Cap a body to ``limit`` chars, appending a clear truncation marker."""
    if not text:
        return "(empty)"
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated {len(text) - limit} chars]"


def _login(node: dict[str, Any] | None) -> str:
    """Author login from a ``{author: {login}}`` node, defaulting to ``unknown``."""
    author = (node or {}).get("author") or {}
    return author.get("login") or "unknown"


_TRIGGER_MARKER = "👉 **TRIGGERING ITEM** — "


def _trigger_marker(ref: GitHubRef, fragment_kind: str, database_id: Any) -> str:
    """A '👉 triggering …' prefix when this node is the one the URL fragment pinned, else ''."""
    if (
        ref.fragment_kind == fragment_kind
        and ref.fragment_id is not None
        and database_id == ref.fragment_id
    ):
        return _TRIGGER_MARKER
    return ""


# ---------------------------------------------------------------------------
# GraphQL queries (ported from strands_coder/context.py, + databaseId for deep-linking)
# ---------------------------------------------------------------------------
_ISSUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      number title body state url createdAt
      author { login }
      comments(first: 100) {
        totalCount
        nodes { databaseId author { login } body createdAt url }
      }
      timelineItems(first: 50, itemTypes: [CROSS_REFERENCED_EVENT, REFERENCED_EVENT]) {
        nodes {
          ... on CrossReferencedEvent {
            source {
              ... on PullRequest { number title state url }
              ... on Issue { number title state url }
            }
          }
        }
      }
    }
  }
}
"""

_PR_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number title body state url createdAt
      author { login }
      baseRefName headRefName
      reviews(first: 50) {
        totalCount
        nodes { databaseId author { login } state body createdAt url }
      }
      comments(first: 100) {
        totalCount
        nodes { databaseId author { login } body createdAt url }
      }
      reviewThreads(first: 50) {
        totalCount
        nodes {
          isResolved
          comments(first: 10) {
            nodes { databaseId author { login } body createdAt path line url }
          }
        }
      }
      closingIssuesReferences(first: 25) {
        totalCount
        nodes { number title state url }
      }
    }
  }
}
"""

_DISCUSSION_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    discussion(number: $number) {
      number title body url createdAt
      author { login }
      comments(first: 100) {
        totalCount
        nodes {
          databaseId author { login } body createdAt url
          replies(first: 20) {
            nodes { databaseId author { login } body createdAt url }
          }
        }
      }
    }
  }
}
"""


# ---------------------------------------------------------------------------
# Per-kind enrichment renderers
# ---------------------------------------------------------------------------
def _render_issue(ref: GitHubRef, issue: dict[str, Any]) -> str:
    parts = [
        f"## 🎫 ISSUE {ref.owner}/{ref.repo}#{issue.get('number')}: {issue.get('title', '')}",
        f"**State:** {issue.get('state', '')}  ·  **Author:** @{_login(issue)}  ·  "
        f"**Created:** {issue.get('createdAt', '')}",
        f"**URL:** {issue.get('url', ref.url)}",
        "",
        "### Body",
        "```markdown",
        _truncate(issue.get("body")),
        "```",
    ]

    comments = (issue.get("comments") or {}).get("nodes") or []
    total = (issue.get("comments") or {}).get("totalCount", len(comments))
    if comments:
        parts.append(f"\n### 💬 Comments ({total} total)")
        for idx, c in enumerate(comments[:MAX_COMMENTS], 1):
            marker = _trigger_marker(ref, "issuecomment", c.get("databaseId"))
            parts.append(
                f"\n**{marker}Comment #{idx}** by @{_login(c)} at {c.get('createdAt', '')}:\n"
                f"```markdown\n{_truncate(c.get('body'))}\n```"
            )

    linked = _collect_linked(issue.get("timelineItems"))
    if linked:
        parts.append("\n### 🔗 Linked / cross-referenced items")
        parts.extend(linked)
    return "\n".join(parts)


def _render_pull(ref: GitHubRef, pr: dict[str, Any]) -> str:
    parts = [
        f"## 🔀 PULL REQUEST {ref.owner}/{ref.repo}#{pr.get('number')}: {pr.get('title', '')}",
        f"**State:** {pr.get('state', '')}  ·  **Author:** @{_login(pr)}  ·  "
        f"**Created:** {pr.get('createdAt', '')}",
        f"**Branches:** {pr.get('headRefName', '')} → {pr.get('baseRefName', '')}",
        f"**URL:** {pr.get('url', ref.url)}",
        "",
        "### Body",
        "```markdown",
        _truncate(pr.get("body")),
        "```",
    ]

    reviews = (pr.get("reviews") or {}).get("nodes") or []
    total_reviews = (pr.get("reviews") or {}).get("totalCount", len(reviews))
    if reviews:
        parts.append(f"\n### ✅ Reviews ({total_reviews} total)")
        for idx, r in enumerate(reviews[:MAX_REVIEWS], 1):
            marker = _trigger_marker(ref, "review", r.get("databaseId"))
            parts.append(
                f"\n**{marker}Review #{idx}** by @{_login(r)} — {r.get('state', '')} "
                f"at {r.get('createdAt', '')}:\n"
                f"```markdown\n{_truncate(r.get('body') or '(no comment)')}\n```"
            )

    comments = (pr.get("comments") or {}).get("nodes") or []
    total_comments = (pr.get("comments") or {}).get("totalCount", len(comments))
    if comments:
        parts.append(f"\n### 💬 Comments ({total_comments} total)")
        for idx, c in enumerate(comments[:MAX_COMMENTS], 1):
            marker = _trigger_marker(ref, "issuecomment", c.get("databaseId"))
            parts.append(
                f"\n**{marker}Comment #{idx}** by @{_login(c)} at {c.get('createdAt', '')}:\n"
                f"```markdown\n{_truncate(c.get('body'))}\n```"
            )

    threads = (pr.get("reviewThreads") or {}).get("nodes") or []
    total_threads = (pr.get("reviewThreads") or {}).get("totalCount", len(threads))
    if threads:
        parts.append(f"\n### 🧵 Code Review Threads ({total_threads} total)")
        for idx, thread in enumerate(threads[:MAX_REVIEW_THREADS], 1):
            tcomments = (thread.get("comments") or {}).get("nodes") or []
            if not tcomments:
                continue
            first = tcomments[0]
            status = "✅ Resolved" if thread.get("isResolved") else "🔴 Unresolved"
            marker = _trigger_marker(ref, "review_comment", first.get("databaseId"))
            parts.append(
                f"\n**{marker}Thread #{idx}** [{status}] by @{_login(first)} on "
                f"`{first.get('path', '')}:{first.get('line')}`:\n"
                f"```markdown\n{_truncate(first.get('body'))}\n```"
            )
            for reply in tcomments[1:MAX_THREAD_COMMENTS]:
                rmarker = _trigger_marker(ref, "review_comment", reply.get("databaseId"))
                parts.append(f"  ↳ {rmarker}@{_login(reply)}: {_truncate(reply.get('body'), 500)}")

    closing = (pr.get("closingIssuesReferences") or {}).get("nodes") or []
    if closing:
        parts.append("\n### 🎫 Linked / closing issues")
        for issue in closing[:MAX_LINKED]:
            parts.append(
                f"  - Fixes #{issue.get('number')}: {issue.get('title', '')} "
                f"({issue.get('state', '')}) — {issue.get('url', '')}"
            )
    return "\n".join(parts)


def _render_discussion(ref: GitHubRef, disc: dict[str, Any]) -> str:
    parts = [
        f"## 💭 DISCUSSION {ref.owner}/{ref.repo}#{disc.get('number')}: {disc.get('title', '')}",
        f"**Author:** @{_login(disc)}  ·  **Created:** {disc.get('createdAt', '')}",
        f"**URL:** {disc.get('url', ref.url)}",
        "",
        "### Body",
        "```markdown",
        _truncate(disc.get("body")),
        "```",
    ]

    comments = (disc.get("comments") or {}).get("nodes") or []
    total = (disc.get("comments") or {}).get("totalCount", len(comments))
    if comments:
        parts.append(f"\n### 💬 Replies ({total} total)")
        for idx, c in enumerate(comments[:MAX_COMMENTS], 1):
            marker = _trigger_marker(ref, "discussion_comment", c.get("databaseId"))
            parts.append(
                f"\n**{marker}Reply #{idx}** by @{_login(c)} at {c.get('createdAt', '')}:\n"
                f"```markdown\n{_truncate(c.get('body'))}\n```"
            )
            nested = (c.get("replies") or {}).get("nodes") or []
            for reply in nested[:MAX_REPLIES]:
                rmarker = _trigger_marker(ref, "discussion_comment", reply.get("databaseId"))
                parts.append(f"  ↳ {rmarker}@{_login(reply)}: {_truncate(reply.get('body'), 500)}")
    return "\n".join(parts)


def _login_rest(node: dict[str, Any] | None) -> str:
    """Author login from a REST ``{user: {login}}`` node, defaulting to ``unknown``."""
    user = (node or {}).get("user") or {}
    return user.get("login") or "unknown"


def _render_issue_rest(ref: GitHubRef, issue: dict[str, Any], comments: Any) -> str:
    """Render a public issue fetched anonymously via REST (body + comments)."""
    parts = [
        f"## 🎫 ISSUE {ref.owner}/{ref.repo}#{issue.get('number')}: {issue.get('title', '')}",
        f"**State:** {issue.get('state', '')}  ·  **Author:** @{_login_rest(issue)}  ·  "
        f"**Created:** {issue.get('created_at', '')}",
        f"**URL:** {issue.get('html_url', ref.url)}",
        _REST_FALLBACK_NOTE,
        "",
        "### Body",
        "```markdown",
        _truncate(issue.get("body")),
        "```",
    ]
    items = comments if isinstance(comments, list) else []
    if items:
        parts.append(f"\n### 💬 Comments ({len(items)} shown)")
        for idx, c in enumerate(items[:MAX_COMMENTS], 1):
            marker = _trigger_marker(ref, "issuecomment", c.get("id"))
            parts.append(
                f"\n**{marker}Comment #{idx}** by @{_login_rest(c)} at {c.get('created_at', '')}:\n"
                f"```markdown\n{_truncate(c.get('body'))}\n```"
            )
    return "\n".join(parts)


def _render_pull_rest(
    ref: GitHubRef, pr: dict[str, Any], comments: Any, reviews: Any, review_comments: Any
) -> str:
    """Render a public PR fetched anonymously via REST (body + comments + reviews + review comments)."""
    parts = [
        f"## 🔀 PULL REQUEST {ref.owner}/{ref.repo}#{pr.get('number')}: {pr.get('title', '')}",
        f"**State:** {pr.get('state', '')}  ·  **Author:** @{_login_rest(pr)}  ·  "
        f"**Created:** {pr.get('created_at', '')}",
        f"**Branches:** {(pr.get('head') or {}).get('ref', '')} → {(pr.get('base') or {}).get('ref', '')}",
        f"**URL:** {pr.get('html_url', ref.url)}",
        _REST_FALLBACK_NOTE,
        "",
        "### Body",
        "```markdown",
        _truncate(pr.get("body")),
        "```",
    ]

    review_items = reviews if isinstance(reviews, list) else []
    # REST returns a "PENDING"/empty review for every viewer; only show ones with state+content.
    shown_reviews = [r for r in review_items if (r.get("state") or "").upper() != "PENDING"]
    if shown_reviews:
        parts.append(f"\n### ✅ Reviews ({len(shown_reviews)} shown)")
        for idx, r in enumerate(shown_reviews[:MAX_REVIEWS], 1):
            marker = _trigger_marker(ref, "review", r.get("id"))
            parts.append(
                f"\n**{marker}Review #{idx}** by @{_login_rest(r)} — {r.get('state', '')} "
                f"at {r.get('submitted_at', '')}:\n"
                f"```markdown\n{_truncate(r.get('body') or '(no comment)')}\n```"
            )

    comment_items = comments if isinstance(comments, list) else []
    if comment_items:
        parts.append(f"\n### 💬 Comments ({len(comment_items)} shown)")
        for idx, c in enumerate(comment_items[:MAX_COMMENTS], 1):
            marker = _trigger_marker(ref, "issuecomment", c.get("id"))
            parts.append(
                f"\n**{marker}Comment #{idx}** by @{_login_rest(c)} at {c.get('created_at', '')}:\n"
                f"```markdown\n{_truncate(c.get('body'))}\n```"
            )

    rc_items = review_comments if isinstance(review_comments, list) else []
    if rc_items:
        parts.append(f"\n### 🧵 Review Comments ({len(rc_items)} shown)")
        for idx, rc in enumerate(rc_items[:MAX_REVIEW_THREADS], 1):
            marker = _trigger_marker(ref, "review_comment", rc.get("id"))
            line = rc.get("line")
            if line is None:
                line = rc.get("original_line")
            parts.append(
                f"\n**{marker}Review comment #{idx}** by @{_login_rest(rc)} on "
                f"`{rc.get('path', '')}:{line}`:\n"
                f"```markdown\n{_truncate(rc.get('body'))}\n```"
            )
    return "\n".join(parts)


def _collect_linked(timeline: dict[str, Any] | None) -> list[str]:
    """Format cross-referenced PRs/issues from an issue's timeline items."""
    nodes = (timeline or {}).get("nodes") or []
    out: list[str] = []
    for item in nodes:
        source = item.get("source") or {}
        number = source.get("number")
        title = source.get("title")
        if number and title:
            out.append(
                f"  - #{number}: {title} ({source.get('state', '')}) — {source.get('url', '')}"
            )
        if len(out) >= MAX_LINKED:
            break
    return out


_QUERY_BY_KIND = {
    "issue": (_ISSUE_QUERY, "issue", _render_issue),
    "pull": (_PR_QUERY, "pullRequest", _render_pull),
    "discussion": (_DISCUSSION_QUERY, "discussion", _render_discussion),
}


def _maybe_trigger_note(ref: GitHubRef, body: str) -> str:
    """Append a trailing 'triggered by …' note, but only if a 👉 marker was actually rendered.

    The pinned item can fall beyond the fetched page (or the fragment kind not apply to this URL),
    in which case no marker is placed — and we must not point the reader at a 👉 that isn't there.
    """
    if ref.fragment_kind and ref.fragment_id is not None and _TRIGGER_MARKER in body:
        body += (
            f"\n\n> ℹ️ This turn was triggered by a specific {ref.fragment_kind.replace('_', ' ')} "
            f"(id {ref.fragment_id}) — see the item marked 👉 above, or {ref.url}"
        )
    return body


def _enrich_one(ref: GitHubRef, token: str, graphql: GraphQLFn, rest: RestFn) -> str:
    """Fetch + render one ref. Fail-soft: any error returns a short note, never raises.

    With a ``token`` → full GraphQL enrichment. Without one → the anonymous REST fallback for public
    issues/PRs (discussions are GraphQL-only, so they get a short "needs a token" note).
    """
    if not token:
        return _enrich_one_rest(ref, rest)
    return _enrich_one_graphql(ref, token, graphql)


def _enrich_one_graphql(ref: GitHubRef, token: str, graphql: GraphQLFn) -> str:
    """Full GraphQL enrichment for one ref (the token path)."""
    query, field, render = _QUERY_BY_KIND[ref.kind]
    try:
        data = graphql(
            query, {"owner": ref.owner, "name": ref.repo, "number": ref.number}, token
        )
    except Exception as e:  # noqa: BLE001 — one bad URL must never crash the turn
        return _note(ref, f"fetch failed ({type(e).__name__}: {e})")

    if not isinstance(data, dict):
        return _note(ref, "unexpected (non-JSON) response")
    if data.get("errors"):
        return _note(ref, f"GraphQL errors: {data['errors']}")
    node = ((data.get("data") or {}).get("repository") or {}).get(field)
    if not node:
        return _note(ref, "not found (or not visible with this token)")

    return _maybe_trigger_note(ref, render(ref, node))


def _enrich_one_rest(ref: GitHubRef, rest: RestFn) -> str:
    """Anonymous REST v3 fallback for one ref (the no-token path).

    Serves public issues/PRs (body + comments, plus PR reviews & review comments with file:line).
    Discussions are absent from REST v3, so a discussion URL returns a short "needs a token" note.
    Fail-soft: a private/missing thread (REST 404) or any error returns a note, never raises.
    """
    if ref.kind == "discussion":
        return _note(
            ref,
            "discussions are only available via GitHub's GraphQL API, which has no anonymous "
            f"access — configure a GitHub token to enrich discussions, or open it directly: {ref.url}",
        )

    base = f"/repos/{ref.owner}/{ref.repo}"
    try:
        if ref.kind == "issue":
            issue = rest(f"{base}/issues/{ref.number}", "")
            if not isinstance(issue, dict) or issue.get("number") is None:
                return _note(ref, _REST_NOT_FOUND)
            comments = rest(f"{base}/issues/{ref.number}/comments?per_page={MAX_COMMENTS}", "")
            return _maybe_trigger_note(ref, _render_issue_rest(ref, issue, comments))

        # pull
        pr = rest(f"{base}/pulls/{ref.number}", "")
        if not isinstance(pr, dict) or pr.get("number") is None:
            return _note(ref, _REST_NOT_FOUND)
        comments = rest(f"{base}/issues/{ref.number}/comments?per_page={MAX_COMMENTS}", "")
        reviews = rest(f"{base}/pulls/{ref.number}/reviews?per_page={MAX_REVIEWS}", "")
        review_comments = rest(f"{base}/pulls/{ref.number}/comments?per_page={MAX_COMMENTS}", "")
        return _maybe_trigger_note(
            ref, _render_pull_rest(ref, pr, comments, reviews, review_comments)
        )
    except Exception as e:  # noqa: BLE001 — one bad URL must never crash the turn
        return _note(
            ref,
            f"anonymous REST fetch failed ({type(e).__name__}: {e}) — "
            "configure a GitHub token for full enrichment",
        )


def _note(ref: GitHubRef | None, message: str) -> str:
    """A short fail-soft note for one URL (the agent still sees what went wrong)."""
    where = f"{ref.owner}/{ref.repo}#{ref.number}" if ref else "URL"
    return f"## ⚠️ GitHub context unavailable for {where}\n{message}"


def build_github_context(
    urls: list[str] | str,
    *,
    token: str | None,
    graphql: GraphQLFn,
    rest: RestFn | None = None,
) -> str:
    """Build one enriched markdown block per GitHub URL (the pure, testable core).

    Each URL is handled independently and fail-soft: a malformed/non-GitHub URL or an HTTP error
    contributes a short note instead of raising.

    Token-optional: with a ``token`` each thread is enriched fully via ``graphql``; without one,
    public issues/PRs fall back to the anonymous REST API via ``rest`` (discussions are GraphQL-only
    and inject a short "needs a token" note). Both ``graphql`` and ``rest`` are injected network
    seams (``github._graphql`` / ``github._rest_get`` shaped), so tests pass fakes and stay fully
    hermetic; ``rest`` defaults to the real ``github._rest_get`` when omitted.
    """
    if isinstance(urls, str):
        urls = [urls]

    if rest is None:
        from strandly_harness.tools.github import _rest_get

        rest = _rest_get

    blocks: list[str] = []
    for raw in urls:
        ref = parse_github_url(raw)
        if ref is None:
            blocks.append(
                f"## ⚠️ Not an enrichable GitHub URL\n`{raw}` — expected an issue, pull, or "
                f"discussion URL like https://github.com/owner/repo/issues/123"
            )
            continue
        blocks.append(_enrich_one(ref, token or "", graphql, rest))

    if not blocks:
        return ""
    return "\n\n---\n\n".join(blocks)
