"""Regression tests for the ``validate_owner`` write-authz guardrail in the github tool.

Focus: the inline-node-id mutation bypass a fresh-context reviewer reproduced on PR #35
(turning the owner write allow-list ON). ``validate_owner`` historically inspected only the
``variables`` dict and never the GraphQL **query body**, so two model-controllable vectors
defeated the allow-list:

* **Vector A** — an inline ``subjectId: "PR_kwDO…"`` literal with ``variables={}``: the old code
  short-circuited to *allow* a mutation whenever ``variables`` was falsy, returning before the
  strict-mode unresolvable check.
* **Vector B** — a decoy allowed ``owner`` var alongside an external node id inlined in the body:
  the old code matched the decoy ``owner`` and returned early without ever checking the inline id.

These tests are network-free: ``resolve_node_owner`` (the only seam that would touch the network)
is monkeypatched, and the integration tests assert the mutation is blocked *before* ``_graphql``
is ever called.
"""

from __future__ import annotations

import pytest

from strandly_harness.core.config import GitHubSettings
from strandly_harness.tools import github as gh
from strandly_harness.tools.github import (
    extract_node_ids_from_query,
    extract_node_ids_from_variables,
    make_use_github,
    validate_owner,
)

ALLOWED = {"strands-agents", "strands-labs"}
# A node id whose owner is OUTSIDE the allow-list (the attacker's target).
EXTERNAL_NODE = "PR_kwDOExternalEvilRepo01234567"
# A node id whose owner IS in the allow-list (a legitimate target).
ALLOWED_NODE = "PR_kwDOAllowedRepo9876543210"
# A node id no resolver can map to an owner (e.g. fabricated / inaccessible).
UNRESOLVABLE_NODE = "PR_kwDOUnknownUnresolvable999"
# Repo-scoped node types the resolve query has NO fragment for (Label / Milestone): collected as
# targets but ``resolve_node_owner`` returns None for them — the unresolvable decoy-shadow vectors.
UNRESOLVABLE_LABEL_NODE = "LA_kwDOExternalRealLabel012345"
UNRESOLVABLE_MILESTONE_NODE = "MI_kwDOExternalRealMilestone67"
# A real external PR whose resolution returns None transiently (API blip swallowed to None).
TRANSIENT_NONE_PR_NODE = "PR_kwDOExternalTransientBlip42"
# Legacy (pre-2022) base64 node id — NO ``TYPE_`` prefix. Decodes to ``011:PullRequest514607099``;
# its owner is OUTSIDE the allow-list (mapped to evil-external-org in the resolvers below).
EXTERNAL_LEGACY_NODE = "MDExOlB1bGxSZXF1ZXN0NTE0NjA3MDk5"
# Base64-looking but BENIGN literal (decodes to "HelloWorldTestValue123", not the legacy node
# shape). Must NOT be collected as a node id, else a legit target-less mutation is over-blocked.
BENIGN_BASE64_STRING = "SGVsbG9Xb3JsZFRlc3RWYWx1ZTEyMw=="


@pytest.fixture
def patched_resolver(monkeypatch):
    """Map node ids to owners without any network call (the resolution seam)."""

    def _resolve(node_id: str, token: str | None) -> str | None:
        return {
            EXTERNAL_NODE: "evil-external-org",
            EXTERNAL_LEGACY_NODE: "evil-external-org",
            ALLOWED_NODE: "strands-agents",
        }.get(node_id)  # everything else (incl. UNRESOLVABLE_NODE) -> None

    monkeypatch.setattr(gh, "resolve_node_owner", _resolve)
    return _resolve


def _validate(variables, query, *, strict=True):
    return validate_owner(
        variables,
        allowed_owners=ALLOWED,
        is_mutative=True,
        strict=strict,
        token="t",
        query=query,
    )


# ---------------------------------------------------------------------------
# Helper-level: the new query-body scanner
# ---------------------------------------------------------------------------
def test_extract_node_ids_from_query_finds_inline_literal():
    q = f'mutation {{ addComment(input: {{subjectId: "{EXTERNAL_NODE}", body: "hi"}}) {{ id }} }}'
    assert extract_node_ids_from_query(q) == [EXTERNAL_NODE]


def test_extract_node_ids_from_query_dedupes_and_is_order_preserving():
    q = f'mutation {{ a(id: "{EXTERNAL_NODE}") b(id: "{ALLOWED_NODE}") c(id: "{EXTERNAL_NODE}") }}'
    assert extract_node_ids_from_query(q) == [EXTERNAL_NODE, ALLOWED_NODE]


def test_extract_node_ids_from_query_empty_for_plain_body():
    assert extract_node_ids_from_query("mutation { updateUserStatus(input: {message: \"afk\"}) { id } }") == []
    assert extract_node_ids_from_query("") == []


def test_extract_node_ids_from_query_ignores_long_enum_literals():
    """Free-form query text must not flag long SCREAMING_CASE enum literals as node ids.

    The bare ``len >= 16`` fallback (fine for discrete variable values) would otherwise treat a
    long single-underscore enum like ``DISCUSSION_CATEGORY`` as a node id and — with no resolvable
    owner under strict mode — over-block a legitimate target-less mutation. Body literals must
    carry a recognised repo-scoped prefix to count.
    """
    body = (
        "mutation { createDiscussion(input: {repositoryId: $r, "
        "categoryType: DISCUSSION_CATEGORY_ANNOUNCEMENT, title: \"hi\"}) { id } }"
    )
    assert extract_node_ids_from_query(body) == []


# ---------------------------------------------------------------------------
# Vector A — inline node id + empty variables → BLOCKED under strict mode
# ---------------------------------------------------------------------------
def test_vector_a_inline_node_empty_variables_blocked(patched_resolver):
    q = f'mutation {{ addComment(input: {{subjectId: "{EXTERNAL_NODE}", body: "x"}}) {{ id }} }}'
    err, owner = _validate({}, q)
    assert err is not None, "Vector A must be blocked: external node inlined with empty variables"
    assert EXTERNAL_NODE in err and "evil-external-org" in err
    assert owner is None


def test_vector_a_unresolvable_inline_node_empty_variables_blocked_strict(patched_resolver):
    """Even when the inline node can't be resolved, strict mode must block (empty variables)."""
    q = f'mutation {{ addComment(input: {{subjectId: "{UNRESOLVABLE_NODE}"}}) {{ id }} }}'
    err, owner = _validate({}, q, strict=True)
    assert err is not None and "could not be resolved" in err
    assert owner is None


# ---------------------------------------------------------------------------
# Vector B — decoy allowed owner var + inline external node id → BLOCKED
# ---------------------------------------------------------------------------
def test_vector_b_decoy_owner_var_inline_external_node_blocked(patched_resolver):
    q = f'mutation {{ addComment(input: {{subjectId: "{EXTERNAL_NODE}", body: "x"}}) {{ id }} }}'
    err, owner = _validate({"owner": "strands-agents"}, q)
    assert err is not None, "Vector B must be blocked: decoy owner var must not shadow inline node"
    assert EXTERNAL_NODE in err and "evil-external-org" in err
    assert owner is None


def test_vector_b_decoy_owner_var_inline_node_in_variables_blocked(patched_resolver):
    """The decoy-shadow hole also applies when the external node is a discrete variable value."""
    err, owner = _validate({"owner": "strands-agents", "subjectId": EXTERNAL_NODE}, "mutation { x }")
    assert err is not None and "evil-external-org" in err
    assert owner is None


# ---------------------------------------------------------------------------
# Vectors P2/P3/P4 — decoy ALLOWED owner var + an UNRESOLVABLE node id → BLOCKED.
#
# Regression for the iteration-1 blocker: Layer 1 (explicit owner var) used to ``return`` before
# Layer 3 (strict unresolvable check), so a decoy *allowed* ``owner`` variable short-circuited to
# ALLOW while an inline node id that NONE resolve to an owner (repo-scoped types the resolve query
# has no fragment for — Label ``LA_``/Milestone ``MI_`` — or any transient API failure swallowed to
# None) slipped straight through. The strict fail-closed block must now run FIRST, so a decoy owner
# can never shadow an unresolvable target. ``resolve_node_owner`` returns None for all three node
# ids below (they are not in ``patched_resolver``'s map).
# ---------------------------------------------------------------------------
def test_p2_decoy_owner_unresolvable_label_node_blocked(patched_resolver):
    """P2: decoy owner=strands-agents + unresolvable LA_ (Label) node → must BLOCK under strict."""
    q = f'mutation {{ deleteLabel(input: {{id: "{UNRESOLVABLE_LABEL_NODE}"}}) {{ clientMutationId }} }}'
    err, owner = _validate({"owner": "strands-agents"}, q, strict=True)
    assert err is not None, "P2 must BLOCK: decoy allowed owner must not shadow an unresolvable LA_ node"
    assert "could not be resolved" in err and UNRESOLVABLE_LABEL_NODE in err
    assert owner is None


def test_p3_decoy_owner_unresolvable_milestone_node_blocked(patched_resolver):
    """P3: decoy owner=strands-labs + unresolvable MI_ (Milestone) node → must BLOCK under strict."""
    q = f'mutation {{ deleteMilestone(input: {{id: "{UNRESOLVABLE_MILESTONE_NODE}"}}) {{ clientMutationId }} }}'
    err, owner = _validate({"owner": "strands-labs"}, q, strict=True)
    assert err is not None, "P3 must BLOCK: decoy allowed owner must not shadow an unresolvable MI_ node"
    assert "could not be resolved" in err and UNRESOLVABLE_MILESTONE_NODE in err
    assert owner is None


def test_p4_decoy_owner_transient_unresolvable_pr_node_blocked(patched_resolver):
    """P4: decoy owner + external PR whose resolution returns None (transient API blip) → BLOCK."""
    q = f'mutation {{ closePullRequest(input: {{pullRequestId: "{TRANSIENT_NONE_PR_NODE}"}}) {{ clientMutationId }} }}'
    err, owner = _validate({"owner": "strands-agents"}, q, strict=True)
    assert err is not None, "P4 must BLOCK: a transient resolve->None must fail closed, not be shadowed"
    assert "could not be resolved" in err and TRANSIENT_NONE_PR_NODE in err
    assert owner is None


def test_p2_decoy_owner_unresolvable_node_in_variables_blocked(patched_resolver):
    """The decoy+unresolvable shadow also applies when the node id is a discrete variable value."""
    err, owner = _validate(
        {"owner": "strands-agents", "id": UNRESOLVABLE_LABEL_NODE}, "mutation { deleteLabel { x } }", strict=True
    )
    assert err is not None and "could not be resolved" in err
    assert owner is None


def test_decoy_owner_unresolvable_node_allowed_when_not_strict(patched_resolver):
    """Sanity: the unresolvable fail-closed block is strict-mode only — non-strict honours the owner."""
    q = f'mutation {{ deleteLabel(input: {{id: "{UNRESOLVABLE_LABEL_NODE}"}}) {{ clientMutationId }} }}'
    err, owner = _validate({"owner": "strands-agents"}, q, strict=False)
    assert err is None
    assert owner == "strands-agents"


# ---------------------------------------------------------------------------
# Positive paths — must NOT over-block
# ---------------------------------------------------------------------------
def test_targetless_mutation_allowed(patched_resolver):
    """No owner var and no node id anywhere → a user-/schema-scoped mutation stays allowed."""
    q = 'mutation { updateUserStatus(input: {message: "afk"}) { clientMutationId } }'
    err, owner = _validate({}, q)
    assert err is None
    assert owner is None


def test_inline_node_for_allowed_owner_resolves_and_allowed(patched_resolver):
    q = f'mutation {{ addComment(input: {{subjectId: "{ALLOWED_NODE}"}}) {{ id }} }}'
    err, owner = _validate({}, q)
    assert err is None
    assert owner == "strands-agents"


def test_allowed_owner_var_with_no_nodes_allowed(patched_resolver):
    err, owner = _validate({"owner": "strands-agents"}, "mutation { createIssue { id } }")
    assert err is None
    assert owner == "strands-agents"


def test_allowed_owner_var_plus_allowed_inline_node_allowed(patched_resolver):
    q = f'mutation {{ addComment(input: {{subjectId: "{ALLOWED_NODE}"}}) {{ id }} }}'
    err, owner = _validate({"owner": "strands-labs"}, q)
    assert err is None
    assert owner in {"strands-agents", "strands-labs"}


def test_explicit_external_owner_var_blocked(patched_resolver):
    err, owner = _validate({"owner": "evil-external-org"}, "mutation { createIssue { id } }")
    assert err is not None and "evil-external-org" in err
    assert owner is None


# ---------------------------------------------------------------------------
# Reads and disabled-guardrail behaviour must be unchanged
# ---------------------------------------------------------------------------
def test_read_query_not_subject_to_node_scan(patched_resolver, monkeypatch):
    """A read (is_mutative=False) is owner-checked via the explicit var only — no node resolution."""
    calls = {"n": 0}

    def _spy(node_id, token):
        calls["n"] += 1
        return "evil-external-org"

    monkeypatch.setattr(gh, "resolve_node_owner", _spy)
    q = f'query {{ node(id: "{EXTERNAL_NODE}") {{ id }} }}'
    err, owner = validate_owner(
        {}, allowed_owners=ALLOWED, is_mutative=False, strict=True, token="t", query=q
    )
    assert err is None  # reads aren't blocked by the write allow-list here
    assert calls["n"] == 0  # and we did NOT resolve any node id for a read


def test_guardrail_off_when_no_allowlist(patched_resolver):
    q = f'mutation {{ addComment(input: {{subjectId: "{EXTERNAL_NODE}"}}) {{ id }} }}'
    err, owner = validate_owner(
        {}, allowed_owners=set(), is_mutative=True, strict=True, token="t", query=q
    )
    assert err is None  # allow-list empty → guardrail OFF, nothing blocked


# ---------------------------------------------------------------------------
# Integration: the bypass is blocked through the actual use_github tool, and a
# blocked call NEVER reaches the network (_graphql).
# ---------------------------------------------------------------------------
@pytest.fixture
def github_tool(monkeypatch):
    monkeypatch.setenv("STRANDLY_GITHUB_TOKEN", "ghp_test")

    def _resolve(node_id, token):
        return {
            EXTERNAL_NODE: "evil-external-org",
            EXTERNAL_LEGACY_NODE: "evil-external-org",
            ALLOWED_NODE: "strands-agents",
        }.get(node_id)

    monkeypatch.setattr(gh, "resolve_node_owner", _resolve)

    def _no_network(query, variables, token):
        raise AssertionError(f"network reached for query: {query!r}")

    monkeypatch.setattr(gh, "_graphql", _no_network)

    settings = GitHubSettings(allowed_owners=ALLOWED, internal_owners=ALLOWED)
    return make_use_github(settings)


def test_tool_blocks_vector_a(github_tool):
    q = f'mutation {{ addComment(input: {{subjectId: "{EXTERNAL_NODE}", body: "x"}}) {{ id }} }}'
    result = github_tool(query_type="mutation", query=q, label="poc-A", variables={})
    assert result["status"] == "error"
    assert "evil-external-org" in result["content"][0]["text"]


def test_tool_blocks_vector_b(github_tool):
    q = f'mutation {{ addComment(input: {{subjectId: "{EXTERNAL_NODE}", body: "x"}}) {{ id }} }}'
    result = github_tool(
        query_type="mutation", query=q, label="poc-B", variables={"owner": "strands-agents"}
    )
    assert result["status"] == "error"
    assert "evil-external-org" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# GENERAL CLASS — per-node AND semantics (iteration-3 structural fix)
# ---------------------------------------------------------------------------
# The iter-2 fix moved the strict unresolvable-node block ahead of the explicit
# ``owner`` var, but it still gated on a single ``resolved_owner is None`` — OR
# semantics. So a decoy node that *does* resolve to an ALLOWED owner set
# ``resolved_owner`` non-None and let an UNRESOLVABLE external node (Label
# ``LA_``/Milestone ``MI_`` types the resolve query has no fragment for, or any
# transient resolve→None) ride through in the SAME mutation. The fix tracks
# unresolved ids per-node: under strict, EVERY node id must resolve to an
# allowed owner; ANY unresolved id blocks — regardless of a resolvable-allowed
# sibling and regardless of any decoy ``owner`` var.
def test_general_decoy_allowed_node_plus_unresolvable_label_blocked(patched_resolver):
    """Mixed vector from iter-2 review: decoy resolvable-ALLOWED node + unresolvable LA_ → BLOCK."""
    q = (
        f'mutation {{'
        f'  c: addComment(input: {{subjectId: "{ALLOWED_NODE}", body: "decoy"}}) {{ clientMutationId }}'
        f'  l: addLabelsToLabelable(input: {{labelableId: "{UNRESOLVABLE_LABEL_NODE}"}}) {{ clientMutationId }}'
        f'}}'
    )
    err, owner = _validate({}, q, strict=True)
    assert err is not None and "cannot verify" in err
    # Only the UNRESOLVABLE id is named as the blocker — not the resolvable-allowed sibling.
    assert UNRESOLVABLE_LABEL_NODE in err
    assert ALLOWED_NODE not in err
    assert owner is None


def test_general_decoy_allowed_node_plus_unresolvable_milestone_blocked(patched_resolver):
    """Decoy resolvable-ALLOWED node + unresolvable MI_ (Milestone) in one mutation → BLOCK."""
    q = (
        f'mutation {{'
        f'  c: addComment(input: {{subjectId: "{ALLOWED_NODE}"}}) {{ clientMutationId }}'
        f'  m: updateMilestone(input: {{milestoneId: "{UNRESOLVABLE_MILESTONE_NODE}"}}) {{ clientMutationId }}'
        f'}}'
    )
    err, owner = _validate({}, q, strict=True)
    assert err is not None and "cannot verify" in err
    assert UNRESOLVABLE_MILESTONE_NODE in err
    assert owner is None


def test_general_decoy_allowed_node_plus_decoy_owner_plus_unresolvable_blocked(patched_resolver):
    """Decoy resolvable-ALLOWED node + decoy allowed ``owner`` var + unresolvable external → BLOCK.

    Belt-and-suspenders: neither the resolvable-allowed sibling NOR the decoy owner var may shadow
    the unresolvable external node under strict mode.
    """
    q = (
        f'mutation {{'
        f'  c: addComment(input: {{subjectId: "{ALLOWED_NODE}"}}) {{ clientMutationId }}'
        f'  l: addLabelsToLabelable(input: {{labelableId: "{UNRESOLVABLE_LABEL_NODE}"}}) {{ clientMutationId }}'
        f'}}'
    )
    err, owner = _validate({"owner": "strands-agents"}, q, strict=True)
    assert err is not None and "cannot verify" in err
    assert UNRESOLVABLE_LABEL_NODE in err
    assert owner is None


def test_general_decoy_allowed_node_plus_transient_none_blocked(patched_resolver):
    """Decoy resolvable-ALLOWED node + a node that resolves to None transiently → BLOCK under strict."""
    q = (
        f'mutation {{'
        f'  c: addComment(input: {{subjectId: "{ALLOWED_NODE}"}}) {{ clientMutationId }}'
        f'  r: addPullRequestReview(input: {{pullRequestId: "{TRANSIENT_NONE_PR_NODE}"}}) {{ clientMutationId }}'
        f'}}'
    )
    err, owner = _validate({}, q, strict=True)
    assert err is not None and "cannot verify" in err
    assert TRANSIENT_NONE_PR_NODE in err
    assert owner is None


def test_general_decoy_allowed_node_plus_external_resolvable_blocked(patched_resolver):
    """Sanity: a sibling that resolves to a NON-allowed owner still blocks (named-owner path)."""
    q = (
        f'mutation {{'
        f'  c: addComment(input: {{subjectId: "{ALLOWED_NODE}"}}) {{ clientMutationId }}'
        f'  d: addComment(input: {{subjectId: "{EXTERNAL_NODE}"}}) {{ clientMutationId }}'
        f'}}'
    )
    err, owner = _validate({}, q, strict=True)
    assert err is not None and "evil-external-org" in err
    assert owner is None


def test_general_all_allowed_nodes_not_overblocked(patched_resolver):
    """No over-block: when every discovered node resolves to an allowed owner, the AND gate passes."""
    q = (
        f'mutation {{'
        f'  c: addComment(input: {{subjectId: "{ALLOWED_NODE}"}}) {{ clientMutationId }}'
        f'}}'
    )
    err, owner = _validate({"owner": "strands-labs"}, q, strict=True)
    assert err is None
    assert owner in {"strands-agents", "strands-labs"}


def test_general_mixed_unresolvable_allowed_in_non_strict_not_blocked(patched_resolver):
    """The per-node unresolvable gate is strict-only; non-strict still honours the resolvable target."""
    q = (
        f'mutation {{'
        f'  c: addComment(input: {{subjectId: "{ALLOWED_NODE}"}}) {{ clientMutationId }}'
        f'  l: addLabelsToLabelable(input: {{labelableId: "{UNRESOLVABLE_LABEL_NODE}"}}) {{ clientMutationId }}'
        f'}}'
    )
    err, owner = _validate({}, q, strict=False)
    assert err is None
    assert owner == "strands-agents"


def test_tool_blocks_general_mixed_decoy_allowed_plus_unresolvable_label(github_tool, monkeypatch):
    """Integration: mixed decoy-allowed + unresolvable LA_ is blocked through the real tool, no network."""
    # The shared github_tool fixture's resolver only maps EXTERNAL/ALLOWED; LA_ already → None.
    q = (
        f'mutation {{'
        f'  c: addComment(input: {{subjectId: "{ALLOWED_NODE}", body: "x"}}) {{ clientMutationId }}'
        f'  l: addLabelsToLabelable(input: {{labelableId: "{UNRESOLVABLE_LABEL_NODE}"}}) {{ clientMutationId }}'
        f'}}'
    )
    result = github_tool(query_type="mutation", query=q, label="poc-general", variables={})
    assert result["status"] == "error"
    assert "cannot verify" in result["content"][0]["text"]
    assert UNRESOLVABLE_LABEL_NODE in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# CLASSIFIER BYPASS (iteration-3 blocker) — a leading GraphQL *ignored token*
# (``#`` comment line / comma / whitespace / BOM) must not let a mutation sent
# with ``query_type="query"`` skip the guardrail. ``is_mutation_query`` strips
# all leading ignored tokens BEFORE the ``startswith("mutation")`` check and
# fails CLOSED (a body whose first significant token is ``mutation`` is a
# mutation regardless of the caller-supplied ``query_type``).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "prefix",
    [
        "# sneaky comment\n",
        "# c1\n# c2\n",
        ",",
        "  \n\t ",
        "\ufeff",
        "\ufeff# comment\n ,\n",
        # bare-CR (U+000D) line terminators — a GraphQL comment ends at a bare \r too
        # (graphql-js/graphql-ruby), so these must NOT swallow the ``mutation`` keyword.
        "# x\r",
        "# x\r\n",  # CRLF
        "# c1\r# c2\r",
        "\ufeff# c\r,\r",
        ",\r# c\r  ",
    ],
)
def test_is_mutation_query_ignored_token_prefix_is_mutative(prefix):
    """A leading comment / comma / whitespace / BOM before ``mutation`` still classifies mutative,
    even when the caller mislabels it ``query_type="query"`` (fail-closed)."""
    q = prefix + 'mutation { addComment(input: {subjectId: "PR_x"}) { id } }'
    assert gh.is_mutation_query(q, query_type="query") is True


def test_is_mutation_query_does_not_regress_reads():
    """Legit reads (incl. comment/BOM-prefixed and anonymous shorthand) stay classified as reads."""
    assert gh.is_mutation_query("query { viewer { login } }", "query") is False
    assert gh.is_mutation_query("# a comment\nquery { viewer { login } }", "query") is False
    assert gh.is_mutation_query("\ufeff  { viewer { login } }", "query") is False
    assert gh.is_mutation_query(",\n  query { repository(owner: \"x\") { id } }", "query") is False


# ---------------------------------------------------------------------------
# BARE-CR comment terminator (round-5 blocker). A GraphQL comment ends at a bare
# carriage return ``\r`` (U+000D) — LineTerminator includes CR, so GitHub would
# execute the trailing ``mutation``. The classifier must therefore end the
# leading ``#`` comment at ``\r`` (not swallow it + the keyword), otherwise
# ``# x\rmutation{...}`` stripped to '' → misclassified read → allow-list skipped.
# ---------------------------------------------------------------------------
def test_is_mutation_query_bare_cr_terminates_comment():
    """bare-CR / CRLF / LF after a leading ``#`` comment all still classify mutative
    (query_type='query' must not let them fail open)."""
    assert gh.is_mutation_query("# x\rmutation { addComment { id } }", "query") is True
    assert gh.is_mutation_query("# x\r\nmutation { addComment { id } }", "query") is True
    assert gh.is_mutation_query("# x\nmutation { addComment { id } }", "query") is True
    # interleaved BOM + comma + bare-CR comment run, then the real keyword
    assert gh.is_mutation_query("\ufeff,# c\r mutation { addComment { id } }", "query") is True


def test_is_mutation_query_bare_cr_read_not_overblocked():
    """A legit read whose leading bare-CR comment merely *mentions* ``mutation``
    stays classified as a READ (no over-block → not subjected to node-scan/throttle)."""
    assert gh.is_mutation_query("# mutation cool\rquery { viewer { login } }", "query") is False
    assert gh.is_mutation_query("# talk about mutation\r\nquery { viewer { login } }", "query") is False


def test_strip_leading_ignored_tokens_bare_cr_fixed_point():
    """The stripper reaches the real leading token across bare-CR/CRLF/LF comment runs."""
    assert gh._strip_leading_ignored_tokens("# x\rmutation{a}") == "mutation{a}"
    assert gh._strip_leading_ignored_tokens("# x\r\nmutation{a}") == "mutation{a}"
    assert gh._strip_leading_ignored_tokens("\ufeff,# c\r mutation{a}") == "mutation{a}"
    # read body preserved (comment mentioning mutation does not eat the query keyword)
    assert gh._strip_leading_ignored_tokens("# mutation cool\rquery{v}") == "query{v}"


@pytest.mark.parametrize(
    "prefix",
    [
        "# sneaky comment\n",
        ",",
        "  \n\t ",
        "\ufeff",
        "\ufeff# c\n,",
        # bare-CR line terminators (round-5 blocker): the comment must end at the \r
        # so the trailing ``mutation`` is still classified + routed through the allow-list.
        "# x\r",
        "# x\r\n",  # CRLF
        "\ufeff# c\r,\r",
    ],
)
def test_tool_blocks_classifier_bypass_external_target(github_tool, prefix):
    """End-to-end: an ignored-token-prefixed mutation LABELLED ``query_type="query"`` still routes
    its external node target through the allow-list and is BLOCKED before the network is reached."""
    q = prefix + f'mutation {{ addComment(input: {{subjectId: "{EXTERNAL_NODE}", body: "x"}}) {{ id }} }}'
    result = github_tool(query_type="query", query=q, label="poc-classifier", variables={})
    assert result["status"] == "error"
    assert "evil-external-org" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# LEGACY base64 node ids (no ``TYPE_`` prefix) — should-fix. Detected PRECISELY:
# a token must be valid base64 that decodes to ``<digits>:<TypeName><digits>``.
# External legacy ids are collected + blocked; benign base64-looking strings
# are NOT collected (no over-block of legit target-less mutations).
# ---------------------------------------------------------------------------
def test_looks_like_legacy_node_id_detects_real_legacy_id():
    assert gh._looks_like_legacy_node_id(EXTERNAL_LEGACY_NODE) is True


def test_looks_like_legacy_node_id_rejects_benign_base64():
    assert gh._looks_like_legacy_node_id(BENIGN_BASE64_STRING) is False
    # Not base64 at all / wrong length / decodes to non-legacy text — all rejected.
    assert gh._looks_like_legacy_node_id("not base64 !!!") is False
    assert gh._looks_like_legacy_node_id("PR_kwDOAllowedRepo9876543210") is False


def test_extract_legacy_node_from_query_body():
    q = f'mutation {{ closePullRequest(input: {{pullRequestId: "{EXTERNAL_LEGACY_NODE}"}}) {{ clientMutationId }} }}'
    assert extract_node_ids_from_query(q) == [EXTERNAL_LEGACY_NODE]


def test_extract_legacy_node_from_variables():
    assert extract_node_ids_from_variables({"id": EXTERNAL_LEGACY_NODE}) == [EXTERNAL_LEGACY_NODE]


def test_benign_base64_not_collected_from_query():
    q = f'mutation {{ createIssue(input: {{title: "{BENIGN_BASE64_STRING}"}}) {{ id }} }}'
    assert extract_node_ids_from_query(q) == []


def test_benign_base64_not_collected_from_variables():
    assert extract_node_ids_from_variables({"title": BENIGN_BASE64_STRING}) == []


def test_validate_blocks_external_legacy_id_empty_variables(patched_resolver):
    """External legacy node id inlined + empty variables → BLOCKED (owner outside allow-list)."""
    q = f'mutation {{ closePullRequest(input: {{pullRequestId: "{EXTERNAL_LEGACY_NODE}"}}) {{ clientMutationId }} }}'
    err, owner = _validate({}, q)
    assert err is not None and "evil-external-org" in err
    assert owner is None


def test_validate_benign_base64_targetless_not_overblocked(patched_resolver):
    """A benign base64-looking literal yields NO target → target-less mutation → ALLOWED (strict)."""
    q = f'mutation {{ createIssue(input: {{title: "{BENIGN_BASE64_STRING}"}}) {{ id }} }}'
    err, owner = _validate({}, q, strict=True)
    assert err is None
    assert owner is None


def test_tool_blocks_external_legacy_id_empty_variables(github_tool):
    """End-to-end: external legacy id + empty variables is blocked BEFORE the network."""
    q = f'mutation {{ closePullRequest(input: {{pullRequestId: "{EXTERNAL_LEGACY_NODE}"}}) {{ clientMutationId }} }}'
    result = github_tool(query_type="mutation", query=q, label="poc-legacy", variables={})
    assert result["status"] == "error"
    assert "evil-external-org" in result["content"][0]["text"]


def test_tool_benign_base64_not_overblocked(github_tool):
    """A benign base64-looking literal must NOT be treated as a node id: the mutation is target-less
    so the allow-list doesn't block it — it proceeds to execution (the mocked network is reached)."""
    q = f'mutation {{ createIssue(input: {{title: "{BENIGN_BASE64_STRING}"}}) {{ id }} }}'
    result = github_tool(query_type="mutation", query=q, label="benign", variables={})
    assert result["status"] == "error"
    # Reached _graphql (our _no_network sentinel) rather than being guardrail-blocked → no over-block.
    assert "network reached" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# _get_token: config-resolved token (secret) vs os.environ fallback
# ---------------------------------------------------------------------------
def test_get_token_prefers_config_resolved_token(monkeypatch):
    # Regression (prod): on the deployed runtime the token lives ONLY in the config secret and never
    # reaches os.environ, so use_github must read the token GitHubSettings carries from the loaded
    # Config. With no token env vars set, the config token must still be used.
    for name in ("STRANDLY_GITHUB_TOKEN", "GITHUB_TOKEN", "PAT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    settings = GitHubSettings(token="ghp_fromsecret")
    assert gh._get_token(settings, use_pat_token=False) == "ghp_fromsecret"


def test_get_token_falls_back_to_environ_when_no_config_token(monkeypatch):
    # Local / GitHub-Actions: no config token → scan the env var names (the original behavior).
    for name in ("STRANDLY_GITHUB_TOKEN", "GITHUB_TOKEN", "PAT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fromenv")
    settings = GitHubSettings()  # token=None
    assert gh._get_token(settings, use_pat_token=False) == "ghp_fromenv"
