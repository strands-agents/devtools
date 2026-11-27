"""GitHub target authorization, mutation classification, REST paths, and write throttling."""

from __future__ import annotations

import base64
import fnmatch
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from .github_client import count_external_writes, resolve_node_owner
from .github_graphql import GraphQLDocument, GraphQLDocumentError, parse_document

if TYPE_CHECKING:
    from strandly.core.config import GitHubSettings

logger = logging.getLogger("strandly.tools.github")


def is_mutation_query(query: str, query_type: str = "") -> bool:
    """True when the parsed document contains a ``mutation`` operation or the caller labels it one.

    The ``query_type`` label can raise the classification but never lower it, so a mutation behind
    a leading fragment, comment, BOM, or comma is still a mutation. A document that does not parse
    counts as a mutation, so the write checks run and refuse it.
    """
    if query_type.strip().lower() == "mutation":
        return True
    try:
        return parse_document(query).has_mutation
    except GraphQLDocumentError:
        return True


NODE_ID_PATTERN = re.compile(r"^[A-Z][A-Za-z]*_[a-zA-Z0-9+/=_\-]{4,}$")


_NODE_ID_LITERAL_PATTERN = re.compile(r"[A-Z][A-Za-z]*_[a-zA-Z0-9+/=_\-]{4,}")


# Repo-scoped types that ``github_client._RESOLVE_QUERY`` resolves. Ids with other prefixes are
# still collected; a known prefix only exempts an all-caps id from the enum filter.
REPO_SCOPED_PREFIXES = {
    "PR_", "I_", "IC_", "R_", "RE_", "RC_", "CC_", "D_", "DI_", "DC_", "LA_", "MI_",
    "PRR_", "PRRC_", "PRRT_", "REF_", "DIC_",
}


# Users, bots and teams name who a write involves (assignees, reviewers), never where it lands, so
# they are not repository targets. Safe to skip: every strict root field still needs its own
# target argument to resolve to an allowed repository, and an actor id there resolves to none.
_ACTOR_PREFIXES = ("U_", "BOT_", "T_")
_ACTOR_LEGACY_TYPES = frozenset({"User", "Bot", "Team"})


_LEGACY_DECODED_PATTERN = re.compile(r"^\d+:[A-Za-z][A-Za-z0-9]*\d+$")


_BASE64_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9+/]{8,}={0,2}")


_BASE64_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)


_ENUM_LITERAL_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _looks_like_legacy_node_id(value: str) -> bool:
    """Recognize base64-encoded legacy GitHub node ids by their decoded structure."""
    if not value or len(value) < 8 or len(value) % 4 != 0:
        return False
    if any(c not in _BASE64_ALPHABET for c in value):
        return False
    try:
        decoded = base64.b64decode(value, validate=True).decode("ascii")
    except Exception:  # noqa: BLE001 — any decode/charset failure => not a legacy id
        return False
    return bool(_LEGACY_DECODED_PATTERN.match(decoded))


def _looks_like_node_id(value: str) -> bool:
    """Recognize a node id: legacy, or ``TYPE_payload`` that is not an all-caps enum literal."""
    if _looks_like_legacy_node_id(value):
        return True
    if not NODE_ID_PATTERN.match(value):
        return False
    # Known repo prefixes are recognized BEFORE the enum filter so an all-uppercase repo-scoped id
    # is never hidden from authorization. Prefix-shaped enums (PR_REVIEW, I_LIKE_IT) remain
    # conservative false positives: strict mode blocks them rather than skipping a real id.
    if any(value.startswith(p) for p in REPO_SCOPED_PREFIXES):
        return True
    return not _ENUM_LITERAL_PATTERN.match(value)


def _is_actor_id(value: str) -> bool:
    """True for a user, bot or team node id, modern or legacy."""
    if value.startswith(_ACTOR_PREFIXES):
        return True
    if not _looks_like_legacy_node_id(value):
        return False
    decoded = base64.b64decode(value).decode("ascii")
    return re.sub(r"\d+$", "", decoded.split(":", 1)[1]) in _ACTOR_LEGACY_TYPES


_OWNER_KEYS = ("owner", "repositoryOwner", "organizationLogin", "login")


_NAME_KEYS = ("name", "repositoryName", "repo", "repository")


_FULL_NAME_KEYS = ("repositoryNameWithOwner", "nameWithOwner", "repository", "repositoryName")


def extract_owner_from_variables(variables: dict[str, Any]) -> str | None:
    """Return the first explicit owner from common variable shapes."""
    for key in _OWNER_KEYS:
        value = variables.get(key)
        if isinstance(value, str) and value:
            return value
    repo = variables.get("repository")
    if isinstance(repo, str) and "/" in repo:
        return repo.split("/")[0]
    return None


def extract_target_from_variables(variables: dict[str, Any]) -> str | None:
    """Return owner/repo when both are explicit, otherwise the bare owner."""
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


_URL_KEY_PATTERN = re.compile(r"(?:^url|Url|URL)$")


_GITHUB_REPO_URL_PATTERN = re.compile(
    r"^https?://(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9_-]*)/([A-Za-z0-9._-]+?)(?:\.git)?(?:[/?#]|$)",
    re.IGNORECASE,
)


def extract_repository_targets(value: Any) -> list[str]:
    """Collect every repository or owner named at any depth of a variables or arguments value.

    Recognizes ``owner/name`` strings under ``repositoryNameWithOwner``, ``nameWithOwner``,
    ``repository`` and ``repositoryName``, owner keys paired with a sibling name key, and GitHub
    URLs under ``...Url`` keys (``addSubIssue.subIssueUrl``). A GitHub URL that does not name a
    repository is kept as-is, so it fails the allow-list.
    """
    found: list[str] = []

    def _scan(node: Any) -> None:
        if isinstance(node, dict):
            for key in _FULL_NAME_KEYS:
                full = node.get(key)
                if isinstance(full, str) and "/" in full:
                    found.append(full)
            for key, url in node.items():
                if isinstance(key, str) and _URL_KEY_PATTERN.search(key) and isinstance(url, str):
                    if m := _GITHUB_REPO_URL_PATTERN.match(url.strip()):
                        found.append(f"{m.group(1)}/{m.group(2)}")
                    elif "github" in url.lower():
                        found.append(url)
            owner = next(
                (node[k] for k in _OWNER_KEYS if isinstance(node.get(k), str) and node[k]), None
            )
            if owner is not None:
                name = next(
                    (
                        node[k] for k in _NAME_KEYS
                        if isinstance(node.get(k), str) and node[k] and "/" not in node[k]
                    ),
                    None,
                )
                found.append(f"{owner}/{name}" if name else owner)
            for child in node.values():
                _scan(child)
        elif isinstance(node, list):
            for item in node:
                _scan(item)

    _scan(value)
    return list(dict.fromkeys(found))


def extract_node_ids_from_variables(variables: dict[str, Any]) -> list[str]:
    """Collect node ids recursively from variables, including unknown opaque ids."""
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


# Input keys GitHub types as ``ID`` (``id``, ``ids``, ``...Id``, ``...Ids``, and ``inReplyTo``).
# Every non-empty string under one is a candidate target, whatever its shape.
_ID_KEY_PATTERN = re.compile(r"^(?:ids?|[a-z][A-Za-z0-9]*Ids?|inReplyTo)$")
# A caller-chosen echo string, never an object reference.
_NON_REFERENCE_ID_KEYS = frozenset({"clientMutationId"})
# Outside ``ID`` keys (bodies, titles, headlines, branch names, paths), a string counts as a node id
# only in GitHub's own shape: an all-caps type prefix and a mixed-case base64url payload. That keeps
# ``Update_README`` or ``Needs_Review`` out, and legacy ids are recognized by their decoded form.
_FREE_TEXT_NODE_ID_PATTERN = re.compile(
    r"^[A-Z]{1,12}_(?=[A-Za-z0-9_\-]*[a-z])(?=[A-Za-z0-9_\-]*[A-Z0-9])[A-Za-z0-9_\-]{8,}$"
)


def _is_id_key(key: str | None) -> bool:
    return key is not None and key not in _NON_REFERENCE_ID_KEYS and bool(_ID_KEY_PATTERN.match(key))


def collect_node_ids(value: Any) -> list[str]:
    """Collect node-id candidates from variables or arguments, keyed by where each string sits.

    Every string under an ``ID``-typed key counts. Elsewhere only GitHub-shaped ids count, so
    free text does not turn into an unresolvable target that strict mode refuses. List items
    inherit their key (``labelIds: [...]``).
    """
    found: list[str] = []

    def _scan(node: Any, key: str | None) -> None:
        if isinstance(node, str):
            if _is_id_key(key):
                if node.strip():
                    found.append(node)
            elif _looks_like_legacy_node_id(node) or _FREE_TEXT_NODE_ID_PATTERN.match(node):
                found.append(node)
        elif isinstance(node, dict):
            for k, v in node.items():
                _scan(v, k if isinstance(k, str) else None)
        elif isinstance(node, list):
            for item in node:
                _scan(item, key)

    _scan(value, None)
    return list(dict.fromkeys(found))


def extract_node_ids_from_query(query: str) -> list[str]:
    """Collect every inline string literal in the document that looks like a node id.

    Enum literals are syntax, not strings, so they never count. A document that does not parse
    falls back to scanning the raw text, which over-collects rather than misses.
    """
    if not query:
        return []
    try:
        candidates: Any = parse_document(query).string_literals
    except GraphQLDocumentError:
        candidates = [m.group(0) for m in _NODE_ID_LITERAL_PATTERN.finditer(query)]
        candidates += [m.group(0) for m in _BASE64_TOKEN_PATTERN.finditer(query)]
    found = [value for value in candidates if _looks_like_node_id(value)]
    return list(dict.fromkeys(found))


# Every mutation ``use_github`` may send under strict mode, mapped to the input paths that name
# its target. At least one path must be present, and each present value must be a node id that
# resolves to an allowed repository, or (``...NameWithOwner``) an allowed ``owner/repo``.
MUTATION_TARGET_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "addComment": ("input.subjectId",),
    "updateIssueComment": ("input.id",),
    "deleteIssueComment": ("input.id",),
    "minimizeComment": ("input.subjectId",),
    "unminimizeComment": ("input.subjectId",),
    "addReaction": ("input.subjectId",),
    "removeReaction": ("input.subjectId",),
    "addUpvote": ("input.subjectId",),
    "removeUpvote": ("input.subjectId",),
    "createIssue": ("input.repositoryId",),
    "updateIssue": ("input.id",),
    "closeIssue": ("input.issueId",),
    "reopenIssue": ("input.issueId",),
    "pinIssue": ("input.issueId",),
    "unpinIssue": ("input.issueId",),
    "transferIssue": ("input.issueId", "input.repositoryId"),
    "addSubIssue": ("input.issueId",),
    "removeSubIssue": ("input.issueId",),
    "createLinkedBranch": ("input.issueId", "input.repositoryId"),
    "lockLockable": ("input.lockableId",),
    "unlockLockable": ("input.lockableId",),
    "addLabelsToLabelable": ("input.labelableId",),
    "removeLabelsFromLabelable": ("input.labelableId",),
    "clearLabelsFromLabelable": ("input.labelableId",),
    "createLabel": ("input.repositoryId",),
    "updateLabel": ("input.id",),
    "deleteLabel": ("input.id",),
    "addAssigneesToAssignable": ("input.assignableId",),
    "removeAssigneesFromAssignable": ("input.assignableId",),
    "createPullRequest": ("input.repositoryId",),
    "updatePullRequest": ("input.pullRequestId",),
    "closePullRequest": ("input.pullRequestId",),
    "reopenPullRequest": ("input.pullRequestId",),
    "mergePullRequest": ("input.pullRequestId",),
    "enablePullRequestAutoMerge": ("input.pullRequestId",),
    "disablePullRequestAutoMerge": ("input.pullRequestId",),
    "markPullRequestReadyForReview": ("input.pullRequestId",),
    "convertPullRequestToDraft": ("input.pullRequestId",),
    "updatePullRequestBranch": ("input.pullRequestId",),
    "requestReviews": ("input.pullRequestId",),
    "addPullRequestReview": ("input.pullRequestId",),
    "submitPullRequestReview": ("input.pullRequestReviewId", "input.pullRequestId"),
    "updatePullRequestReview": ("input.pullRequestReviewId",),
    "deletePullRequestReview": ("input.pullRequestReviewId",),
    "dismissPullRequestReview": ("input.pullRequestReviewId",),
    "addPullRequestReviewComment": ("input.pullRequestId", "input.pullRequestReviewId"),
    "addPullRequestReviewThread": ("input.pullRequestId", "input.pullRequestReviewId"),
    "addPullRequestReviewThreadReply": (
        "input.pullRequestReviewThreadId", "input.pullRequestReviewId",
    ),
    "updatePullRequestReviewComment": ("input.pullRequestReviewCommentId",),
    "deletePullRequestReviewComment": ("input.id",),
    "resolveReviewThread": ("input.threadId",),
    "unresolveReviewThread": ("input.threadId",),
    "createCommitOnBranch": ("input.branch.repositoryNameWithOwner", "input.branch.id"),
    "createRef": ("input.repositoryId",),
    "updateRef": ("input.refId",),
    "deleteRef": ("input.refId",),
    "createDiscussion": ("input.repositoryId",),
    "updateDiscussion": ("input.discussionId",),
    "deleteDiscussion": ("input.id",),
    "closeDiscussion": ("input.discussionId",),
    "reopenDiscussion": ("input.discussionId",),
    "addDiscussionComment": ("input.discussionId",),
    "updateDiscussionComment": ("input.commentId",),
    "deleteDiscussionComment": ("input.id",),
    "markDiscussionCommentAsAnswer": ("input.id",),
    "unmarkDiscussionCommentAsAnswer": ("input.id",),
}


def _argument_at(arguments: dict[str, Any], path: str) -> Any:
    cur: Any = arguments
    for key in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


_TARGET_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:/[A-Za-z0-9._-]+)?$")


def _target_allowed(target: str, allowed_owners: set[str]) -> bool:
    """Match a case-insensitive bare owner or owner/repo against the allow-list.

    Anything else (a second ``/``, whitespace) is refused: a glob's ``*`` also matches ``/``.
    """
    if not _TARGET_PATTERN.match(target):
        return False
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


def _check_mutation_fields(
    document: GraphQLDocument,
    *,
    resolve_map: dict[str, str | None],
    allowed_owners: set[str],
    token: str | None,
) -> str | None:
    """Return an error unless every root mutation field is allow-listed and names a verified target."""
    allowed_str = ", ".join(sorted(allowed_owners))
    for field in document.mutation_fields:
        if field.name == "__typename":
            continue
        paths = MUTATION_TARGET_ARGUMENTS.get(field.name)
        if paths is None:
            logger.warning("blocked mutation: field %r is not on the mutation allow-list", field.name)
            return (
                f"Blocked: mutation '{field.name}' is not on the use_github mutation allow-list, "
                f"so its target repository cannot be verified."
            )
        values = [(path, _argument_at(field.arguments, path)) for path in paths]
        odd = [path for path, value in values if value is not None and not isinstance(value, str)]
        if odd:
            # GitHub coerces an Int to ID, so a number here is a target this check cannot verify.
            logger.warning("blocked mutation: %r has a non-string target (%s)", field.name, odd)
            return (
                f"Blocked: '{field.name}' needs {', '.join(odd)} as a node id string, so its "
                f"target repository can be verified."
            )
        present = [(path, value) for path, value in values if isinstance(value, str) and value]
        if not present:
            logger.warning("blocked mutation: %r names no target (%s)", field.name, ", ".join(paths))
            return (
                f"Blocked: cannot verify the target repository for '{field.name}'. Set "
                f"{' or '.join(paths)} to an object in an allowed repository ({allowed_str})."
            )
        for path, value in present:
            if path.endswith("NameWithOwner"):
                owner: str | None = value if "/" in value else None
            else:
                if value not in resolve_map:
                    resolve_map[value] = resolve_node_owner(value, token) if token else None
                owner = resolve_map[value]
            if owner is None or not _target_allowed(owner, allowed_owners):
                logger.warning(
                    "blocked mutation: %s.%s=%r resolves to %r, not in the allowed list (%s)",
                    field.name, path, value, owner, allowed_str,
                )
                return (
                    f"Blocked: cannot verify that '{field.name}' targets an allowed repository: "
                    f"{path} '{value}' resolves to '{owner or 'no repository'}' "
                    f"(allowed: {allowed_str})."
                )
    return None


def validate_owner(
    variables: dict[str, Any],
    *,
    allowed_owners: set[str],
    is_mutative: bool,
    strict: bool,
    token: str | None,
    query: str = "",
) -> tuple[str | None, str | None]:
    """Return (error, target), checking every explicit or node-id target on mutations.

    The parsed document decides whether the call is a mutation; ``is_mutative`` can only raise it.
    Under ``strict``, a mutation passes only when every root field is in
    :data:`MUTATION_TARGET_ARGUMENTS` and names a target in an allowed repository.
    """
    explicit = extract_owner_from_variables(variables)
    explicit_target = extract_target_from_variables(variables)

    document: GraphQLDocument | None = None
    parse_error: str | None = None
    if query or is_mutative:
        try:
            document = parse_document(query, variables)
        except GraphQLDocumentError as e:
            parse_error = str(e)
        is_mutative = is_mutative or document is None or document.has_mutation

    if not allowed_owners:
        return ("Blocked: no repository access policy configured." if is_mutative else None), explicit

    allowed_str = ", ".join(sorted(allowed_owners))

    if not is_mutative:
        # Reads are owner-checked through the explicit top-level variables only.
        if explicit_target is not None and not _target_allowed(explicit_target, allowed_owners):
            # Escape the untrusted explicit target with %r so newlines cannot forge log entries.
            logger.warning(
                "blocked call: target %r is not in the allowed list (%s)", explicit_target, allowed_str
            )
            return (
                f"Blocked: target '{explicit_target}' is not in the allowed list ({allowed_str})."
            ), None
        return None, explicit

    # Collect every node-id target the mutation names, from the variables AND every inline argument.
    # Order-preserving de-dup so an id repeated across both is resolved once.
    # Literals are read from the parsed arguments (variables substituted), so each string keeps the
    # argument or input key it sits under. An unparseable document is refused below either way.
    if document is not None:
        candidates = collect_node_ids(variables) + [
            nid for args in document.argument_values for nid in collect_node_ids(args)
        ]
    else:
        candidates = extract_node_ids_from_variables(variables)
    node_ids = [nid for nid in dict.fromkeys(candidates) if not _is_actor_id(nid)]

    # Resolve every node-id target before considering explicit owner variables.
    # Neither an allowed owner nor an allowed node may mask a blocked target.
    resolve_map: dict[str, str | None] = {}
    if node_ids and token:
        for node_id in node_ids:
            resolved = resolve_node_owner(node_id, token)
            resolve_map[node_id] = resolved
            if resolved is None:
                continue
            if not _target_allowed(resolved, allowed_owners):
                # Log every refused target, including successfully resolved external ones.
                logger.warning(
                    "blocked mutation: node id %s belongs to '%s', not in the allowed list (%s)",
                    node_id,
                    resolved,
                    allowed_str,
                )
                return (
                    f"Blocked: node id '{node_id}' belongs to '{resolved}', "
                    f"not in the allowed list ({allowed_str})."
                ), None

    # Strict mutations require EVERY discovered node to resolve. One allowed node
    # cannot authorize a second inaccessible or unsupported node.
    if strict and node_ids:
        unresolved = [nid for nid in node_ids if resolve_map.get(nid) is None]
        if unresolved:
            # Log the unresolved targets responsible for a strict-mode refusal.
            logger.warning(
                "blocked mutation under strict mode: %d of %d node id(s) could not be resolved "
                "to an owner (%s)",
                len(unresolved),
                len(node_ids),
                ", ".join(unresolved),
            )
            return (
                f"Blocked: cannot verify the target repository for this mutation. The request "
                f"contains node id(s) ({', '.join(unresolved)}) that could not be resolved to an "
                f"owner. Use ids of objects in an allowed repository ({allowed_str})."
            ), None

    # Every repository or owner named anywhere in the variables or inline arguments must be
    # allowed. A repo-only allow entry never grants its whole owner when the name is omitted.
    named_targets = list(
        dict.fromkeys(
            ([explicit_target] if explicit_target else [])
            + extract_repository_targets(variables)
            + [
                t
                for args in (document.argument_values if document else ())
                for t in extract_repository_targets(args)
            ]
        )
    )
    for target in named_targets:
        if not _target_allowed(target, allowed_owners):
            # Escape the untrusted explicit target with %r so newlines cannot forge log entries.
            logger.warning(
                "blocked call: target %r is not in the allowed list (%s)", target, allowed_str
            )
            return f"Blocked: target '{target}' is not in the allowed list ({allowed_str}).", None

    # The checks above use the variables alone when the document does not parse; the specific
    # refusal wins, and an unparseable write is refused either way.
    if document is None:
        logger.warning("blocked mutation: GraphQL document is not valid: %s", parse_error)
        return f"Blocked: the GraphQL document is not valid ({parse_error}).", None

    # Fail closed: "no target found" is never "no target exists". A strict mutation passes only
    # through the field allow-list, which requires a verified target for every root field.
    if strict and document.has_mutation:
        field_error = _check_mutation_fields(
            document, resolve_map=resolve_map, allowed_owners=allowed_owners, token=token
        )
        if field_error:
            return field_error, None

    # Report a target the throttle counts (one allowed only through a repo glob) ahead of a bare
    # allowed owner, so an exempt target in one root field cannot hide a counted one in another.
    verified = list(
        dict.fromkeys([o for o in resolve_map.values() if o] + named_targets)
    ) or ([explicit] if explicit else [])
    bare = {o.lower() for o in allowed_owners if "/" not in o}
    counted = [t for t in verified if t.split("/", 1)[0].lower() not in bare]
    reported = counted or verified
    return None, reported[0] if reported else None


_throttle_cache: dict[str, Any] = {"value": None, "ts": 0.0}


_CACHE_TTL_SECONDS = 60


def enforce_throttle(
    target_owner: str | None,
    *,
    gh: GitHubSettings,
    token: str,
) -> tuple[bool, str]:
    """Gate a write. ``(allowed, message)``; internal targets and API failures never block."""
    if not gh.throttle_enabled:
        return True, "throttle disabled"
    # Only bare internal owners grant a throttle exemption. Repo-glob allow entries
    # authorize specific external repositories without exempting them from the quota.
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
            f"Raise STRANDLY_GITHUB_THROTTLE_LIMIT if this is intentional."
        )
    return True, f"{gh.throttle_limit - used} external writes remaining"


def invalidate_throttle_cache() -> None:
    """Reset the throttle cache (used by tests)."""
    _throttle_cache["value"] = None
    _throttle_cache["ts"] = 0.0


_REST_REPO_FAMILIES = frozenset(
    {
        "pulls", "compare", "contents", "git", "actions", "commits", "issues", "releases",
        "branches", "labels", "milestones", "collaborators", "invitations", "check-runs",
        "check-suites", "languages", "topics", "tags", "readme",
    }
)


_REST_ACCOUNT_FAMILIES = frozenset(
    {"user", "users", "gists", "search", "advisories", "orgs", "notifications"}
)


_REST_ACCOUNT_WRITABLE = frozenset({"user", "gists"})


def parse_rest_path(path: str) -> tuple[str | None, str]:
    """Split a REST path into ``(target, family)``; raise ``ValueError`` if it isn't admissible.

    ``target`` is ``"owner/repo"`` for a repo-scoped path (so it can be run through the owner
    allow-list) or ``None`` for an account-scoped one. ``family`` is the resource family used for the
    allow-list check — the segment after ``owner/repo``, or the first segment otherwise.

    Rejects anything that isn't a plain absolute API path: a scheme/host (so the fixed
    ``api.github.com`` base can't be swapped), ``..`` traversal, empty segments, and backslashes.
    """
    raw = (path or "").strip()
    if not raw.startswith("/"):
        raise ValueError("path must start with '/' (e.g. '/repos/owner/repo/pulls/1/files')")
    if "://" in raw or "\\" in raw or "@" in raw.split("?", 1)[0]:
        raise ValueError("path must be a bare API path, not a URL")

    bare = raw.split("?", 1)[0].split("#", 1)[0]
    segments = [s for s in bare.split("/") if s]
    if not segments:
        raise ValueError("path has no segments")
    if any(s == ".." or s == "." for s in segments):
        raise ValueError("path must not contain '.' or '..' segments")

    if segments[0] == "repos":
        if len(segments) < 3:
            raise ValueError("a /repos path needs both an owner and a repo")
        owner, repo = segments[1], segments[2]
        # ``/repos/{owner}/{repo}`` itself (repo metadata) has no family segment.
        family = segments[3] if len(segments) > 3 else "readme"
        if family not in _REST_REPO_FAMILIES:
            allowed = ", ".join(sorted(_REST_REPO_FAMILIES))
            raise ValueError(f"resource '{family}' is not in the allowed set ({allowed})")
        return f"{owner}/{repo}", family

    family = segments[0]
    if family not in _REST_ACCOUNT_FAMILIES:
        allowed = ", ".join(sorted(_REST_REPO_FAMILIES | _REST_ACCOUNT_FAMILIES))
        raise ValueError(f"resource '{family}' is not in the allowed set ({allowed})")
    return None, family


def validate_rest_path(
    path: str,
    method: str,
    *,
    allowed_owners: set[str],
) -> tuple[str | None, str | None]:
    """Guardrail a REST call. Returns ``(error, target)`` — ``error`` non-None means blocked.

    Mirrors :func:`validate_owner`'s contract for the GraphQL path, adapted to REST's addressing:
    the target owner comes from the *path* (``/repos/{owner}/{repo}/…``) rather than from GraphQL
    node ids, so there is nothing to resolve over the network and no strict-mode ambiguity — the
    owner is either right there in the path or the path is account-scoped.

    An account-scoped path can't be owner-checked, so a non-GET is confined to the families that can
    only ever act on the token's own account (see :data:`_REST_ACCOUNT_WRITABLE`).
    """
    verb = (method or "GET").upper()
    if verb != "GET" and not allowed_owners:
        return "Blocked: no repository access policy configured.", None
    try:
        target, family = parse_rest_path(path)
    except ValueError as e:
        return f"Blocked: {e}", None

    if verb not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
        return f"Blocked: method '{verb}' is not supported.", None

    if target is None:
        if verb != "GET" and family not in _REST_ACCOUNT_WRITABLE:
            return (
                f"Blocked: {verb} on '/{family}' has no repository owner to verify. Only "
                f"{', '.join(sorted(_REST_ACCOUNT_WRITABLE))} accept writes without one."
            ), None
        # No owner to validate — allowed per the family rules above.
        return None, None

    if allowed_owners and not _target_allowed(target, allowed_owners):
        allowed_str = ", ".join(sorted(allowed_owners))
        return (
            f"Blocked: target '{target}' is not in the allowed list ({allowed_str})."
        ), None
    return None, target
