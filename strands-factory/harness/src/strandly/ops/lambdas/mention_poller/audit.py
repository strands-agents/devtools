"""Out-of-band GitHub write audit: does the agent's token act only inside the allowed owners?

``use_github`` enforces the owner allow-list in-band, which cannot see a leaked token, a bypassed
guardrail or a prompt-injected path around it. This scheduled Lambda asks GitHub (GraphQL
``contributionsCollection`` and the viewer's discussion connections, plus the REST events backstop)
what the token's account did in the last window and publishes every write outside the allow-list
to SNS. It is a safety check, not a metric.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from strandly.ops.github import open_github
from strandly.ops.lambdas import configure_logging

if TYPE_CHECKING:
    from strandly.core.config import AuditSettings

logger = logging.getLogger(__name__)

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
GITHUB_REST_URL = "https://api.github.com"
_USER_AGENT = "strandly-write-audit/1.0"

# Public-events feed types that represent a *write* by the account. Mirrors
# ``tools/github_client.py::_THROTTLED_EVENT_TYPES``; a parity test pins them. These are the
# comment/push/branch actions ``contributionsCollection`` either omits (plain comments) or we want a
# second independent source for. ``DiscussionEvent`` covers only discussion creation; discussion
# comments come from the viewer's discussion connections in the contributions query.
WRITE_EVENT_TYPES = frozenset(
    {
        "IssueCommentEvent",
        "PullRequestReviewEvent",
        "PullRequestReviewCommentEvent",
        "IssuesEvent",
        "PullRequestEvent",
        "CommitCommentEvent",
        "CreateEvent",
        "DeleteEvent",
        "PushEvent",
        "DiscussionEvent",
    }
)

# The contributions query: the token's own identity (`viewer`) + every repo it touched in the
# window. `first: 100` / `maxRepositories: 100` is far above a normal window's activity; we
# deliberately don't paginate (one cheap call) — but because the tail past 100 is exactly where a
# flood attack would push a violation, each connection also fetches its `totalCount` (and the
# collection's `totalRepositoriesWithContributedCommits`) so a truncated tail is DETECTED and
# recorded as an audit error (see `contribution_saturation_errors`) instead of silently ignored.
_CONTRIBUTIONS_QUERY = """
query($from: DateTime!, $to: DateTime!) {
  viewer {
    login
    contributionsCollection(from: $from, to: $to) {
      issueContributions(first: 100) { totalCount nodes { issue { repository { nameWithOwner } } } }
      pullRequestContributions(first: 100) { totalCount nodes { pullRequest { repository { nameWithOwner } } } }
      pullRequestReviewContributions(first: 100) { totalCount nodes { pullRequestReview { repository { nameWithOwner } } } }
      commitContributionsByRepository(maxRepositories: 100) { repository { nameWithOwner } }
      totalRepositoriesWithContributedCommits
    }
  }
}
"""

# Discussion writes are in no contribution kind, so the viewer's own discussion connections cover
# them. `repositoryDiscussionComments` takes no date filter or documented order, so both ends are
# fetched and filtered to the window (see `discussion_saturation_errors` for the hidden middle).
# A separate request: these fields are non-null, so a token that cannot read discussions would
# null the whole viewer and take the contributions (and the login the events backstop needs) too.
_DISCUSSIONS_QUERY = """
query {
  viewer {
    discussionCommentsHead: repositoryDiscussionComments(first: 100) { totalCount nodes { createdAt updatedAt discussion { repository { nameWithOwner } } } }
    discussionCommentsTail: repositoryDiscussionComments(last: 100) { nodes { createdAt updatedAt discussion { repository { nameWithOwner } } } }
    repositoryDiscussions(first: 100, orderBy: {field: UPDATED_AT, direction: DESC}) { totalCount nodes { createdAt updatedAt repository { nameWithOwner } } }
  }
}
"""

# The public events feed is hard-capped by GitHub at 300 events (3 pages × per_page=100).
# We paginate to that cap: a single page was a *floodable* blind spot for a SECURITY audit —
# an attacker holding a leaked token could emit 100+ innocuous public events (stars, pushes to
# a throwaway repo) to push a violating write off the only page we read. The cap itself is
# load-bearing, so hitting it without reaching the window start is surfaced as an audit error.
_EVENTS_PER_PAGE = 100
_EVENTS_MAX_PAGES = 3
_EVENTS_FEED_CAP = _EVENTS_PER_PAGE * _EVENTS_MAX_PAGES


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class AuditReport:
    """The outcome of one audit pass.

    ``violations`` is the security signal: repos the account wrote to whose owner is **not** in the
    allow-list. ``errors`` records any source that failed (the audit is fail-soft — a failed source
    contributes no repos rather than raising), so a caller can tell "clean" apart from "couldn't
    fully check".
    """

    login: str | None = None
    checked_repos: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True iff no out-of-org writes were found (regardless of soft errors)."""
        return not self.violations

    def as_dict(self) -> dict[str, Any]:
        return {
            "login": self.login,
            "checked_repos": self.checked_repos,
            "violations": self.violations,
            "errors": self.errors,
            "ok": self.ok,
        }


# ---------------------------------------------------------------------------
# HTTP seam (stdlib urllib; the only functions tests monkeypatch for the network)
# ---------------------------------------------------------------------------
def _request(method: str, url: str, token: str, body: dict[str, Any] | None = None) -> Any:
    """One GitHub API call against the fixed ``api.github.com`` host. Returns parsed JSON."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", _USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    # Fixed api.github.com URLs; open_github keeps the token off any redirect host.
    with open_github(req, token, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _graphql(query: str, variables: dict[str, Any], token: str) -> dict[str, Any]:
    return _request("POST", GITHUB_GRAPHQL_URL, token, {"query": query, "variables": variables})


def _rest_get(path: str, token: str) -> Any:
    return _request("GET", f"{GITHUB_REST_URL}{path}", token)


# ---------------------------------------------------------------------------
# Pure extractors (fully unit-testable, no network)
# ---------------------------------------------------------------------------
def extract_repos_from_contributions(viewer: dict[str, Any] | None) -> set[str]:
    """Every ``owner/repo`` in a ``viewer.contributionsCollection`` payload (defensive on shape).

    Walks the four contribution kinds (issues, PRs, PR reviews, commit repos), pulling each item's
    ``repository.nameWithOwner``. Anything missing/null/wrong-typed is skipped rather than raising,
    so a partial or shape-shifted GraphQL response still yields whatever repos it *can*.
    """
    repos: set[str] = set()
    if not isinstance(viewer, dict):
        return repos
    cc = viewer.get("contributionsCollection")
    if not isinstance(cc, dict):
        return repos

    def _add(node: Any, *path: str) -> None:
        cur: Any = node
        for key in path:
            if not isinstance(cur, dict):
                return
            cur = cur.get(key)
        if isinstance(cur, str) and "/" in cur:
            repos.add(cur)

    for field_name, item_key in (
        ("issueContributions", "issue"),
        ("pullRequestContributions", "pullRequest"),
        ("pullRequestReviewContributions", "pullRequestReview"),
    ):
        section = cc.get(field_name)
        nodes = section.get("nodes") if isinstance(section, dict) else None
        if isinstance(nodes, list):
            for node in nodes:
                _add(node, item_key, "repository", "nameWithOwner")

    commit_repos = cc.get("commitContributionsByRepository")
    if isinstance(commit_repos, list):
        for node in commit_repos:
            _add(node, "repository", "nameWithOwner")

    return repos


_CONTRIBUTION_SECTIONS = (
    "issueContributions",
    "pullRequestContributions",
    "pullRequestReviewContributions",
)


def contribution_saturation_errors(viewer: dict[str, Any] | None) -> list[str]:
    """Error strings for contribution connections whose tail was truncated by ``first: 100``.

    GitHub reports each connection's ``totalCount`` regardless of how many nodes we asked for.
    ``totalCount > len(nodes)`` means activity past the fetched window exists — exactly where a
    flood attack would hide a violation — so the truncation must surface as a visible audit
    error, not be silently ignored. Defensive on shape (missing/odd fields yield no error):
    older payloads without ``totalCount`` simply produce nothing, keeping this additive.
    """
    errors: list[str] = []
    if not isinstance(viewer, dict):
        return errors
    cc = viewer.get("contributionsCollection")
    if not isinstance(cc, dict):
        return errors
    for name in _CONTRIBUTION_SECTIONS:
        section = cc.get(name)
        if not isinstance(section, dict):
            continue
        total = section.get("totalCount")
        nodes = section.get("nodes")
        fetched = len(nodes) if isinstance(nodes, list) else 0
        if isinstance(total, int) and total > fetched:
            errors.append(
                f"contributions saturated: {name} totalCount {total} > fetched {fetched} — "
                "tail truncated, window may hide writes"
            )
    total_commit_repos = cc.get("totalRepositoriesWithContributedCommits")
    commit_nodes = cc.get("commitContributionsByRepository")
    fetched_commits = len(commit_nodes) if isinstance(commit_nodes, list) else 0
    if isinstance(total_commit_repos, int) and total_commit_repos > fetched_commits:
        errors.append(
            f"contributions saturated: commitContributionsByRepository "
            f"{total_commit_repos} repos > fetched {fetched_commits} — "
            "tail truncated, window may hide writes"
        )
    return errors


def _connection_nodes(viewer: Any, name: str) -> list[dict[str, Any]]:
    section = viewer.get(name) if isinstance(viewer, dict) else None
    nodes = section.get("nodes") if isinstance(section, dict) else None
    return [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else []


def _touched_since(node: dict[str, Any], since: datetime) -> bool:
    """True when the node was created or edited at or after ``since``; undated nodes count."""
    stamps = [ts for key in ("createdAt", "updatedAt") if (ts := _parse_ts(node.get(key)))]
    return not stamps or max(stamps) >= since


def extract_repos_from_discussions(viewer: dict[str, Any] | None, *, since: datetime) -> set[str]:
    """Every ``owner/repo`` where the viewer created or edited a discussion or discussion comment.

    Reads the ``discussionCommentsHead``/``discussionCommentsTail`` and ``repositoryDiscussions``
    connections of the contributions query and keeps nodes touched at or after ``since``.
    """
    repos: set[str] = set()
    comments = _connection_nodes(viewer, "discussionCommentsHead")
    comments += _connection_nodes(viewer, "discussionCommentsTail")
    for node in comments:
        discussion = node.get("discussion")
        repo = discussion.get("repository") if isinstance(discussion, dict) else None
        name = repo.get("nameWithOwner") if isinstance(repo, dict) else None
        if isinstance(name, str) and "/" in name and _touched_since(node, since):
            repos.add(name)
    for node in _connection_nodes(viewer, "repositoryDiscussions"):
        repo = node.get("repository")
        name = repo.get("nameWithOwner") if isinstance(repo, dict) else None
        if isinstance(name, str) and "/" in name and _touched_since(node, since):
            repos.add(name)
    return repos


def discussion_saturation_errors(viewer: dict[str, Any] | None, *, since: datetime) -> list[str]:
    """Error strings when unfetched discussion activity may fall inside the window.

    Comments come from both ends of a connection in a stable but undocumented order, so the
    unfetched middle can hold in-window comments only when a node next to it is in the window.
    Discussions are newest-updated first, so only the oldest fetched node bounds the tail.
    """
    errors: list[str] = []
    if not isinstance(viewer, dict):
        return errors
    head_section = viewer.get("discussionCommentsHead")
    total = head_section.get("totalCount") if isinstance(head_section, dict) else None
    head = _connection_nodes(viewer, "discussionCommentsHead")
    tail = _connection_nodes(viewer, "discussionCommentsTail")
    if isinstance(total, int) and total > len(head) + len(tail):
        edges = head[-1:] + tail[:1]
        if not edges or any(_touched_since(n, since) for n in edges):
            errors.append(
                f"discussion comments saturated: totalCount {total} > fetched "
                f"{len(head) + len(tail)} and the unfetched middle may hold writes in the window"
            )
    disc_section = viewer.get("repositoryDiscussions")
    disc_total = disc_section.get("totalCount") if isinstance(disc_section, dict) else None
    discussions = _connection_nodes(viewer, "repositoryDiscussions")
    if isinstance(disc_total, int) and disc_total > len(discussions):
        if not discussions or _touched_since(discussions[-1], since):
            errors.append(
                f"discussions saturated: totalCount {disc_total} > fetched {len(discussions)} "
                "without reaching the window start"
            )
    return errors


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def extract_repos_from_events(events: Any, *, since: datetime) -> set[str]:
    """Every ``owner/repo`` from *write* events in a ``/users/{login}/events`` feed since ``since``.

    The comment backstop: filters to :data:`WRITE_EVENT_TYPES` (so a read/watch/star never counts as
    a write) and to events at or after ``since`` (the feed is broader than our window). Items with a
    missing/unparseable timestamp are **kept** — failing open here means we'd rather over-report a
    borderline event than silently drop a genuine out-of-org write.
    """
    repos: set[str] = set()
    if not isinstance(events, list):
        return repos
    for event in events:
        if not isinstance(event, dict) or event.get("type") not in WRITE_EVENT_TYPES:
            continue
        when = _parse_ts(event.get("created_at"))
        if when is not None and when < since:
            continue
        repo = event.get("repo")
        name = repo.get("name") if isinstance(repo, dict) else None
        if isinstance(name, str) and "/" in name:
            repos.add(name)
    return repos


def repo_allowed(repo: str, allowed_owners: set[str]) -> bool:
    """True iff ``repo`` (``owner/repo``) is permitted by ``allowed_owners``. Case-insensitive.

    Mirrors ``tools/github.py::_target_allowed`` (the guardrail that decides what the agent may
    write to) so the audit never reports a write the guardrail allowed: an entry is either a bare
    owner (whole-org grant) or an ``owner/repo`` glob such as ``aws/bedrock-agentcore-*`` matched
    with ``fnmatch`` against the full name. Duplicated rather than imported because this Lambda
    stays stdlib-only (``tools/github`` pulls in the agent SDK); a parity test pins the two together.
    """
    r = repo.lower()
    r_owner = r.split("/", 1)[0]
    for entry in allowed_owners:
        e = entry.lower()
        if "/" in e:
            if "/" in r and fnmatch.fnmatchcase(r, e):
                return True
        elif r_owner == e:
            return True
    return False


def find_violations(repos: set[str], allowed_owners: set[str]) -> list[str]:
    """Repos that ``allowed_owners`` does **not** permit (owner or repo-glob match), sorted.

    Before this used the guardrail's matcher it compared only the owner half, so an allowed
    ``owner/repo-*`` glob entry produced a false "out-of-org" violation for every matching repo.

    With an **empty** ``allowed_owners`` we cannot say what's in-org, so we return ``[]`` rather than
    flagging everything — the audit gate (:meth:`Config.audit_enabled`) requires a non-empty
    allow-list, so the Lambda never actually runs the check without one; this only guards the pure
    function against a caller that forgot.
    """
    if not allowed_owners:
        return []
    return sorted(r for r in repos if not repo_allowed(r, allowed_owners))


# ---------------------------------------------------------------------------
# Network gatherers (fail-soft: a failed source records an error, contributes no repos)
# ---------------------------------------------------------------------------
def gather_contributions(
    token: str, *, frm: datetime, to: datetime, errors: list[str]
) -> tuple[str | None, set[str]]:
    """``(login, repos)`` from the GraphQL contributions query. Fail-soft via ``errors``."""
    try:
        data = _graphql(
            _CONTRIBUTIONS_QUERY,
            {"from": frm.isoformat(), "to": to.isoformat()},
            token,
        )
    except Exception as e:  # noqa: BLE001 — fail-soft: a bad call records an error, never raises
        errors.append(f"contributions fetch failed: {type(e).__name__}: {e}")
        return None, set()
    if isinstance(data, dict) and data.get("errors"):
        errors.append(f"contributions GraphQL errors: {json.dumps(data['errors'])}")
    viewer = ((data or {}).get("data") or {}).get("viewer") if isinstance(data, dict) else None
    login = viewer.get("login") if isinstance(viewer, dict) else None
    # Truncated tails (totalCount > fetched nodes) must be visible: a flood attack hides its
    # violation exactly in the part we didn't fetch.
    errors.extend(contribution_saturation_errors(viewer))
    since = frm if frm.tzinfo else frm.replace(tzinfo=timezone.utc)
    return login, extract_repos_from_contributions(viewer) | gather_discussions(
        token, since=since, errors=errors
    )


def gather_discussions(token: str, *, since: datetime, errors: list[str]) -> set[str]:
    """Repos of the viewer's discussions and discussion comments touched since ``since``."""
    try:
        data = _graphql(_DISCUSSIONS_QUERY, {}, token)
    except Exception as e:  # noqa: BLE001 — fail-soft: a bad call records an error, never raises
        errors.append(f"discussions fetch failed: {type(e).__name__}: {e}")
        return set()
    if isinstance(data, dict) and data.get("errors"):
        errors.append(f"discussions GraphQL errors: {json.dumps(data['errors'])}")
    viewer = ((data or {}).get("data") or {}).get("viewer") if isinstance(data, dict) else None
    if not isinstance(viewer, dict):
        errors.append("discussions query returned no viewer; discussion writes were not audited")
        return set()
    errors.extend(discussion_saturation_errors(viewer, since=since))
    return extract_repos_from_discussions(viewer, since=since)


def gather_events(login: str, token: str, *, since: datetime, errors: list[str]) -> set[str]:
    """Write-event repos from the REST events feed for ``login``. Fail-soft via ``errors``.

    Paginated to the feed's hard API cap (3 pages × 100 = 300 events) because this is a
    SECURITY backstop: with a single page, an attacker holding a leaked token could flood
    100+ innocuous public events (stars, forks, pushes to a throwaway repo) to push a
    violating write off the only page we read — the cap was load-bearing and silently
    bypassable. Pagination raises the bar to GitHub's hard limit, and two outcomes are made
    *visible* instead of silent:

    - **Saturation**: all 3 pages full without reaching an event older than ``since`` means
      the window extends past what the feed can show → recorded as an audit error so a
      "clean" report can't masquerade over a flooded window.
    - **Partial fetch**: a page fetch failing mid-way keeps earlier pages' repos (partial
      coverage beats none) and records the failure.

    Early stop: the feed is newest-first, so the first event with a parseable timestamp older
    than ``since`` proves the window is fully in view — deeper pages are older still, skip them.
    """
    if not login:
        return set()
    # Normalize once: GitHub timestamps are always aware (Z-suffixed), so a naive `since`
    # would raise TypeError out of a function whose contract is "never raises, errors-list
    # only". The only production caller passes aware datetimes; this guards direct callers.
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    events: list[Any] = []
    window_covered = False  # saw an event older than `since` → the whole window is in view
    pages_fetched = 0
    last_page_full = False
    for page in range(1, _EVENTS_MAX_PAGES + 1):
        try:
            batch = _rest_get(
                f"/users/{login}/events?per_page={_EVENTS_PER_PAGE}&page={page}", token
            )
        except Exception as e:  # noqa: BLE001 — fail-soft
            errors.append(f"events fetch failed (page {page}): {type(e).__name__}: {e}")
            break  # keep whatever earlier pages produced — partial coverage beats none
        if not isinstance(batch, list):
            errors.append(f"events fetch returned non-list payload (page {page})")
            break
        pages_fetched += 1
        events.extend(batch)
        last_page_full = len(batch) >= _EVENTS_PER_PAGE
        # OR-accumulate so correctness doesn't hinge on the `break` below (review finding:
        # plain reassignment was correct only because a True immediately exits the loop).
        window_covered = window_covered or any(
            (ts := _parse_ts(e.get("created_at"))) is not None and ts < since
            for e in batch
            if isinstance(e, dict)
        )
        if window_covered or not last_page_full:
            break
    if pages_fetched == _EVENTS_MAX_PAGES and last_page_full and not window_covered:
        # The feed's cap was reached while still inside the audit window: older writes in the
        # window are invisible. This is exactly the flood-attack shape, so it must be an ERROR
        # (degraded pass), not a silently "clean" result.
        errors.append(
            f"events feed saturated: fetched {len(events)} events (feed cap "
            f"{_EVENTS_FEED_CAP}) without reaching the window start — older writes in the "
            "window may be hidden; treat this pass as degraded, not clean"
        )
    return extract_repos_from_events(events, since=since)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def audit(settings: AuditSettings, *, now: datetime | None = None) -> AuditReport:
    """Run one audit pass: enumerate what the token's account wrote, flag out-of-org repos.

    Pure-ish: the only I/O is the two monkeypatchable gatherers. ``now`` is injectable for
    deterministic tests. Fail-soft throughout — a source that errors lands in ``report.errors`` and
    contributes no repos, so the pass always returns a report rather than raising.
    """
    now = now or datetime.now(timezone.utc)
    frm = now - timedelta(hours=settings.lookback_hours)
    errors: list[str] = []

    token = settings.token or ""
    if not token:
        errors.append("no audit token configured")
        return AuditReport(errors=errors)

    login, contrib_repos = gather_contributions(token, frm=frm, to=now, errors=errors)
    event_repos = gather_events(login or "", token, since=frm, errors=errors)
    all_repos = contrib_repos | event_repos

    violations = find_violations(all_repos, set(settings.allowed_owners))
    return AuditReport(
        login=login,
        checked_repos=sorted(all_repos),
        violations=violations,
        errors=errors,
    )


def notify(report: AuditReport, settings: AuditSettings) -> bool:
    """Publish a violation finding to SNS when a topic is configured. Best-effort; returns sent?.

    Only fires when there's something to report (``report.violations``) **and** a topic ARN is set.
    boto3 is imported lazily and any publish error is swallowed (logged) — a failed notification
    must not turn a successful audit into a crash.
    """
    if not report.violations or not settings.sns_topic_arn:
        return False
    subject = f"\u26a0 strandly write-audit: {len(report.violations)} out-of-org write(s)"
    message = json.dumps(report.as_dict(), indent=2)
    try:
        import boto3

        client = (
            boto3.client("sns", region_name=settings.region)
            if settings.region
            else boto3.client("sns")
        )
        client.publish(
            TopicArn=settings.sns_topic_arn,
            Subject=subject[:100],  # SNS Subject hard limit
            Message=message,
        )
        return True
    except Exception as e:  # noqa: BLE001 — notification is best-effort
        logger.warning("audit SNS publish failed: %s", e)
        return False


def lambda_handler(event: Any = None, context: Any = None) -> dict[str, Any]:  # noqa: ARG001
    """AWS Lambda entrypoint: run an audit pass and notify on any out-of-org write.

    Gated like every other capability: with no allow-list + token (``Config.audit_enabled`` is
    False) it's a no-op. Logs a warning when violations are found so the finding is visible even
    without SNS wired.
    """
    from strandly.core.config import Config

    configure_logging()
    config = Config.load()
    if not config.audit_enabled:
        logger.info("write-audit: disabled (set STRANDLY_AUDIT_ALLOWED_OWNERS + a token)")
        return {"status": "disabled"}

    settings = config.audit
    report = audit(settings)
    if report.errors:
        # A degraded pass (a source errored) must not masquerade as "clean": with both sources down
        # we'd see zero violations over zero repos. Surface it on the warning channel so the
        # "audit could not fully check" stays distinguishable from a finding.
        logger.warning(
            "write-audit: degraded — %d source error(s), only %d repo(s) checked: %s",
            len(report.errors),
            len(report.checked_repos),
            report.errors,
        )
    if report.violations:
        logger.warning(
            "write-audit: %d out-of-org write(s) by %s: %s",
            len(report.violations),
            report.login,
            report.violations,
        )
        notify(report, settings)
    else:
        logger.info(
            "write-audit: clean (%d repos checked for %s)", len(report.checked_repos), report.login
        )
    return {"status": "ok", "report": report.as_dict()}
