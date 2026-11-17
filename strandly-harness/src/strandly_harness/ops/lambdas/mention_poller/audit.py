"""Independent GitHub write-audit — does our token only act inside the allowed org?

This is a **safety check, not a metric**. The ``use_github`` tool already enforces an owner
allow-list *in-band* (``tools/github.py``), but an in-band guardrail is blind to exactly the cases
that matter most: a leaked token used elsewhere, a bug that bypasses ``validate_owner``, or a prompt
injection that talks the agent into a path the guardrail doesn't cover. Anything that sidesteps our
own code is, by definition, invisible to a metric our own code emits.

So this module asks GitHub **directly, out of band**: *what did this account actually do across all
of GitHub in the last window, and is any of it outside the owners we allow?* It is meant to run on
its own schedule (an EventBridge-triggered Lambda), independently of the agent runtime, ideally with
its **own read-only audit token** — so it still works if the agent's write token is the thing that
leaked.

How it asks (GraphQL-led, REST backstop):

1. **GraphQL** ``viewer { login, contributionsCollection(from, to) { … } }`` enumerates every repo
   the token's account *contributed* to in the window — issues, PRs, PR reviews, and commits — each
   carrying ``repository { nameWithOwner }``. One call, scoped to the token's own identity.
2. **REST** ``/users/{login}/events`` is the comment backstop: ``contributionsCollection`` does not
   surface plain issue/PR *comments* on existing threads (the agent's most common write), but the
   public events feed does (``IssueCommentEvent`` etc.) — the same source the in-band throttle
   already uses. We union the two for full write coverage.

**Known blind spots (best-effort, not airtight).** Some write paths appear in *neither* source and
are therefore not caught: discussion comments, gists, releases, and wiki (Gollum) edits. The REST
events feed is additionally **public-only** and capped (``per_page=100``, no pagination), so a
comment on a *private* out-of-org repo, or activity in a window busier than 100 events, can be
missed. The audit is a high-value backstop, not a complete one — treat a clean result as "no
out-of-org write was *observed* in the covered surfaces," not a proof of none.

Then :func:`find_violations` flags any repo whose owner is not in the configured allow-list. A
finding is the alert — surfaced via SNS when a topic is configured (and always logged/returned),
driven by **GitHub's own record of what the token did**, not by anything we self-report.

Design matches the rest of ``ingress/``: HTTP goes through stdlib ``urllib.request`` (no new dep; the
``_request`` seam is the only network call tests monkeypatch), boto3 is imported lazily, and every
network helper is fail-soft so one bad response can't sink the audit (a source that errors is
recorded in ``errors`` and simply contributes no repos — it never raises).
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from strandly_harness.core.config import AuditSettings

logger = logging.getLogger(__name__)

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
GITHUB_REST_URL = "https://api.github.com"
_USER_AGENT = "strandly-write-audit/1.0"

# Public-events feed types that represent a *write* by the account (mirrors the in-band throttle's
# set in ``tools/github.py``). These are the comment/push/branch actions ``contributionsCollection``
# either omits (plain comments) or we want a second independent source for.
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
    }
)

# The contributions query: the token's own identity (`viewer`) + every repo it touched in the
# window. `first: 100` / `maxRepositories: 100` is far above a normal window's activity; if an
# account ever exceeds it the audit still sees the first 100 repos (a violation in the tail would
# surface on the next, shorter window) — we deliberately don't paginate to keep one cheap call.
_CONTRIBUTIONS_QUERY = """
query($from: DateTime!, $to: DateTime!) {
  viewer {
    login
    contributionsCollection(from: $from, to: $to) {
      issueContributions(first: 100) { nodes { issue { repository { nameWithOwner } } } }
      pullRequestContributions(first: 100) { nodes { pullRequest { repository { nameWithOwner } } } }
      pullRequestReviewContributions(first: 100) { nodes { pullRequestReview { repository { nameWithOwner } } } }
      commitContributionsByRepository(maxRepositories: 100) { repository { nameWithOwner } }
    }
  }
}
"""


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
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", _USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed api.github.com host
        return json.loads(resp.read().decode())


def _graphql(query: str, variables: dict[str, Any], token: str) -> dict[str, Any]:
    return _request("POST", GITHUB_GRAPHQL_URL, token, {"query": query, "variables": variables})


def _rest_get(path: str, token: str) -> Any:
    return _request("GET", f"{GITHUB_REST_URL}{path}", token)


# ---------------------------------------------------------------------------
# Pure extractors (fully unit-testable, no network)
# ---------------------------------------------------------------------------
def _owner_of(name_with_owner: str) -> str:
    """The owner segment of an ``owner/repo`` string (``""`` if malformed)."""
    return name_with_owner.split("/", 1)[0] if "/" in name_with_owner else ""


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


def find_violations(repos: set[str], allowed_owners: set[str]) -> list[str]:
    """Repos whose owner is **not** in ``allowed_owners`` (case-insensitive), sorted.

    With an **empty** ``allowed_owners`` we cannot say what's in-org, so we return ``[]`` rather than
    flagging everything — the audit gate (:meth:`Config.audit_enabled`) requires a non-empty
    allow-list, so the Lambda never actually runs the check without one; this only guards the pure
    function against a caller that forgot.
    """
    if not allowed_owners:
        return []
    allowed_lower = {o.lower() for o in allowed_owners}
    return sorted(r for r in repos if _owner_of(r).lower() not in allowed_lower)


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
    return login, extract_repos_from_contributions(viewer)


def gather_events(login: str, token: str, *, since: datetime, errors: list[str]) -> set[str]:
    """Write-event repos from the REST events feed for ``login``. Fail-soft via ``errors``."""
    if not login:
        return set()
    try:
        events = _rest_get(f"/users/{login}/events?per_page=100", token)
    except Exception as e:  # noqa: BLE001 — fail-soft
        errors.append(f"events fetch failed: {type(e).__name__}: {e}")
        return set()
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
    from strandly_harness.core.config import Config

    config = Config.load()
    if not config.audit_enabled:
        logger.info("write-audit: disabled (set STRANDLY_AUDIT_ALLOWED_OWNERS + a token)")
        return {"status": "disabled"}

    settings = config.audit
    report = audit(settings)
    if report.errors:
        # A degraded pass (a source errored) must not masquerade as "clean": with both sources down
        # we'd see zero violations over zero repos. Surface it on the warning channel so the
        # follow-up CDK stack can alarm on "audit could not fully check" distinctly from a finding.
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
