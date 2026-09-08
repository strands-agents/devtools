"""AWS mention poller — strandly's GitHub ``@mention`` ingress trigger.

strandly's deployed runtime is fire-and-forget; it needs something to call ``InvokeAgentRuntime``
when an authorized user asks for it. This module is that trigger: an EventBridge-scheduled Lambda
that polls the GitHub Notifications API for ``@mention`` requests and dispatches the deployed runtime
fire-and-forget (EventBridge Scheduler for the tick; ``InvokeAgentRuntime`` for the dispatch;
``last_read_at`` **plus** a durable DynamoDB backstop for dedup).

Algorithm:

1. **Poll** ``GET /notifications?all=false&participating=true&per_page=50`` with a PAT that can read
   cross-repo notifications.
2. **Filter** to ``reason in {"mention","team_mention"}``.
3. For each, **skip the agent's own repo** (direct events handle those), else fetch the subject and
   **search** for ``@<handle>`` across (a) issue/PR body, (b) comments, (c) PR review bodies, and
   (d) PR review line-comments — capturing the mention author, source, and timestamp (``updated_at``
   so edits count). The **newest** mention across all locations wins (not the highest-precedence
   one), so a follow-up comment isn't shadowed by an older body mention.
4. **Authorize**: the mention author must be in the allow-list **or** a member of an allowed org
   (the org-membership invoke gate); an unknown author on a ``reason=mention`` is skipped for
   security.
5. **Dedup (fail-open)**: skip dispatch if the mention isn't newer than the thread's ``last_read_at``
   *or* if the DynamoDB backstop already recorded a dispatch ≥ this mention; a missing/unparseable
   timestamp dispatches anyway (never drop a genuinely new/edited mention).
6. **Build** a rich prompt + a stable session id (``gh-<repo-slug>-pr-N`` / ``-issue-N``).
7. **Dispatch** the deployed runtime fire-and-forget. The invoke result is **checked**: only an
   explicit acceptance records the (pre-written) dedup backstop and marks the notification read; a
   rejection (HTTP-200 ``{"status":"error"}``) rolls back the backstop and leaves the notification
   unread so the next poll retries (fail-closed — a rejected invoke never silently consumes a
   mention).

Design matches the rest of the harness: HTTP goes through stdlib ``urllib.request`` (no new dep; the
``_request`` seam is the only network call tests monkeypatch), boto3 is imported lazily, and the
dispatch reuses strandly's own ``runtime_client.launch_run`` + ``deploy`` ARN/region resolution
rather than hand-rolling ``bedrock-agentcore``. Every helper that does I/O is best-effort so one bad
notification can't sink a poll.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from strandly_harness.core.config import Config, MentionPollerSettings
from strandly_harness.ops import metrics
from strandly_harness.ops.lambdas.mention_poller import dedup, mention_log
from strandly_harness.ops.lambdas.mention_poller.sessions import (
    KIND_ISSUE,
    KIND_PR,
    canonical_session_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
_USER_AGENT = "strandly-mention-poller/1.0"
_MENTION_REASONS = {"mention", "team_mention"}
# Truncation cap for the mention body folded into the dispatch prompt.
MENTION_BODY_MAX = 2000
# Sort sentinel for "no/unparseable timestamp" so such a candidate sorts oldest (never wins on time).
_TS_MIN = datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# HTTP seam (stdlib urllib; the only function tests monkeypatch for the network)
# ---------------------------------------------------------------------------
def _is_github_api_url(url: str) -> bool:
    """Only ``https://api.github.com/...`` URLs are addressable.

    Notification/content URLs come from the API payload; this guard ensures a doctored
    ``subject.url`` / ``comments_url`` can never make us send the PAT to an arbitrary host.
    """
    return url.startswith(f"{GITHUB_API}/") or url == GITHUB_API


def _request(method: str, url: str, token: str, *, parse: bool = True) -> Any:
    """One GitHub REST call. Returns parsed JSON (GET) or None (PATCH/no-content)."""
    if not _is_github_api_url(url):
        raise ValueError(f"refusing to send token to non-GitHub-API URL: {url!r}")
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", _USER_AGENT)
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — host guarded above
        if not parse:
            return None
        body = resp.read().decode()
        return json.loads(body) if body else None


def _get(url: str, token: str) -> Any:
    """GET an absolute GitHub API URL, returning parsed JSON (None on any error — fail soft).

    Catches ``OSError`` (covers ``URLError`` *and* a read ``TimeoutError``) and ``ValueError``
    (covers ``JSONDecodeError`` and the non-GitHub-URL guard) so a flaky call never escapes.
    """
    if not url:
        return None
    try:
        return _request("GET", url, token)
    except (OSError, ValueError) as e:
        logger.warning("github GET failed for %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Step 4b: org-membership invoke gate (an ADDITIONAL authorizer alongside the static allow-list)
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect handler that never follows redirects.

    The org-membership endpoint returns ``302`` (to the *public* members list) when the *token's*
    own account is not a member of the org — i.e. it can't see private membership. Following that
    redirect could turn an inconclusive answer into a misleading ``2xx``; refusing to follow makes
    the raw ``302`` surface as an :class:`~urllib.error.HTTPError` so the caller fails closed on
    anything that isn't an explicit ``204``.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
        return None


def _membership_request(org: str, login: str, token: str) -> int:
    """One org-membership REST call: ``GET /orgs/{org}/members/{login}``; returns the HTTP status.

    GitHub signals membership by *status code*, not a body: ``204`` = member, ``404`` = not a
    member, ``302`` = the token's account can't see this org's private membership. We return the
    raw status (redirects deliberately NOT followed) so the caller can require an exact ``204``.
    Uses the same guarded stdlib-urllib seam as :func:`_request`; raises on a network error.
    """
    url = f"{GITHUB_API}/orgs/{org}/members/{login}"
    if not _is_github_api_url(url):  # defense-in-depth; org/login come from our own config/payload
        raise ValueError(f"refusing to send token to non-GitHub-API URL: {url!r}")
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", _USER_AGENT)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=30) as resp:  # noqa: S310 — host guarded above
            return resp.status
    except urllib.error.HTTPError as e:
        # 404 (not a member), 403 (forbidden), 302 (can't see private membership) — surface the code
        # so the caller can treat anything but 204 as "not a member" (fail closed).
        return e.code


# Short-TTL in-memory cache for membership lookups, keyed by (login, org) (both lowercased). A poll
# may re-check the same author across notifications; this avoids hammering the API within one run.
_ORG_MEMBER_CACHE: dict[tuple[str, str], tuple[float, bool]] = {}
_ORG_MEMBER_CACHE_TTL_SECONDS = 300.0


def is_org_member(
    login: str | None,
    orgs: Iterable[str],
    token: str | None,
    *,
    request: Callable[[str, str, str], int] | None = None,
) -> bool:
    """True iff ``login`` is a member of ANY org in ``orgs`` — the org-membership invoke gate.

    Checks ``GET /orgs/{org}/members/{login}`` per org (``204`` = member) and returns ``True`` on
    the first ``204``. **FAIL-CLOSED**: any error or uncertainty — a non-204 status (``404``,
    ``403``, a ``302`` redirect, or any other code), a network error, or anything unparseable —
    yields ``False``. An org check can only ever *grant* access (in addition to the static
    allow-list), never produce a false positive that authorizes a stranger.

    Empty/falsy ``login``, ``orgs`` or ``token`` short-circuit to ``False`` (no network call), so an
    empty ``allowed_orgs`` cleanly means "no org gating" without raising. Results are cached per
    ``(login, org)`` for a short TTL. ``request`` defaults to the module's urllib seam
    (:func:`_membership_request`); it's resolved at call time so tests can monkeypatch that seam.
    """
    if not login or not token:
        return False
    do_request = request if request is not None else _membership_request
    for org in orgs or ():
        if not org:
            continue
        key = (login.lower(), org.lower())
        cached = _ORG_MEMBER_CACHE.get(key)
        if cached is not None and cached[0] > time.monotonic():
            member = cached[1]
        else:
            try:
                member = do_request(org, login, token) == 204
            except Exception as e:  # noqa: BLE001 — fail closed: ANY uncertainty denies the org path
                logger.warning("org-membership check failed for @%s in %s: %s", login, org, e)
                member = False
            _ORG_MEMBER_CACHE[key] = (time.monotonic() + _ORG_MEMBER_CACHE_TTL_SECONDS, member)
        if member:
            return True
    return False


# ---------------------------------------------------------------------------
# Steps 1–2: poll + filter notifications
# ---------------------------------------------------------------------------
def fetch_notifications(token: str) -> list[dict[str, Any]]:
    """Step 1: unread, participating notifications (mentions surface here)."""
    url = f"{GITHUB_API}/notifications?all=false&participating=true&per_page=50"
    data = _get(url, token)
    return data if isinstance(data, list) else []


def mention_notifications(notifications: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Step 2: keep only ``reason in {mention, team_mention}``."""
    return [n for n in notifications if (n or {}).get("reason") in _MENTION_REASONS]


# ---------------------------------------------------------------------------
# Step 3: search the subject for the @handle (author + source + timestamp)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Mention:
    """A located ``@handle`` mention: who said it, where, when, and the text said."""

    author: str
    source: str  # "body" | "comment" | "review (STATE)" | "line comment on PATH"
    timestamp: str  # updated_at / created_at / submitted_at (edits count via updated_at)
    body: str


def _mentions_handle(text: str | None, handle: str) -> bool:
    """Case-insensitive check for ``@handle`` in ``text`` (literal, not a fuzzy match)."""
    if not text or not handle:
        return False
    return f"@{handle}".lower() in text.lower()


def _item_ts(item: dict[str, Any], *keys: str) -> str:
    """First non-empty timestamp from ``keys`` (e.g. updated_at, created_at, submitted_at)."""
    for key in keys:
        value = item.get(key)
        if value:
            return str(value)
    return ""


def _latest_match(
    items: Iterable[dict[str, Any]], handle: str, *ts_keys: str
) -> dict[str, Any] | None:
    """The most recent item whose ``body`` mentions ``@handle`` (by the given timestamp keys)."""
    matches = [it for it in (items or []) if _mentions_handle(it.get("body"), handle)]
    if not matches:
        return None
    return max(matches, key=lambda it: _item_ts(it, *ts_keys))


def select_mention(
    *,
    content: dict[str, Any],
    comments: Iterable[dict[str, Any]],
    reviews: Iterable[dict[str, Any]],
    review_comments: Iterable[dict[str, Any]],
    handle: str,
    is_pull_request: bool,
) -> Mention | None:
    """Locate the *newest* ``@handle`` mention across all four locations.

    Pure: takes already-fetched data so it is fully unit-testable without network. We collect at
    most one candidate per location (body, comment, review body, review line-comment) — the latest
    matching item *within* that location — then return the **newest candidate across all locations**
    (max ``updated_at``/``submitted_at``).

    Newest-wins (not precedence-wins) is the dedup-correct choice: the staleness/dedup gate in
    ``process_notification`` checks only the selected mention's timestamp against ``last_read_at``,
    so picking the highest-*precedence* location would let an old body mention shadow a newer
    follow-up comment — the follow-up would test as stale and be dropped. Ties (identical or
    unparseable timestamps) fall back to location precedence (body → comment → review → line) via
    the order candidates are appended, so the previous behaviour is preserved when timestamps don't
    disambiguate.
    """
    candidates: list[Mention] = []

    # (a) issue/PR body. Timestamp from ``created_at`` ONLY — NOT ``updated_at``: GitHub bumps an
    # issue/PR's ``updated_at`` on *any* activity (a new comment, a label, a review), so using it
    # would make the body mention perpetually "fresh" and let it win the newest-wins tie-break over
    # a genuine follow-up comment posted at the same time — selecting the PR *opener* as the author
    # instead of the commenter who actually invoked the bot (e.g. a PR whose body merely links
    # ``@handle`` gets attributed to its author, not to the maintainer who commented "review this").
    # The body text is fixed at creation for our purposes; a later body edit to add a mention is a
    # rare case better served by a comment anyway.
    if _mentions_handle(content.get("body"), handle):
        candidates.append(
            Mention(
                author=(content.get("user") or {}).get("login") or "",
                source="body",
                timestamp=_item_ts(content, "created_at"),
                body=content.get("body") or "",
            )
        )

    # (b) issue/PR comments
    comment = _latest_match(comments, handle, "updated_at", "created_at")
    if comment:
        candidates.append(
            Mention(
                author=(comment.get("user") or {}).get("login") or "",
                source="comment",
                timestamp=_item_ts(comment, "updated_at", "created_at"),
                body=comment.get("body") or "",
            )
        )

    if is_pull_request:
        # (c) PR review bodies
        review = _latest_match(reviews, handle, "submitted_at")
        if review:
            state = review.get("state") or ""
            candidates.append(
                Mention(
                    author=(review.get("user") or {}).get("login") or "",
                    source=f"review ({state})",
                    timestamp=_item_ts(review, "submitted_at"),
                    body=review.get("body") or "",
                )
            )

        # (d) PR review line-comments
        line = _latest_match(review_comments, handle, "updated_at", "created_at")
        if line:
            candidates.append(
                Mention(
                    author=(line.get("user") or {}).get("login") or "",
                    source=f"line comment on {line.get('path') or ''}".rstrip(),
                    timestamp=_item_ts(line, "updated_at", "created_at"),
                    body=line.get("body") or "",
                )
            )

    if not candidates:
        return None
    # Newest across all locations. ``max`` returns the FIRST item among equal keys, and candidates
    # are appended in precedence order, so a tie (equal or unparseable timestamps → _TS_MIN) breaks
    # toward the higher-precedence location.
    return max(candidates, key=lambda m: _parse_ts(m.timestamp) or _TS_MIN)


def gather_subject(subject_url: str, is_pull_request: bool, token: str) -> dict[str, Any]:
    """Fetch the subject and all of its mention-bearing locations (network, fail-soft)."""
    content = _get(subject_url, token) or {}
    comments_url = content.get("comments_url")
    comments = _get(f"{comments_url}?per_page=20&sort=created&direction=desc", token) if comments_url else []
    reviews: Any = []
    review_comments: Any = []
    if is_pull_request:
        reviews = _get(f"{subject_url}/reviews?per_page=20", token) or []
        review_comments = (
            _get(f"{subject_url}/comments?per_page=20&sort=created&direction=desc", token) or []
        )
    return {
        "content": content,
        "comments": comments if isinstance(comments, list) else [],
        "reviews": reviews if isinstance(reviews, list) else [],
        "review_comments": review_comments if isinstance(review_comments, list) else [],
    }


# ---------------------------------------------------------------------------
# Step 5: dedup (last_read_at primary, DynamoDB backstop) — both fail-open
# ---------------------------------------------------------------------------
def _parse_ts(value: str | None) -> datetime | None:
    if not value or value == "null":
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    # Coerce naive → UTC so comparisons never raise on a naive-vs-aware mismatch.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_stale(mention_ts: str | None, last_read_at: str | None) -> bool:
    """True iff the mention isn't newer than ``last_read_at`` (already handled).

    Fail-open: a missing/unparseable timestamp on either side returns ``False`` (→ dispatch), so the
    gate can only suppress a re-surfaced old mention, never drop a new or edited one.
    """
    mention_dt = _parse_ts(mention_ts)
    last_read_dt = _parse_ts(last_read_at)
    if mention_dt is None or last_read_dt is None:
        return False
    return mention_dt <= last_read_dt


# ---------------------------------------------------------------------------
# Step 6: build the dispatch prompt + a stable session id
# ---------------------------------------------------------------------------
def build_session_id(repo: str, is_pull_request: bool, number: int | str) -> str:
    """Stable, deterministic id so multiple polls of one thread share a conversation.

    Delegates to the canonical, ingress-agnostic scheme (:mod:`strandly_harness.ops.lambdas.mention_poller.sessions`):
    ``gh-<owner>-<repo>-{pr,issue}-N``. Keeping every ingress on that one helper is what makes a
    mention and a ``strandly invoke`` from a GitHub Action land in the *same* session for the same
    item.
    """
    kind = KIND_PR if is_pull_request else KIND_ISSUE
    return canonical_session_id(repo, kind, number)


def _html_url(repo: str, is_pull_request: bool, number: int | str) -> str:
    kind = "pull" if is_pull_request else "issues"
    return f"https://github.com/{repo}/{kind}/{number}"


def build_prompt(
    mention: Mention, repo: str, is_pull_request: bool, number: int | str, now: datetime
) -> str:
    """The rich, anti-dedup dispatch prompt (mention text + parent URL, truncated)."""
    url = _html_url(repo, is_pull_request, number)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    body = mention.body
    if len(body) > MENTION_BODY_MAX:
        body = body[:MENTION_BODY_MAX] + "... (truncated)"
    lines = [
        f"NEW mention at {ts} by @{mention.author} in {mention.source} of {repo}#{number}.",
        f"URL: {url}",
        "",
    ]
    if body:
        lines += [f"What @{mention.author} said:", body, ""]
    lines += [
        "This is a NEW trigger with new content above. Do NOT dismiss as duplicate.",
        "Check the PR/issue for all recent comments and reviews, then respond appropriately.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 7: dispatch (fire-and-forget) + mark read
# ---------------------------------------------------------------------------
def dispatch(settings: MentionPollerSettings, session_id: str, prompt: str) -> dict[str, Any]:
    """Invoke the deployed runtime fire-and-forget via strandly's own ``launch_run``.

    No GitHub context is passed: the deployed two-modes runtime reports results out of band to
    AgentCore Memory, so the poller is a pure trigger. Reusing ``launch_run`` (not raw boto) keeps
    the runtime-session-id padding + payload shape in one place. The serving imports are lazy: they
    pull the Strands SDK transitively, so a poll that dispatches nothing never imports it.
    """
    from strandly_harness.ops import runtime_client

    arn = runtime_client.resolve_runtime_arn(settings.runtime_arn)
    region = runtime_client.resolve_region(settings.region)
    if not arn or not region:
        raise RuntimeError(
            f"cannot dispatch: runtime_arn={arn!r} region={region!r} (set STRANDLY_RUNTIME_ARN / AWS_REGION)"
        )
    # github_context is intentionally empty → fire-and-forget. NOTE (deploy ordering): the deployed
    # runtime only accepts an empty GitHub context once PR #6 (drop-github-gate) is merged AND the
    # runtime is redeployed; until then it returns {"status": "error", ...} and the caller must
    # treat that as a rejection (see _dispatch_accepted / process_notification) rather than success.
    return runtime_client.launch_run(arn, region, session_id, prompt, {})


def _dispatch_accepted(result: Any) -> bool:
    """True iff a fire-and-forget dispatch was *accepted* by the deployed runtime.

    ``runtime_client.launch_run`` returns a **dict for any HTTP-200 body**, including a rejection
    like ``{"status": "error", ...}`` (e.g. the runtime's GitHub-context gate on un-redeployed
    ``main``). Only the documented success shape (``status == "accepted"``, which also carries a
    ``taskId``) counts as accepted; everything else (an error body, a non-dict, a missing/garbled
    payload) is treated as a failure so the mention is **retried, not silently consumed**.
    """
    return isinstance(result, dict) and result.get("status") == "accepted"


def mark_read(thread_id: str, token: str) -> None:
    """PATCH a notification thread read. Best-effort — a failure must not abort the poll."""
    if not thread_id:
        return
    try:
        _request("PATCH", f"{GITHUB_API}/notifications/threads/{thread_id}", token, parse=False)
    except (OSError, ValueError) as e:  # URLError/TimeoutError + the URL guard; never fail the poll
        logger.warning("mark_read failed for thread %s: %s", thread_id, e)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _dynamodb_client(config: Config) -> Any | None:
    """A DynamoDB client for the backstop + mention-log tables, or None if neither is configured."""
    if not (config.mention_poller.dedup_table or config.mention_poller.mention_log_table):
        return None
    session = config.boto_session()
    if session is not None:
        return session.client("dynamodb")
    import boto3

    return boto3.client("dynamodb", region_name=config.aws_region)


def process_notification(
    notification: dict[str, Any],
    *,
    settings: MentionPollerSettings,
    token: str,
    ddb_client: Any | None,
    now: datetime,
) -> str:
    """Process one notification end-to-end; returns a short outcome label.

    Outcomes: ``skipped-own-repo``, ``no-mention``, ``unauthorized``, ``stale``, ``duplicate``,
    ``dispatched``, ``dispatch-error``. Every terminal outcome marks the notification read
    **except two**: ``dispatch-error`` — a rejected/failed invoke is left unread (and its backstop
    intent rolled back) so the next poll retries instead of silently dropping the mention
    (fail-closed) — and ``duplicate`` — a concurrent poll won the conditional intent write (RC-1)
    and owns both the dispatch and the mark-read, so we must not touch either.
    """
    thread_id = str(notification.get("id") or "")
    repo = (notification.get("repository") or {}).get("full_name") or ""
    subject = notification.get("subject") or {}
    subject_type = subject.get("type") or ""
    subject_url = subject.get("url") or ""
    last_read_at = notification.get("last_read_at")
    is_pr = subject_type == "PullRequest"

    # Step 3 (skip own repo): direct events handle those; still mark read so it doesn't resurface.
    if settings.skip_repo and repo == settings.skip_repo:
        mark_read(thread_id, token)
        return "skipped-own-repo"

    gathered = gather_subject(subject_url, is_pr, token)
    content = gathered["content"]
    number = content.get("number")

    mention = select_mention(
        content=content,
        comments=gathered["comments"],
        reviews=gathered["reviews"],
        review_comments=gathered["review_comments"],
        handle=settings.handle,
        is_pull_request=is_pr,
    )

    # Step 4 (authorize). No identifiable author on a mention reason → skip for security.
    if mention is None or not mention.author:
        mark_read(thread_id, token)
        return "no-mention"

    def _log(outcome: str, *, authorized: bool, session_id: str | None = None) -> None:
        """Fail-open mention-log write (dashboard Mentions tab) — never blocks the outcome."""
        mention_log.record(
            ddb_client,
            settings.mention_log_table,
            thread_id=thread_id,
            outcome=outcome,
            authorized=authorized,
            author=mention.author,
            repo=repo,
            number=number,
            is_pull_request=is_pr,
            mention_ts=mention.timestamp,
            body=mention.body,
            url=_html_url(repo, is_pr, number) if repo and number is not None else None,
            session_id=session_id,
            now=now,
        )
    # Authorized if EITHER the static allow-list OR org membership says so. The static list is
    # checked first (no network); only if it fails do we consult the org-membership gate (an
    # ADDITIONAL grant — it can never override an explicit allow, and fails closed on any error).
    if not settings.is_authorized(mention.author) and not is_org_member(
        mention.author, settings.allowed_orgs, token
    ):
        logger.info("unauthorized mention author @%s in %s#%s — skipping", mention.author, repo, number)
        _log("unauthorized", authorized=False)
        mark_read(thread_id, token)
        return "unauthorized"

    # Step 5 (dedup, fail-open): last_read_at primary signal + DynamoDB durable backstop.
    if is_stale(mention.timestamp, last_read_at) or dedup.already_dispatched(
        ddb_client, settings.dedup_table, thread_id, mention.timestamp
    ):
        _log("stale", authorized=True)
        mark_read(thread_id, token)
        return "stale"

    # Steps 6–7: build + dispatch fire-and-forget (fail-closed).
    session_id = build_session_id(repo, is_pr, number)
    prompt = build_prompt(mention, repo, is_pr, number, now)

    # MEDIUM-4 (narrow the re-fire window): record the dispatch *intent* in the durable backstop
    # BEFORE invoking. If we then crash after a successful invoke but before mark_read, the next
    # poll sees the backstop and suppresses the re-dispatch (no double-fire into the same session).
    # RC-1 (TOCTOU): the write is CONDITIONAL — if a concurrent poll won the intent row between our
    # already_dispatched read and this write, it is dispatching this very mention right now. Skip,
    # and deliberately do NOT mark_read: the winner marks read on its success, or rolls back and
    # leaves the notification unread for retry on its failure. Either way the mention is handled
    # exactly once — we must not double-fire into the same live session, nor consume the
    # notification out from under the winner's failure path.
    if not dedup.record_dispatch(ddb_client, settings.dedup_table, thread_id, mention.timestamp):
        logger.info(
            "dedup: concurrent poll already dispatching %s#%s (thread %s) — skipping duplicate",
            repo,
            number,
            thread_id,
        )
        _log("duplicate", authorized=True)
        return "duplicate"

    try:
        result = dispatch(settings, session_id, prompt)
    except Exception:
        # HIGH-A (fail-closed on the *exception* path too): a raised dispatch — an unresolved
        # ARN/region RuntimeError, or a boto throttle/timeout/5xx from launch_run's live invoke —
        # must NOT leave the intent row we just wrote orphaned. Otherwise the next poll would read
        # it as already_dispatched → "stale" → mark_read and silently consume the genuine mention.
        # Roll the intent back, then re-raise so poll_once records this tick as "error" (and skips
        # mark_read), leaving the notification to be re-dispatched on the next poll.
        dedup.clear_dispatch(ddb_client, settings.dedup_table, thread_id)
        raise

    # HIGH-1 (fail-closed): a fire-and-forget invoke returns a dict even for an HTTP-200 error body,
    # so a rejected invoke must NOT be treated as success. On rejection: roll back the backstop
    # intent we just wrote and leave the notification UNREAD so the next poll re-dispatches —
    # rather than recording dedup + marking read and losing the mention forever.
    if not _dispatch_accepted(result):
        logger.error(
            "dispatch REJECTED for %s in %s#%s (result=%r); rolling back backstop and leaving "
            "unread so it retries (is the runtime redeployed past PR #6's github-context gate?)",
            session_id,
            repo,
            number,
            result,
        )
        dedup.clear_dispatch(ddb_client, settings.dedup_table, thread_id)
        _log("dispatch-error", authorized=True, session_id=session_id)
        return "dispatch-error"

    logger.info("dispatched %s for @%s in %s#%s", session_id, mention.author, repo, number)
    _log("dispatched", authorized=True, session_id=session_id)
    mark_read(thread_id, token)
    return "dispatched"


def _emit_poll_metrics(summary: dict[str, Any]) -> None:
    """Emit poller-health EMF after a completed poll. The key metric is ``PollSuccess`` (=1): the
    poller is fail-soft, so a silently dead trigger leaves no error — an alarm on "no PollSuccess in
    30 min" is how we learn we stopped answering mentions. ``DispatchFailed`` rolls up the
    fail-closed paths (a rejected invoke + a per-notification error) that leave a mention unread for
    retry. Fail-open + a no-op when metrics are disabled."""
    counts = summary.get("counts", {}) if isinstance(summary, dict) else {}
    doc = {
        metrics.POLL_SUCCESS: 1,
        metrics.NOTIFICATIONS_FETCHED: int(summary.get("processed", 0) or 0),
        metrics.DISPATCHED: int(counts.get("dispatched", 0) or 0),
        metrics.DISPATCH_FAILED: int(counts.get("dispatch-error", 0) or 0)
        + int(counts.get("error", 0) or 0),
        metrics.UNAUTHORIZED: int(counts.get("unauthorized", 0) or 0),
    }
    metrics.emit(doc, surface=metrics.SURFACE_POLLER)


def poll_once(config: Config | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    """Run one poll cycle. Returns a summary dict of per-outcome counts (the Lambda return value)."""
    config = config or Config.load()
    if not config.poller_enabled:
        logger.warning("mention poller disabled (need notifications token + runtime ARN); no-op")
        return {"status": "disabled", "counts": {}}

    settings = config.mention_poller
    if not settings.handle:
        logger.error("mention poller: no STRANDLY_MENTION_HANDLE configured; nothing to search for")
        return {"status": "error", "error": "no mention handle configured", "counts": {}}

    token = config.notifications_token
    assert token is not None  # guaranteed by poller_enabled
    now = now or datetime.now(timezone.utc)
    ddb_client = _dynamodb_client(config)

    counts: dict[str, int] = {}
    notifications = mention_notifications(fetch_notifications(token))
    logger.info("mention poller: %d mention notification(s) to process", len(notifications))
    for notification in notifications:
        try:
            outcome = process_notification(
                notification, settings=settings, token=token, ddb_client=ddb_client, now=now
            )
        except Exception as e:  # noqa: BLE001 — one bad notification must not sink the whole poll
            logger.exception("error processing notification %s: %s", notification.get("id"), e)
            outcome = "error"
        counts[outcome] = counts.get(outcome, 0) + 1

    summary = {"status": "ok", "processed": len(notifications), "counts": counts}
    _emit_poll_metrics(summary)
    return summary


def lambda_handler(event: dict[str, Any] | None, context: Any | None = None) -> dict[str, Any]:
    """AWS Lambda entrypoint — invoked by the EventBridge schedule. ``event``/``context`` unused."""
    summary = poll_once()
    logger.info("mention poller summary: %s", summary)
    return summary
