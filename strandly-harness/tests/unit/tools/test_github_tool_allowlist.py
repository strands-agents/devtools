"""Owner write allow-list enforcement for the ``use_github`` tool, wired from ``Config.github``.

These are hermetic: the only network seam is ``tools.github._graphql`` (monkeypatched), exactly
as the rest of the suite stays network-free. They assert that flipping the allow-list ON via
``Config.github`` (PR 2) actually blocks writes outside the Strands orgs while letting Strands
writes through, and that ``strict_mutations`` blocks an unverifiable mutation target.
"""

from __future__ import annotations

from typing import Any

import pytest

from strandly_harness.core.config import Config
from strandly_harness.tools import github as gh_tool
from strandly_harness.tools.github import make_use_github, validate_owner


@pytest.fixture
def gh_settings():
    """Default (env-free) Config → the hardcoded Strands-orgs allow-list."""
    return Config(values={}).github


def test_use_github_blocks_non_strands_owner(monkeypatch, gh_settings):
    monkeypatch.setenv("STRANDLY_GITHUB_TOKEN", "t")

    def _boom(*a, **k):  # the call must be blocked *before* any network I/O
        raise AssertionError("network must not be reached for a blocked owner")

    monkeypatch.setattr(gh_tool, "_graphql", _boom)

    use_github = make_use_github(gh_settings)
    res = use_github(
        query_type="mutation",
        query="mutation($id: ID!) { addComment(input: {subjectId: $id}) { clientMutationId } }",
        label="comment",
        variables={"owner": "agent-of-mkmeral", "name": "strands-coder-private"},
    )
    assert res["status"] == "error"
    assert "not in the allowed list" in res["content"][0]["text"]


def test_use_github_allows_strands_owner(monkeypatch, gh_settings):
    monkeypatch.setenv("STRANDLY_GITHUB_TOKEN", "t")
    calls: list[dict[str, Any]] = []

    def _fake_graphql(query, variables, token):
        calls.append({"query": query, "variables": variables, "token": token})
        return {"data": {"createIssue": {"issue": {"number": 1}}}}

    monkeypatch.setattr(gh_tool, "_graphql", _fake_graphql)

    use_github = make_use_github(gh_settings)
    res = use_github(
        query_type="mutation",
        query="mutation { createIssue { issue { number } } }",
        label="create issue",
        variables={"owner": "strands-agents", "name": "sdk-python"},
    )
    assert res["status"] == "success"
    assert len(calls) == 1  # the real mutation executed once


def test_validate_owner_node_id_strict_block(monkeypatch, gh_settings):
    # A mutation carrying only an opaque node id that cannot be resolved is blocked under strict
    # mode (the default) rather than silently allowed.
    monkeypatch.setattr(gh_tool, "resolve_node_owner", lambda node_id, token: None)
    err, resolved = validate_owner(
        {"subjectId": "PR_kwDOABCDEF1234567890"},
        allowed_owners=set(gh_settings.allowed_owners),
        is_mutative=True,
        strict=gh_settings.strict_mutations,
        token="t",
    )
    assert err is not None and "cannot verify" in err
    assert resolved is None


def test_validate_owner_node_id_resolves_to_blocked_owner(monkeypatch, gh_settings):
    monkeypatch.setattr(gh_tool, "resolve_node_owner", lambda node_id, token: "evilcorp")
    err, _ = validate_owner(
        {"subjectId": "PR_kwDOABCDEF1234567890"},
        allowed_owners=set(gh_settings.allowed_owners),
        is_mutative=True,
        strict=gh_settings.strict_mutations,
        token="t",
    )
    assert err is not None and "evilcorp" in err


def test_validate_owner_node_id_resolves_to_allowed_owner(monkeypatch, gh_settings):
    monkeypatch.setattr(gh_tool, "resolve_node_owner", lambda node_id, token: "strands-labs")
    err, resolved = validate_owner(
        {"subjectId": "PR_kwDOABCDEF1234567890"},
        allowed_owners=set(gh_settings.allowed_owners),
        is_mutative=True,
        strict=gh_settings.strict_mutations,
        token="t",
    )
    assert err is None
    assert resolved == "strands-labs"


# ---------------------------------------------------------------------------
# BARE-CR comment classifier bypass (round-5 remediation, PR #35 follow-up).
# A GraphQL comment ends at a bare carriage return ``\r`` (U+000D), so
# ``# x\rmutation{...}`` executes the trailing mutation on GitHub. The tool must
# classify it as a mutation and route its inline external node id through the
# owner allow-list (BLOCK), while a genuine bare-CR-prefixed read is untouched.
# ---------------------------------------------------------------------------
_BARE_CR_PREFIXES = ["# x\r", "# x\r\n", "\ufeff,# c\r "]


@pytest.mark.parametrize("prefix", _BARE_CR_PREFIXES)
def test_use_github_blocks_bare_cr_inline_external_node_poc(monkeypatch, gh_settings, prefix):
    """The round-5 PoC: bare-CR comment + inline external node id + empty vars,
    mislabelled ``query_type='query'`` — must be BLOCKED before any network I/O
    (allow-list ON = Strands orgs, strict ON = the default)."""
    monkeypatch.setenv("STRANDLY_GITHUB_TOKEN", "t")
    # resolve_node_owner: GitHub-realistic — the attacker node resolves to an external owner.
    monkeypatch.setattr(gh_tool, "resolve_node_owner", lambda node_id, token: "attacker-org")

    def _boom(*a, **k):
        raise AssertionError("network must NOT be reached — the mutation should be blocked")

    monkeypatch.setattr(gh_tool, "_graphql", _boom)

    use_github = make_use_github(gh_settings)
    q = prefix + 'mutation{ addComment(input:{subjectId:"PR_kwDOEXTattacker0123"}){clientMutationId} }'
    res = use_github(query_type="query", query=q, variables={}, label="poc")
    assert res["status"] == "error"
    assert "attacker-org" in res["content"][0]["text"]


def test_use_github_bare_cr_targetless_read_not_overblocked(monkeypatch, gh_settings):
    """A benign bare-CR-prefixed READ (no external node) is NOT over-blocked: it executes."""
    monkeypatch.setenv("STRANDLY_GITHUB_TOKEN", "t")
    monkeypatch.setattr(gh_tool, "resolve_node_owner", lambda node_id, token: "attacker-org")
    calls: list[dict[str, Any]] = []

    def _fake_graphql(query, variables, token):
        calls.append({"query": query})
        return {"data": {"viewer": {"login": "me"}}}

    monkeypatch.setattr(gh_tool, "_graphql", _fake_graphql)

    use_github = make_use_github(gh_settings)
    q = "# mutation cool\rquery{ viewer { login } }"
    res = use_github(query_type="query", query=q, variables={}, label="benign-read")
    assert res["status"] == "success"
    assert len(calls) == 1  # the read executed, not over-blocked


# ---------------------------------------------------------------------------
# Repo-glob allow entries (owner/repo patterns) — let specific external repos through
# (e.g. the AgentCore packages) WITHOUT opening a whole org, while bare-owner entries still
# grant the whole org. Matcher: strandly_harness.tools.github._target_allowed.
# ---------------------------------------------------------------------------
from strandly_harness.tools.github import _target_allowed  # noqa: E402

_MIXED = {"strands-agents", "strands-labs", "aws/bedrock-agentcore-*", "aws/agentcore-cli"}


def test_target_allowed_bare_owner_grants_whole_org():
    assert _target_allowed("strands-agents", _MIXED)
    assert _target_allowed("strands-agents/sdk-python", _MIXED)  # any repo under a bare owner
    assert _target_allowed("STRANDS-AGENTS/Foo", _MIXED)  # case-insensitive


def test_target_allowed_repo_glob_matches_only_specific_repos():
    assert _target_allowed("aws/bedrock-agentcore-sdk-python", _MIXED)
    assert _target_allowed("aws/bedrock-agentcore-sdk-typescript", _MIXED)
    assert _target_allowed("aws/bedrock-agentcore-starter-toolkit", _MIXED)
    assert _target_allowed("aws/agentcore-cli", _MIXED)
    # A different aws repo NOT matching the glob is denied — the org is not wholesale-allowed.
    assert not _target_allowed("aws/aws-cli", _MIXED)
    assert not _target_allowed("aws/some-other-repo", _MIXED)


def test_target_allowed_bare_owner_never_satisfies_a_repo_glob_only_org():
    # 'aws' appears ONLY as repo globs, never as a bare owner → a bare 'aws' target (repo unknown)
    # must be DENIED (fail-closed). This is the strict/unverifiable-repo case.
    assert not _target_allowed("aws", _MIXED)


def test_target_allowed_glob_does_not_leak_across_owner():
    # The glob is anchored to its owner: a lookalike owner must not match.
    assert not _target_allowed("notaws/bedrock-agentcore-sdk-python", _MIXED)
    # And a repo that merely starts like the pattern under the wrong owner is denied.
    assert not _target_allowed("evil/bedrock-agentcore-x", _MIXED)


def test_validate_owner_glob_allows_agentcore_node_id(monkeypatch):
    # A comment mutation whose node id resolves to an AgentCore repo is allowed by the glob.
    monkeypatch.setattr(
        gh_tool, "resolve_node_owner", lambda nid, tok: "aws/bedrock-agentcore-sdk-typescript"
    )
    err, resolved = validate_owner(
        {"subjectId": "IC_kwDOagentcore123"},
        allowed_owners=_MIXED, is_mutative=True, strict=True, token="t",
    )
    assert err is None
    assert resolved == "aws/bedrock-agentcore-sdk-typescript"


def test_validate_owner_glob_blocks_other_aws_repo_node_id(monkeypatch):
    # A node id resolving to a non-matching aws repo is blocked — the org isn't wholesale-allowed.
    monkeypatch.setattr(gh_tool, "resolve_node_owner", lambda nid, tok: "aws/aws-cli")
    err, _ = validate_owner(
        {"subjectId": "IC_kwDOawscli999"},
        allowed_owners=_MIXED, is_mutative=True, strict=True, token="t",
    )
    assert err is not None and "aws/aws-cli" in err


def test_validate_owner_glob_blocks_owner_only_target_failclosed():
    # A mutation naming only owner='aws' (no repo) can't be verified against a repo-glob-only org →
    # blocked (fail-closed), the behavior we want for unverifiable external targets.
    err, _ = validate_owner(
        {"owner": "aws"}, allowed_owners=_MIXED, is_mutative=True, strict=True, token=None,
    )
    assert err is not None and "not in the allowed list" in err


def test_validate_owner_glob_allows_explicit_owner_and_name(monkeypatch):
    # owner + name variables build owner/repo, which the glob matches — no network needed.
    err, resolved = validate_owner(
        {"owner": "aws", "name": "bedrock-agentcore-sdk-python"},
        allowed_owners=_MIXED, is_mutative=True, strict=True, token="t",
    )
    assert err is None


# ---------------------------------------------------------------------------
# Throttle × repo-scoped targets: resolve_node_owner now returns full owner/repo, and
# internal_owners (mirroring allowed_owners) can contain repo-glob entries. The internal
# exemption must (a) still recognize an internal target resolved to owner/repo, and (b) NOT
# exempt external repos that are merely write-allowed via a repo glob / literal repo entry.
# ---------------------------------------------------------------------------
from strandly_harness.core.config import GitHubSettings  # noqa: E402
from strandly_harness.tools.github import enforce_throttle  # noqa: E402

_GH_THROTTLED = GitHubSettings(
    allowed_owners=("strands-agents", "aws/bedrock-agentcore-*", "aws/agentcore-cli"),
    internal_owners=("strands-agents", "aws/bedrock-agentcore-*", "aws/agentcore-cli"),
    throttle_enabled=True,
    throttle_limit=50,
)


@pytest.fixture
def _throttle_at_limit(monkeypatch):
    """Prime the throttle cache at the limit so any non-internal target is blocked."""
    gh_tool.invalidate_throttle_cache()
    monkeypatch.setitem(gh_tool._throttle_cache, "value", 50)
    monkeypatch.setitem(gh_tool._throttle_cache, "ts", __import__("time").time())
    yield
    gh_tool.invalidate_throttle_cache()


def test_throttle_internal_target_resolved_to_owner_repo_is_exempt(_throttle_at_limit):
    # resolve_node_owner returns 'owner/repo' now — an internal write routed by node id must
    # still be exempt from the external-write throttle (regression: exact-match on the full
    # string treated it as external and blocked it at the limit).
    allowed, msg = enforce_throttle("strands-agents/sdk-python", gh=_GH_THROTTLED, token="t")
    assert allowed
    assert "internal" in msg


def test_throttle_bare_internal_owner_still_exempt(_throttle_at_limit):
    allowed, _ = enforce_throttle("strands-agents", gh=_GH_THROTTLED, token="t")
    assert allowed


def test_throttle_glob_allowed_external_repo_is_NOT_exempt(_throttle_at_limit):
    # A repo allowed for writes via a glob (or a literal owner/repo entry) is still EXTERNAL:
    # it must be throttled, not exempted by the internal_owners mirror containing the entry.
    allowed, msg = enforce_throttle(
        "aws/bedrock-agentcore-sdk-python", gh=_GH_THROTTLED, token="t"
    )
    assert not allowed and "throttle reached" in msg
    # Even an exact-string internal entry like 'aws/agentcore-cli' must not exempt itself.
    allowed, msg = enforce_throttle("aws/agentcore-cli", gh=_GH_THROTTLED, token="t")
    assert not allowed and "throttle reached" in msg
