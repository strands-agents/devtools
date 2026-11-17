"""GitHub GraphQL API tool + its repository-scope guardrails.

`use_github` executes any GitHub GraphQL query or mutation (the universal-API pattern from the
strands-coder agent). It is **settings-aware**, so it cannot be a plain builtin: it carries the
GitHub guardrail settings (``settings.github``) and is injected by `build_agent` (like `spawn`),
then listed in the tool spec.

What this adds *on top of* the harness's existing gates
-------------------------------------------------------
The harness already gates *every* tool call through interventions — HITL (approve/interrupt) and
Cedar (policy authorization). Those gate **by tool name**, not by the repository a GraphQL mutation
actually targets, which is often hidden inside an opaque node id (`PR_kwDO…`). So this tool keeps
only the **GitHub-semantic** guardrails Cedar/HITL can't express:

1. **Owner allow-list** — when `github.allowed_owners` is set, queries/mutations may only target
   those users/orgs. Empty list = no owner restriction (rely on token scope + interventions).
2. **Node-id resolution** — for mutations, opaque node ids in the variables are resolved via the
   API to their repository owner before the mutation runs (port of strands-coder fix #57).
3. **Strict mutations** — a mutation whose target owner can't be verified is blocked rather than
   silently allowed (only when an allow-list is configured).
4. **Daily write throttle** — optional cap on writes to *external* repos (not in
   `internal_owners`), counted via the GitHub Events API. Off by default.

No new third-party dependency: HTTP goes through stdlib ``urllib.request`` (the rest of the
harness is dependency-lean). The two network helpers (`_graphql`, `_rest_get`) are the only
seams tests monkeypatch, keeping the suite network-free.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from strandly_harness.core.config import GitHubSettings

logger = logging.getLogger(__name__)

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
GITHUB_REST_URL = "https://api.github.com"
_USER_AGENT = "meta-harness-github-tool/1.0"

# ---------------------------------------------------------------------------
# Mutation detection
# ---------------------------------------------------------------------------
# Keywords that, appearing in a query body, flag a likely-mutative operation even when the caller
# mislabels query_type. Used to decide whether owner/throttle guardrails apply.
_MUTATIVE_KEYWORDS = (
    "create", "update", "delete", "add", "remove", "merge", "close", "reopen", "lock",
    "unlock", "pin", "unpin", "transfer", "archive", "unarchive", "enable", "disable",
    "accept", "decline", "dismiss", "submit", "request", "cancel", "convert",
)


# GraphQL "ignored tokens" that may legally precede the operation keyword: the UTF-8 BOM
# (``\ufeff``), whitespace / line terminators, commas (insignificant in GraphQL), and comments
# (``#`` to end-of-line). A body whose first *significant* token is ``mutation`` is a mutation no
# matter how the caller labels it, so these leading tokens are stripped (repeatedly) BEFORE the
# keyword check. Missing this let ``# comment\nmutation {...}`` / ``,mutation`` / BOM-prefixed
# bodies sent with ``query_type="query"`` skip the guardrail entirely (fail-OPEN → allow-list
# bypass). We now fail CLOSED: strip the noise, then classify on the real leading token.
#
# Comment termination follows the GraphQL spec: ``CommentChar :: SourceCharacter but not
# LineTerminator``, and ``LineTerminator`` includes a BARE carriage return ``\r`` (U+000D) as
# well as ``\n`` and ``\r\n`` (cf. graphql-js ``readComment`` stopping at 0x000A OR 0x000D,
# and graphql-ruby's lexer ending a comment at ``\r``). The comment char class therefore
# excludes BOTH ``\n`` and ``\r`` and the terminator alternation matches ``\r\n|\n|\r|$``.
# Without the bare-CR case, ``# x\rmutation{...}`` (query_type="query") had its ``\r`` and the
# following ``mutation`` keyword swallowed into the "comment", stripping to '' → misclassified as
# a READ → owner allow-list / node-id resolution / strict-mode / throttle all SKIPPED (fail-open).
_LEADING_IGNORED_RE = re.compile(r"^(?:\ufeff|[\s,]+|#[^\n\r]*(?:\r\n|\n|\r|$))+")


def _strip_leading_ignored_tokens(text: str) -> str:
    """Strip all leading GraphQL ignored tokens (BOM, whitespace, commas, ``#`` comments)."""
    prev: str | None = None
    cur = text
    # Iterate to a fixed point so interleaved comment/whitespace/comma runs are fully removed.
    while cur != prev:
        prev = cur
        cur = _LEADING_IGNORED_RE.sub("", cur, count=1)
    return cur


def is_mutation_query(query: str, query_type: str = "") -> bool:
    """True if the operation is (or looks) mutative — drives the guardrail layers.

    Precedence (defense-in-depth, fail-closed, without over-flagging reads):
    1. A body whose first *significant* token is ``mutation`` is a mutation, whatever the caller
       claims — computed AFTER stripping leading GraphQL ignored tokens (BOM, whitespace, commas,
       ``#`` comment lines, where a comment ends at ``\n``, ``\r\n`` OR a bare ``\r`` per the
       GraphQL LineTerminator rule). This closes the classifier bypass where a leading ``#``-comment /
       comma / BOM made ``startswith("mutation")`` False and a ``query_type="query"`` label then
       short-circuited the mutation to a read, skipping the owner allow-list.
    2. An explicit ``query_type`` ("mutation"/"query") is then trusted — so a read query that merely
       *mentions* fields like ``pullRequest``/``reviewRequests``/``createdAt`` is NOT misclassified
       (that would wrongly subject reads to the throttle and strict node-id checks).
    3. Only when ``query_type`` is absent/unknown do we fall back to the keyword heuristic.
    """
    q = _strip_leading_ignored_tokens(query.lower())
    if q.startswith("mutation"):
        return True
    qt = query_type.lower().strip()
    if qt == "mutation":
        return True
    if qt == "query":
        return False
    return any(kw in q for kw in _MUTATIVE_KEYWORDS)


# ---------------------------------------------------------------------------
# Node-id detection / resolution (port of strands-coder github_guardrails.py)
# ---------------------------------------------------------------------------
# GitHub node ids look like `PR_kwDO…`, `I_kwDO…`: a TYPE prefix + base64-ish payload.
NODE_ID_PATTERN = re.compile(r"^[A-Z][A-Za-z]*_[a-zA-Z0-9+/=\-]{4,}$")

# Unanchored variant used to find node-id *literals embedded in an arbitrary string* — e.g. an
# inline `subjectId: "PR_kwDO…"` in a GraphQL query body (not just a discrete variable value).
# Same shape as NODE_ID_PATTERN; the leading payload char class excludes `_` so the match stops at
# the type-prefix separator and a single token is captured per id.
_NODE_ID_LITERAL_PATTERN = re.compile(r"[A-Z][A-Za-z]*_[a-zA-Z0-9+/=\-]{4,}")

# Prefixes for repository-scoped resources (resolvable to a repo owner).
REPO_SCOPED_PREFIXES = {
    "PR_", "I_", "IC_", "R_", "RE_", "RC_", "CC_", "DI_", "DC_", "LA_", "MI_",
}

# Legacy (pre-2022) GitHub global node ids carry NO ``TYPE_`` prefix — e.g.
# ``MDExOlB1bGxSZXF1ZXN0NTE0NjA3MDk5`` base64-decodes to ``011:PullRequest514607099``. The modern
# ``NODE_ID_PATTERN`` / inline-literal scan both require an underscore-prefixed type, so a legacy
# id slipped past BOTH extractors → an external legacy id (with empty/decoy variables) bypassed
# the owner allow-list. We detect them PRECISELY to avoid over-blocking: a token must be valid
# base64 whose *decoded* form matches the legacy shape ``<digits>:<TypeName><digits>``. Requiring a
# successful decode to that exact structure means ordinary base64-looking strings (opaque cursors,
# tokens, hashes) are NOT misclassified, so target-less / user-scoped mutations aren't over-blocked.
_LEGACY_DECODED_PATTERN = re.compile(r"^\d+:[A-Za-z][A-Za-z0-9]*\d+$")
# Candidate base64 run scanned out of a free-form query body (base64 alphabet, meaningful length).
_BASE64_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9+/]{8,}={0,2}")
_BASE64_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)


def _looks_like_legacy_node_id(value: str) -> bool:
    """True if ``value`` is a legacy (no-``TYPE_``-prefix) GitHub node id.

    Precise by construction: ``value`` must be valid base64 that decodes to the known legacy shape
    ``<digits>:<TypeName><digits>``. Ordinary base64-looking strings won't decode to that exact
    structure, so this stays over-block-safe (no false positives on cursors/tokens/hashes).
    """
    if not value or len(value) < 8 or len(value) % 4 != 0:
        return False
    if any(c not in _BASE64_ALPHABET for c in value):
        return False
    try:
        decoded = base64.b64decode(value, validate=True).decode("ascii")
    except Exception:  # noqa: BLE001 — any decode/charset failure => not a legacy id
        return False
    return bool(_LEGACY_DECODED_PATTERN.match(decoded))


def _looks_like_node_id(value: str, *, require_known_prefix: bool = False) -> bool:
    """True if ``value`` is a plausible GitHub node id (repo-scoped prefix or long opaque token).

    Shared acceptance predicate so a node id is recognised consistently wherever it appears —
    a discrete variable value (``extract_node_ids_from_variables``) or an inline literal scanned
    out of a query body (``extract_node_ids_from_query``).

    ``require_known_prefix`` tightens acceptance to literals carrying a recognised repo-scoped
    prefix (``PR_``/``I_``/``IC_``/…). It is used when scanning **free-form query text**, where the
    bare ``len >= 16`` fallback would false-positive on long ``SCREAMING_CASE`` GraphQL enum
    literals and could over-block a legitimate target-less mutation under strict mode. Repo-scoped
    resources — the actual write-authz target (and the only types ``resolve_node_owner`` resolves)
    — always carry such a prefix, so this stays precise while closing the inline-literal bypass.
    """
    if _looks_like_legacy_node_id(value):
        return True
    if not NODE_ID_PATTERN.match(value):
        return False
    if any(value.startswith(p) for p in REPO_SCOPED_PREFIXES):
        return True
    if require_known_prefix:
        return False
    return len(value) >= 16

# GraphQL variable keys that directly name a target owner.
_OWNER_KEYS = ("owner", "repositoryOwner", "organizationLogin", "login")


def extract_owner_from_variables(variables: dict[str, Any]) -> str | None:
    """Pull a repo owner from common GraphQL variable shapes (``owner``, ``owner/name``, …)."""
    for key in _OWNER_KEYS:
        value = variables.get(key)
        if isinstance(value, str) and value:
            return value
    repo = variables.get("repository")
    if isinstance(repo, str) and "/" in repo:
        return repo.split("/")[0]
    return None


def extract_target_from_variables(variables: dict[str, Any]) -> str | None:
    """The most specific target the variables name: ``owner/repo`` when both are present, else the
    bare ``owner``, else ``None``.

    The allow-list matcher needs the full ``owner/repo`` to honour a repo-scoped pattern like
    ``aws/bedrock-agentcore-*``; a bare owner only satisfies a whole-org (bare-owner) allow entry.
    Reuses :func:`extract_owner_from_variables` for the owner half and pairs it with the repo
    ``name`` variable (or a ``repository`` string that already carries ``owner/repo``).

    This is a best-effort target for the **explicit-variable** layer only — NOT the authoritative
    write check. GraphQL write mutations route by **node id** (``repositoryId``/``subjectId``/…),
    which is resolved to the real ``owner/repo`` and checked in ``validate_owner``'s node-id layer
    *before* this. So even if ``name`` here is an overloaded/unrelated field (a label or branch
    name rather than the repo) and synthesizes a wrong ``owner/repo``, it cannot authorize a write
    to an unlisted repo: any node-id target is blocked on its true owner regardless. And when only
    a bare owner can be derived, matching falls through to the bare owner — which fails **closed**
    against a repo-glob-only org (the intended strictness).
    """
    repo = variables.get("repository")
    if isinstance(repo, str) and "/" in repo:
        return repo
    owner = extract_owner_from_variables(variables)
    if owner is None:
        return None
    name = variables.get("name") or variables.get("repositoryName") or variables.get("repo")
    if isinstance(name, str) and name and "/" not in name:
        return f"{owner}/{name}"
    return owner


def extract_node_ids_from_variables(variables: dict[str, Any]) -> list[str]:
    """Recursively collect GitHub node ids from variable values (flat or nested in dicts/lists).

    The model may pass either flat (`{"subjectId": "PR_…"}`) or nested
    (`{"input": {"subjectId": "PR_…"}}`) variables; both must be scanned (strands-coder #57/#189).
    """
    found: list[str] = []

    def _scan(value: Any) -> None:
        if isinstance(value, str):
            if _looks_like_node_id(value):
                found.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                _scan(v)
        elif isinstance(value, list):
            for item in value:
                _scan(item)

    for v in variables.values():
        _scan(v)
    return found


def extract_node_ids_from_query(query: str) -> list[str]:
    """Collect GitHub node ids written *inline* in a GraphQL query/mutation body.

    The owner allow-list is only meaningful if it sees the mutation's real target. A model acting
    on untrusted issue/PR content can bypass variable-only inspection by inlining the target node
    id as a literal — e.g. ``mutation { addComment(input: {subjectId: "PR_kwDOExternal…"}) {…} }``
    with empty/decoy variables. Scanning the body for the same node-id literals closes that hole.

    Order-preserving and de-duplicated so resolution does not repeat work for a repeated id.
    """
    if not query:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in _NODE_ID_LITERAL_PATTERN.finditer(query):
        token = match.group(0)
        if token not in seen and _looks_like_node_id(token, require_known_prefix=True):
            seen.add(token)
            found.append(token)
    # Legacy ids carry no ``TYPE_`` prefix, so the pattern above misses them. Scan base64-shaped
    # literals and accept ONLY those that decode to the legacy node-id shape (over-block-safe).
    for match in _BASE64_TOKEN_PATTERN.finditer(query):
        token = match.group(0)
        if token not in seen and _looks_like_legacy_node_id(token):
            seen.add(token)
            found.append(token)
    return found


# Resolve an opaque node id to its repository owner via the `node` interface.
_RESOLVE_QUERY = """
query($id: ID!) {
  node(id: $id) {
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
  }
}
"""


def resolve_node_owner(node_id: str, token: str) -> str | None:
    """Resolve a node id to its repo target, or None if it can't be resolved.

    Returns the full ``owner/repo`` (``nameWithOwner``) — NOT just the owner — so the allow-list can
    match repo-scoped patterns like ``aws/bedrock-agentcore-*`` as well as bare owners. The matcher
    (:func:`_target_allowed`) accepts either form, so a bare-owner allow entry still grants the whole
    org. (Historically this returned only the owner; the extra repo half is additive.)
    """
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
        return None
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
    return None


def _target_allowed(target: str, allowed_owners: set[str]) -> bool:
    """True iff ``target`` is permitted by ``allowed_owners``. Case-insensitive.

    ``target`` is either a bare ``owner`` or a full ``owner/repo``. An allow entry is either:

    - **a bare owner** (no ``/``, e.g. ``strands-agents``) — matches any repo under that owner
      (whole-org grant), by comparing the target's owner half; or
    - **a repo glob** (contains ``/``, e.g. ``aws/bedrock-agentcore-*``) — matched with ``fnmatch``
      against the FULL ``owner/repo``, so it only ever grants specific repos.

    A repo glob can therefore match only when the repo half is known: an owner-only target (repo
    couldn't be determined) can satisfy a bare-owner entry but NEVER a repo glob — which is the
    fail-closed behavior we want (an unverifiable repo under an org we only allow specific repos of
    is denied). GitHub owner/repo names can't contain glob metacharacters, so patterns are safe.
    """
    t = target.lower()
    t_owner = t.split("/", 1)[0]
    for entry in allowed_owners:
        e = entry.lower()
        if "/" in e:
            if "/" in t and fnmatch.fnmatchcase(t, e):
                return True
        elif t_owner == e:
            return True
    return False


def validate_owner(
    variables: dict[str, Any],
    *,
    allowed_owners: set[str],
    is_mutative: bool,
    strict: bool,
    token: str | None,
    query: str = "",
) -> tuple[str | None, str | None]:
    """Validate the GraphQL target owner against the allow-list.

    Returns ``(error_message, resolved_owner)``: a non-None error blocks the call. When
    ``allowed_owners`` is empty the owner guardrail is OFF — everything is allowed (the resolved
    owner is still returned when cheaply known, for the throttle).

    Mutations are checked against **every** target they name — an ``owner``-shaped variable *and*
    any node id, whether that node id is a discrete variable value or a literal inlined in the
    ``query`` body. This matters because a model acting on untrusted content can (a) inline the
    node id with empty variables, or (b) pass a decoy allowed ``owner`` var while inlining an
    external node id. Both are caught here: the inline/variable node ids are resolved and a target
    outside the allow-list blocks the call regardless of any decoy ``owner`` var, and a mutation
    whose node-id target can't be resolved is blocked under ``strict`` even when ``variables`` is
    empty. A genuinely target-less mutation (no ``owner`` var, no node id anywhere) is still
    allowed, so user-/schema-scoped mutations are not over-blocked.
    """
    explicit = extract_owner_from_variables(variables)
    explicit_target = extract_target_from_variables(variables)

    # Guardrail off: no allow-list configured.
    if not allowed_owners:
        return None, explicit

    allowed_str = ", ".join(sorted(allowed_owners))

    # Collect every node-id target the mutation names — from the variables AND inlined in the
    # query body. Only mutations get the heavier node-id resolution / strict treatment; reads are
    # owner-checked via the explicit variable alone (no extra network, no strict block). Order-
    # preserving de-dup so a node id repeated across variables/body is resolved once.
    node_ids: list[str] = []
    if is_mutative:
        seen: set[str] = set()
        for node_id in extract_node_ids_from_variables(variables) + extract_node_ids_from_query(query):
            if node_id not in seen:
                seen.add(node_id)
                node_ids.append(node_id)

    # Layer 2: resolve and validate ALL discovered node ids FIRST — before honouring any explicit
    # ``owner`` var — so neither a decoy allowed ``owner`` var NOR a decoy sibling node that *does*
    # resolve to an allowed owner can shadow an external/unresolvable node id. Build a per-node
    # resolution map; any node id resolving to an owner outside the allow-list blocks immediately.
    # ``resolve_node_owner`` returns the full ``owner/repo`` (or None). Match it against the
    # allow-list with ``_target_allowed`` so a repo pattern (``aws/bedrock-agentcore-*``) is honoured
    # and a bare-owner entry still matches on the owner half.
    resolve_map: dict[str, str | None] = {}
    resolved_owner: str | None = None
    if node_ids and token:
        for node_id in node_ids:
            resolved = resolve_node_owner(node_id, token)
            resolve_map[node_id] = resolved
            if resolved is None:
                continue
            if not _target_allowed(resolved, allowed_owners):
                return (
                    f"Blocked: node id '{node_id}' belongs to '{resolved}', "
                    f"not in the allowed list ({allowed_str})."
                ), None
            resolved_owner = resolved

    # Layer 3: strict mode — PER-NODE AND semantics. Under a strict mutation EVERY discovered node
    # id must resolve to an (allowed) owner. ANY node id that could not be resolved blocks the call
    # — regardless of any sibling node that DID resolve to an allowed owner, and regardless of any
    # decoy ``owner`` var. Gating on a single ``resolved_owner is None`` would be OR semantics: one
    # resolvable-allowed node would set ``resolved_owner`` non-None and let an unresolvable external
    # node (a repo-scoped type the resolve query has no fragment for — Label ``LA_``/Milestone
    # ``MI_`` — or any transient resolution failure that ``resolve_node_owner`` swallows to None)
    # ride through in the SAME mutation. Tracking unresolved ids per-node closes that whole class.
    # This MUST run BEFORE honouring any explicit ``owner`` var (so a decoy owner can't
    # short-circuit to ALLOW) and applies even when ``variables`` is empty (inline node ids).
    if strict and node_ids:
        unresolved = [nid for nid in node_ids if resolve_map.get(nid) is None]
        if unresolved:
            return (
                f"Blocked: cannot verify the target repository for this mutation. The request "
                f"contains node id(s) ({', '.join(unresolved)}) that could not be resolved to an "
                f"owner. Use explicit owner/name variables instead."
            ), None

    # Layer 1: explicit owner/repo from the variables. Match the most specific target we can build
    # (``owner/repo`` when the repo name is present, else the bare owner). Fail-closed: if the
    # allow-list only permits *specific repos* of this owner (all matching entries are repo globs)
    # and the mutation named only the owner, ``_target_allowed`` returns False on the bare owner and
    # the call is blocked — we won't grant a whole owner we only allow-listed repos of.
    if explicit_target is not None:
        if not _target_allowed(explicit_target, allowed_owners):
            return (
                f"Blocked: target '{explicit_target}' is not in the allowed list ({allowed_str})."
            ), None
        return None, resolved_owner or explicit

    # No node ids and no explicit owner (e.g. schema-level or user-scoped mutation): allow.
    return None, resolved_owner


# ---------------------------------------------------------------------------
# Throttle (writes to external repos) — port of strands-coder activity.py, slimmed
# ---------------------------------------------------------------------------
_THROTTLED_EVENT_TYPES = {
    "IssueCommentEvent", "PullRequestReviewEvent", "PullRequestReviewCommentEvent",
    "IssuesEvent", "PullRequestEvent", "CommitCommentEvent", "CreateEvent",
    "DeleteEvent", "PushEvent",
}
_throttle_cache: dict[str, Any] = {"value": None, "ts": 0.0}
_CACHE_TTL_SECONDS = 60


def _viewer_login(token: str) -> str | None:
    """The authenticated user's login (the account whose events the throttle counts)."""
    env_owner = os.environ.get("GITHUB_REPOSITORY_OWNER")
    if env_owner:
        return env_owner
    try:
        data = _graphql("query { viewer { login } }", {}, token)
        return ((data.get("data") or {}).get("viewer") or {}).get("login")
    except Exception:  # noqa: BLE001
        return None


def count_external_writes(
    *, internal_owners: set[str], token: str, hours: int = 24
) -> int | None:
    """Count this account's write events to external repos in the last ``hours``.

    Returns None when it can't be determined (the caller then fails *open* — never blocks on an
    API hiccup).
    """
    login = _viewer_login(token)
    if not login:
        return None
    try:
        events = _rest_get(f"/users/{login}/events?per_page=100", token)
    except Exception as e:  # noqa: BLE001
        logger.warning("throttle: events fetch failed: %s", e)
        return None
    if not isinstance(events, list):
        return None
    threshold = datetime.now(timezone.utc) - timedelta(hours=hours)
    internal_lower = {o.lower() for o in internal_owners}
    count = 0
    for event in events:
        if event.get("type") not in _THROTTLED_EVENT_TYPES:
            continue
        created = event.get("created_at", "")
        try:
            when = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if when < threshold:
            continue
        repo = event.get("repo", {}).get("name", "")
        owner = repo.split("/")[0].lower() if "/" in repo else ""
        if owner and owner not in internal_lower:
            count += 1
    return count


def enforce_throttle(
    target_owner: str | None,
    *,
    gh: GitHubSettings,
    token: str,
) -> tuple[bool, str]:
    """Gate a write. ``(allowed, message)``; internal targets and API failures never block."""
    if not gh.throttle_enabled:
        return True, "throttle disabled"
    # Internal exemption matches on the OWNER HALF against BARE-OWNER entries only:
    # - ``target_owner`` may now be a full ``owner/repo`` (resolve_node_owner returns
    #   nameWithOwner since repo-scoped allow entries landed), so compare its owner half.
    # - ``internal_owners`` mirrors ``allowed_owners`` and can therefore contain repo-glob /
    #   literal-repo entries (``aws/bedrock-agentcore-*``) — those grant WRITES to specific
    #   external repos and must NOT exempt them from the external-write throttle, so entries
    #   with a ``/`` are excluded from the exemption set.
    internal = {o.lower() for o in gh.internal_owners if "/" not in o}
    if target_owner and target_owner.split("/", 1)[0].lower() in internal:
        return True, "internal target — not throttled"

    now = time.time()
    if _throttle_cache["value"] is not None and (now - _throttle_cache["ts"]) < _CACHE_TTL_SECONDS:
        used = _throttle_cache["value"]
    else:
        used = count_external_writes(internal_owners=internal, token=token)
        _throttle_cache["value"] = used
        _throttle_cache["ts"] = now

    if used is None:
        return True, "throttle check unavailable — allowing (fail-open)"
    if used >= gh.throttle_limit:
        return False, (
            f"Daily external-write throttle reached ({used}/{gh.throttle_limit}). "
            f"This is a safety guardrail, not an error — it resets within 24h. "
            f"Raise the GitHub throttle_limit setting if this is intentional."
        )
    return True, f"{gh.throttle_limit - used} external writes remaining"


def invalidate_throttle_cache() -> None:
    """Reset the throttle cache (used by tests)."""
    _throttle_cache["value"] = None
    _throttle_cache["ts"] = 0.0


# ---------------------------------------------------------------------------
# HTTP seams (stdlib urllib; the only functions tests monkeypatch)
# ---------------------------------------------------------------------------
def _request(method: str, url: str, token: str | None, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    # Authenticate only when a token is present. An empty/None token makes this an *anonymous*
    # request — GitHub's REST v3 serves public issues/PRs that way (GraphQL has no anon tier), which
    # is what lets the context injector enrich public threads without requiring a token.
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", _USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed api.github.com host
        return json.loads(resp.read().decode())


def _graphql(query: str, variables: dict[str, Any], token: str | None) -> dict[str, Any]:
    return _request("POST", GITHUB_GRAPHQL_URL, token, {"query": query, "variables": variables})


def _rest_get(path: str, token: str | None = None) -> Any:
    return _request("GET", f"{GITHUB_REST_URL}{path}", token)


def _get_token(gh: GitHubSettings, use_pat_token: bool) -> str | None:
    """First non-empty token: the config-resolved token, else the env var names (PAT first if asked).

    ``gh.token`` is resolved from the loaded ``Config`` (Secrets Manager / .env merged under the
    env), so it works on the deployed runtime where the token lives only in the secret and never
    reaches ``os.environ``. The ``os.environ`` scan remains the fallback for local / GitHub-Actions
    runs, and honours ``use_pat_token`` (PAT-first) which the single config token can't express.
    """
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


def _result(status: str, text: str) -> dict[str, Any]:
    return {"status": status, "content": [{"text": text}]}


# ---------------------------------------------------------------------------
# The tool factory
# ---------------------------------------------------------------------------
def make_use_github(gh: GitHubSettings) -> Any:
    """Return a ``use_github`` tool bound to the harness's GitHub guardrail settings.

    Built and injected by ``build_agent`` (settings-aware, like ``spawn``), not a plain builtin.
    """
    from strands import tool

    allowed_owners = set(gh.allowed_owners)

    @tool(name="use_github")
    def use_github(
        query_type: str,
        query: str,
        label: str,
        variables: dict[str, Any] | None = None,
        use_pat_token: bool = False,
    ) -> dict[str, Any]:
        """Execute a GitHub GraphQL query or mutation against the GitHub v4 API.

        Universal access to GitHub's GraphQL API: repository/issue/PR/project data, and (with a
        write-scoped token) mutations such as creating issues, commenting, or merging PRs.

        Repository-scope guardrails (the harness's GitHub settings) apply *in addition* to the
        harness's normal tool gates: an optional owner allow-list, node-id → owner resolution for
        mutations, strict blocking of unverifiable mutations, and an optional daily external-write
        throttle. A blocked call returns ``status="error"`` with the reason — do not try to work
        around a guardrail.

        Args:
            query_type: "query" or "mutation".
            query: The GraphQL query/mutation string.
            label: Short human-readable description of the operation (for logs/approval).
            variables: Optional variables for the query.
            use_pat_token: Prefer the PAT_TOKEN env var over GITHUB_TOKEN (use sparingly — a PAT
                can re-trigger workflows).

        Returns:
            ``{"status": "success"|"error", "content": [{"text": <formatted result or error>}]}``.
        """
        vars_ = variables or {}
        is_mutative = is_mutation_query(query, query_type)
        token = _get_token(gh, use_pat_token)
        if not token:
            return _result(
                "error",
                f"No GitHub token found. Set one of: {', '.join(gh.token_env)}.",
            )

        # Guardrail 1–3: owner allow-list + node-id resolution + strict mode.
        owner_error, resolved_owner = validate_owner(
            vars_,
            allowed_owners=allowed_owners,
            is_mutative=is_mutative,
            strict=gh.strict_mutations,
            token=token,
            query=query,
        )
        if owner_error:
            return _result("error", f"🛑 {owner_error}")

        # Guardrail 4: daily external-write throttle (mutations only).
        if is_mutative:
            target = resolved_owner or extract_owner_from_variables(vars_)
            allowed, msg = enforce_throttle(target, gh=gh, token=token)
            if not allowed:
                return _result("error", f"🛑 SAFETY GUARDRAIL — {msg}")

        # Execute.
        try:
            response = _graphql(query, vars_, token)
        except urllib.error.HTTPError as e:
            detail = {
                401: "Authentication failed — check the GitHub token.",
                403: "Forbidden — the token lacks permissions (or you hit a rate limit).",
            }.get(e.code, f"HTTP {e.code}: {e.reason}")
            return _result("error", detail)
        except urllib.error.URLError as e:
            return _result("error", f"Request error: {e.reason}")
        except Exception as e:  # noqa: BLE001
            return _result("error", f"GitHub call failed: {e}")

        if "errors" in response:
            return _result(
                "error",
                "GraphQL errors:\n" + json.dumps(response["errors"], indent=2),
            )
        return _result("success", json.dumps(response.get("data", {}), indent=2))

    return use_github
