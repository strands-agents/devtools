"""GitHub guardrails behavior and security regression tests."""

from __future__ import annotations

import logging as _logging

import pytest

from strandly.core.config import GitHubSettings
from strandly.tools import github as gh
from strandly.tools import github_client as client
from strandly.tools import github_guardrails as guardrails
from strandly.tools.github import (
    extract_node_ids_from_query,
    extract_node_ids_from_variables,
    make_use_github,
    validate_owner,
)

ALLOWED = {"strands-agents", "strands-labs"}


EXTERNAL_NODE = "PR_kwDOExternalEvilRepo01234567"


ALLOWED_NODE = "PR_kwDOAllowedRepo9876543210"


UNRESOLVABLE_NODE = "PR_kwDOUnknownUnresolvable999"


UNRESOLVABLE_LABEL_NODE = "LA_kwDOExternalRealLabel012345"


UNRESOLVABLE_MILESTONE_NODE = "MI_kwDOExternalRealMilestone67"


TRANSIENT_NONE_PR_NODE = "PR_kwDOExternalTransientBlip42"


EXTERNAL_LEGACY_NODE = "MDExOlB1bGxSZXF1ZXN0NTE0NjA3MDk5"


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

    monkeypatch.setattr(guardrails, "resolve_node_owner", _resolve)
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


REAL_SHAPED_EXTERNAL_NODE = "PR_kwDOD__JMM56Fh5d"


def test_node_id_with_double_underscore_payload_is_recognized():
    """Regression: a payload containing `_` (URL-safe base64) must still count as a node id.

    Confirmed live against a real, currently-resolvable GitHub node id during the incident
    investigation — the un-fixed pattern rejected it outright.
    """
    assert guardrails._looks_like_node_id(REAL_SHAPED_EXTERNAL_NODE) is True


def test_extract_node_ids_from_variables_finds_double_underscore_id_nested():
    variables = {"input": {"subjectId": REAL_SHAPED_EXTERNAL_NODE, "body": "hi"}}
    assert extract_node_ids_from_variables(variables) == [REAL_SHAPED_EXTERNAL_NODE]


def test_vector_c_double_underscore_node_id_nested_variable_blocked(patched_resolver, monkeypatch):
    """The exact incident shape: a nested `input.subjectId` mutation, double-underscore payload.

    Must be blocked exactly like any other external node id once it's actually detected.
    """
    monkeypatch.setattr(
        guardrails, "resolve_node_owner",
        lambda node_id, token: {REAL_SHAPED_EXTERNAL_NODE: "some-unrelated-owner/some-repo"}.get(
            node_id
        ),
    )
    q = (
        'mutation AddComment($input: AddCommentInput!) { addComment(input: $input) '
        '{ commentEdge { node { id } } } }'
    )
    variables = {"input": {"subjectId": REAL_SHAPED_EXTERNAL_NODE, "body": "hi"}}
    err, owner = _validate(variables, q)
    assert err is not None, "the double-underscore node id must be detected and blocked"
    assert REAL_SHAPED_EXTERNAL_NODE in err and "some-unrelated-owner/some-repo" in err
    assert owner is None


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


def test_targetless_user_scoped_mutation_blocked_strict(patched_resolver):
    """No owner var and no node id anywhere: strict mode refuses rather than allowing."""
    q = 'mutation { updateUserStatus(input: {message: "afk"}) { clientMutationId } }'
    err, owner = _validate({}, q)
    assert err is not None and "updateUserStatus" in err and "allow-list" in err
    assert owner is None


def test_inline_node_for_allowed_owner_resolves_and_allowed(patched_resolver):
    q = f'mutation {{ addComment(input: {{subjectId: "{ALLOWED_NODE}"}}) {{ id }} }}'
    err, owner = _validate({}, q)
    assert err is None
    assert owner == "strands-agents"


def test_allowed_owner_var_with_no_target_argument_blocked_strict(patched_resolver):
    """An allowed owner variable does not stand in for the mutation's own target argument."""
    err, owner = _validate({"owner": "strands-agents"}, "mutation { createIssue { id } }")
    assert err is not None and "input.repositoryId" in err
    assert owner is None


def test_allowed_owner_var_with_allowed_target_argument_allowed(patched_resolver):
    q = (
        "mutation($r: ID!) { createIssue(input: {repositoryId: $r, title: \"t\"}) "
        "{ issue { id } } }"
    )
    err, owner = _validate({"owner": "strands-agents", "r": ALLOWED_NODE}, q)
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


def test_read_query_not_subject_to_node_scan(patched_resolver, monkeypatch):
    """A read (is_mutative=False) is owner-checked via the explicit var only — no node resolution."""
    calls = {"n": 0}

    def _spy(node_id, token):
        calls["n"] += 1
        return "evil-external-org"

    monkeypatch.setattr(guardrails, "resolve_node_owner", _spy)
    q = f'query {{ node(id: "{EXTERNAL_NODE}") {{ id }} }}'
    err, owner = validate_owner(
        {}, allowed_owners=ALLOWED, is_mutative=False, strict=True, token="t", query=q
    )
    assert err is None  # reads aren't blocked by the write allow-list here
    assert calls["n"] == 0  # and we did NOT resolve any node id for a read


def test_no_allowlist_fails_closed(patched_resolver):
    q = f'mutation {{ addComment(input: {{subjectId: "{EXTERNAL_NODE}"}}) {{ id }} }}'
    err, owner = validate_owner(
        {}, allowed_owners=set(), is_mutative=True, strict=True, token="t", query=q
    )
    assert err is not None  # allow-list empty → guardrail OFF, nothing blocked


@pytest.fixture
def github_tool(monkeypatch):
    monkeypatch.setenv("STRANDLY_GITHUB_TOKEN", "ghp_test")

    def _resolve(node_id, token):
        return {
            EXTERNAL_NODE: "evil-external-org",
            EXTERNAL_LEGACY_NODE: "evil-external-org",
            ALLOWED_NODE: "strands-agents",
        }.get(node_id)

    monkeypatch.setattr(guardrails, "resolve_node_owner", _resolve)

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
    assert guardrails.is_mutation_query(q, query_type="query") is True


def test_is_mutation_query_does_not_regress_reads():
    """Legit reads (incl. comment/BOM-prefixed and anonymous shorthand) stay classified as reads."""
    assert guardrails.is_mutation_query("query { viewer { login } }", "query") is False
    assert guardrails.is_mutation_query("# a comment\nquery { viewer { login } }", "query") is False
    assert guardrails.is_mutation_query("\ufeff  { viewer { login } }", "query") is False
    assert guardrails.is_mutation_query(",\n  query { repository(owner: \"x\") { id } }", "query") is False


def test_is_mutation_query_bare_cr_terminates_comment():
    """bare-CR / CRLF / LF after a leading ``#`` comment all still classify mutative
    (query_type='query' must not let them fail open)."""
    assert guardrails.is_mutation_query("# x\rmutation { addComment { id } }", "query") is True
    assert guardrails.is_mutation_query("# x\r\nmutation { addComment { id } }", "query") is True
    assert guardrails.is_mutation_query("# x\nmutation { addComment { id } }", "query") is True
    # interleaved BOM + comma + bare-CR comment run, then the real keyword
    assert guardrails.is_mutation_query("\ufeff,# c\r mutation { addComment { id } }", "query") is True


def test_is_mutation_query_bare_cr_read_not_overblocked():
    """A legit read whose leading bare-CR comment merely *mentions* ``mutation``
    stays classified as a READ (no over-block → not subjected to node-scan/throttle)."""
    assert guardrails.is_mutation_query("# mutation cool\rquery { viewer { login } }", "query") is False
    assert guardrails.is_mutation_query("# talk about mutation\r\nquery { viewer { login } }", "query") is False


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


def test_looks_like_legacy_node_id_detects_real_legacy_id():
    assert guardrails._looks_like_legacy_node_id(EXTERNAL_LEGACY_NODE) is True


def test_looks_like_legacy_node_id_rejects_benign_base64():
    assert guardrails._looks_like_legacy_node_id(BENIGN_BASE64_STRING) is False
    # Not base64 at all / wrong length / decodes to non-legacy text — all rejected.
    assert guardrails._looks_like_legacy_node_id("not base64 !!!") is False
    assert guardrails._looks_like_legacy_node_id("PR_kwDOAllowedRepo9876543210") is False


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


def test_validate_benign_base64_not_overblocked(patched_resolver):
    """A benign base64-looking literal is not a node id, so only the real target is resolved."""
    q = (
        f'mutation {{ createIssue(input: {{repositoryId: "{ALLOWED_NODE}", '
        f'title: "{BENIGN_BASE64_STRING}"}}) {{ id }} }}'
    )
    assert extract_node_ids_from_query(q) == [ALLOWED_NODE]
    err, owner = _validate({}, q, strict=True)
    assert err is None
    assert owner == "strands-agents"


def test_tool_blocks_external_legacy_id_empty_variables(github_tool):
    """End-to-end: external legacy id + empty variables is blocked BEFORE the network."""
    q = f'mutation {{ closePullRequest(input: {{pullRequestId: "{EXTERNAL_LEGACY_NODE}"}}) {{ clientMutationId }} }}'
    result = github_tool(query_type="mutation", query=q, label="poc-legacy", variables={})
    assert result["status"] == "error"
    assert "evil-external-org" in result["content"][0]["text"]


def test_tool_benign_base64_not_overblocked(github_tool):
    """A benign base64-looking literal must NOT be treated as a node id: the mutation's only target
    is allowed, so it proceeds to execution (the mocked network is reached)."""
    q = (
        f'mutation {{ createIssue(input: {{repositoryId: "{ALLOWED_NODE}", '
        f'title: "{BENIGN_BASE64_STRING}"}}) {{ id }} }}'
    )
    result = github_tool(query_type="mutation", query=q, label="benign", variables={})
    assert result["status"] == "error"
    # Reached _graphql (our _no_network sentinel) rather than being guardrail-blocked → no over-block.
    assert "network reached" in result["content"][0]["text"]


FALSE_POSITIVE_ENUMS = (
    "CHANGES_REQUESTED",
    "READY_FOR_REVIEW",
    "CONVERT_TO_DRAFT",
    "RESOLVE_CONVERSATION",
)


OPAQUE_NON_PREFIXED_NODE = "PVTI_lAHOD9DpTM4BSejEzgoCavE"


@pytest.mark.parametrize("enum_value", FALSE_POSITIVE_ENUMS)
def test_screaming_case_enum_not_a_node_id_in_variables(enum_value):
    """A SCREAMING_CASE GraphQL enum passed as a *variable* is not a node id."""
    assert guardrails._looks_like_node_id(enum_value) is False
    assert extract_node_ids_from_variables({"event": enum_value}) == []


def test_enum_alongside_real_node_id_only_collects_the_node_id():
    """The review-submission shape ``{pullRequestId: PR_…, event: CHANGES_REQUESTED}``.

    Collecting both would block the legitimate submission: ``CHANGES_REQUESTED`` never resolves,
    and every collected node must pass.
    """
    variables = {"pullRequestId": ALLOWED_NODE, "event": "CHANGES_REQUESTED"}
    assert extract_node_ids_from_variables(variables) == [ALLOWED_NODE]


def test_enum_alongside_real_node_id_validates_ok_under_strict(patched_resolver):
    """7a end-to-end: a real allowed ``PR_`` id + an enum must PASS strict validation."""
    err, owner = _validate(
        {"pullRequestId": ALLOWED_NODE, "event": "CHANGES_REQUESTED"},
        "mutation($pullRequestId: ID!, $event: PullRequestReviewEvent!) { "
        "addPullRequestReview(input: {pullRequestId: $pullRequestId, event: $event}) "
        "{ clientMutationId } }",
        strict=True,
    )
    assert err is None
    assert owner == "strands-agents"


def test_enum_in_nested_variables_is_not_a_node_id():
    """The nested (``input: {...}``) variable shape must get the same treatment."""
    variables = {"input": {"pullRequestId": ALLOWED_NODE, "event": "CHANGES_REQUESTED"}}
    assert extract_node_ids_from_variables(variables) == [ALLOWED_NODE]


def test_long_non_prefixed_opaque_node_id_still_collected_in_variables():
    """ANTI-BYPASS (7a): the ``len >= 16`` fallback must survive the enum fix.

    A ``PVTI_`` project-item id carries no ``REPO_SCOPED_PREFIXES`` prefix, so ONLY the length
    fallback catches it. Narrowing the variables path to known prefixes would have made every such
    id invisible to the owner check — a real allow-list bypass. The enum filter is scoped to the
    fallback *and* to ``^[A-Z][A-Z0-9_]*$``, which no real node id matches (they are base64url and
    contain lowercase).
    """
    assert guardrails._looks_like_node_id(OPAQUE_NON_PREFIXED_NODE) is True
    assert extract_node_ids_from_variables({"projectId": OPAQUE_NON_PREFIXED_NODE}) == [
        OPAQUE_NON_PREFIXED_NODE
    ]


def test_long_non_prefixed_opaque_node_id_still_blocks_under_strict(patched_resolver):
    """ANTI-BYPASS (7a): an unresolvable non-prefixed opaque id still fails closed under strict."""
    err, owner = _validate(
        {"projectId": OPAQUE_NON_PREFIXED_NODE}, "mutation { addProjectV2ItemById { x } }"
    )
    assert err is not None and OPAQUE_NON_PREFIXED_NODE in err
    assert owner is None


def test_repo_scoped_prefixed_ids_are_unaffected_by_the_enum_filter():
    """ANTI-BYPASS (7a): prefixed ids short-circuit BEFORE the fallback, so the filter can't hide
    them even if their base64url payload happened to be all upper-case/digits.

    This is precisely why the enum filter lives in the fallback only and never ahead of the
    ``REPO_SCOPED_PREFIXES`` check: repo-scoped ids are the primary write-authz target.
    """
    assert guardrails._looks_like_node_id("PR_KWDO1234ABCD") is True
    assert guardrails._looks_like_node_id("LA_KWDO1234ABCD") is True


def test_legacy_node_ids_are_unaffected_by_the_enum_filter():
    """ANTI-BYPASS (7a): the legacy detector runs first, so legacy ids never reach the filter."""
    assert guardrails._looks_like_node_id(EXTERNAL_LEGACY_NODE) is True
    assert extract_node_ids_from_variables({"id": EXTERNAL_LEGACY_NODE}) == [EXTERNAL_LEGACY_NODE]


def test_short_enums_remain_non_node_ids():
    """Sanity: the enums that escaped by luck (too short) must stay non-node-ids."""
    for value in ("APPROVE", "COMMENT", "DISMISS", "MERGE", "SQUASH"):
        assert guardrails._looks_like_node_id(value) is False


def test_pr_prefixed_enums_remain_a_documented_residual_false_positive():
    """DELIBERATE residual over-block: ``PR_BODY`` / ``PR_TITLE``.

    These are real GitHub enum values (``MergeCommitMessage`` / ``MergeCommitTitle``) that collide
    with the ``PR_`` repo-scoped prefix, so they are accepted BEFORE the fallback's enum filter.
    Fixing them would mean running the SCREAMING_CASE filter *ahead* of the prefix check — which
    would let any all-upper-case repo-scoped node id go unchecked, i.e. trade a false positive for
    a real allow-list bypass. We keep the over-block and pin it here so the trade-off stays a
    conscious, reviewed decision instead of silently regressing into a bypass.

    Verified against the live API: ``node(id: "PR_BODY")`` -> NOT_FOUND, so this can only ever be
    an over-block (a blocked repo-settings mutation), never an under-block.
    """
    assert guardrails._looks_like_node_id("PR_BODY") is True
    assert guardrails._looks_like_node_id("PR_TITLE") is True


def test_add_labels_mutation_passes_strict_when_label_resolves(monkeypatch):
    """7b end-to-end: ``addLabelsToLabelable`` was ALWAYS blocked; now it validates."""
    monkeypatch.setattr(
        client, "_graphql",
        lambda q, v, t: {
            "data": {
                "node": {
                    "__typename": "Label" if v["id"].startswith("LA_") else "Issue",
                    "repository": {"nameWithOwner": "strands-agents/harness-sdk"},
                }
            }
        },
    )
    err, owner = _validate(
        {"labelableId": "I_kwDOAllowedRealIssue01234", "labelIds": ["LA_kwDOAllowedRealLabel012345"]},
        "mutation($labelableId: ID!, $labelIds: [ID!]!) { addLabelsToLabelable(input: "
        "{labelableId: $labelableId, labelIds: $labelIds}) { clientMutationId } }",
    )
    assert err is None
    assert owner == "strands-agents/harness-sdk"


def test_strict_block_is_logged(patched_resolver, caplog):
    """Every strict block must be logged, not just resolution errors."""
    with caplog.at_level(_logging.WARNING, logger="strandly.tools.github"):
        err, _ = _validate({}, f'mutation {{ a(id: "{UNRESOLVABLE_NODE}") }}')
    assert err is not None
    messages = [r.getMessage() for r in caplog.records]
    assert any(UNRESOLVABLE_NODE in m for m in messages)


def test_owner_mismatch_block_is_logged(patched_resolver, caplog):
    """The node-id-outside-the-allow-list block must be logged too."""
    with caplog.at_level(_logging.WARNING, logger="strandly.tools.github"):
        err, _ = _validate({}, f'mutation {{ a(id: "{EXTERNAL_NODE}") }}')
    assert err is not None
    messages = [r.getMessage() for r in caplog.records]
    assert any(EXTERNAL_NODE in m and "evil-external-org" in m for m in messages)


def test_explicit_target_block_log_escapes_newlines(caplog):
    """7c hardening: the block log must not be forgeable from untrusted issue/PR text.

    ``explicit_target`` is built from a free-form ``owner`` variable, so unlike a node id (whose
    anchored pattern admits no whitespace) it can carry a newline. Logged with ``%s`` an attacker
    could append a second, fake log line and corrupt the block-rate counts this logging exists to
    measure. ``%r`` escapes it, so the forged text stays on one line.
    """
    evil = "notallowed\nWARNING forged: blocked mutation: 0 of 0 node id(s)"
    with caplog.at_level(_logging.WARNING, logger="strandly.tools.github"):
        err, _ = validate_owner(
            {"owner": evil},
            allowed_owners=ALLOWED,
            is_mutative=True,
            strict=True,
            token="t",
            query="mutation { updateIssue { x } }",
        )
    assert err is not None
    blocks = [r.getMessage() for r in caplog.records if "blocked call" in r.getMessage()]
    assert blocks, "the explicit-target block was not logged"
    assert all("\n" not in m for m in blocks), "raw newline reached the log (forgeable)"
    assert any("\\n" in m for m in blocks), "newline was not repr-escaped"


# One sample id per type that ``github_client._RESOLVE_QUERY`` resolves.
RESOLVABLE_TYPE_IDS = {
    "PullRequest": "PR_kwDOResolvableSample0001",
    "Issue": "I_kwDOResolvableSample0002",
    "IssueComment": "IC_kwDOResolvableSample0003",
    "PullRequestReview": "PRR_kwDOResolvableSample0004",
    "PullRequestReviewComment": "PRRC_kwDOResolvableSample0005",
    "CommitComment": "CC_kwDOResolvableSample0006",
    "Discussion": "D_kwDOResolvableSample0007",
    "DiscussionComment": "DC_kwDOResolvableSample0008",
    "Release": "RE_kwDOResolvableSample0009",
    "Repository": "R_kgDOResolvableSample0010",
    "Label": "LA_kwDOResolvableSample0011",
    "Milestone": "MI_kwDOResolvableSample0012",
    "PullRequestReviewThread": "PRRT_kwDOResolvableSample0013",
    "Ref": "REF_kwDOResolvableSample0014",
    "DiscussionCategory": "DIC_kwDOResolvableSample0015",
}


EXTERNAL_DISCUSSION = "D_kwDOExternalDiscussion01234"
EXTERNAL_REVIEW_THREAD = "PRRT_kwDOExternalReviewThread0123"
ALLOWED_DISCUSSION = "D_kwDOAllowedDiscussion987654"


@pytest.fixture
def typed_resolver(monkeypatch):
    """Resolve the discussion and review-thread fixtures; anything else is unresolvable."""
    calls: list[str] = []

    def _resolve(node_id: str, token: str | None) -> str | None:
        calls.append(node_id)
        return {
            EXTERNAL_DISCUSSION: "evil-external-org/discussions",
            EXTERNAL_REVIEW_THREAD: "evil-external-org/code",
            ALLOWED_DISCUSSION: "strands-agents/harness-sdk",
            ALLOWED_NODE: "strands-agents/harness-sdk",
            EXTERNAL_NODE: "evil-external-org/code",
        }.get(node_id)

    monkeypatch.setattr(guardrails, "resolve_node_owner", _resolve)
    return calls


@pytest.fixture
def no_resolver(monkeypatch):
    """Fail the test if the guardrail tries to resolve anything."""

    def _boom(*_a, **_k):
        raise AssertionError("no node resolution expected")

    monkeypatch.setattr(guardrails, "resolve_node_owner", _boom)


def test_resolve_query_types_all_have_a_known_prefix():
    """Every type ``_RESOLVE_QUERY`` handles has a sample id here and a known prefix."""
    import re

    types = set(re.findall(r"\.\.\. on (\w+)", client._RESOLVE_QUERY))
    assert types == set(RESOLVABLE_TYPE_IDS)
    for node_id in RESOLVABLE_TYPE_IDS.values():
        assert any(node_id.startswith(p) for p in guardrails.REPO_SCOPED_PREFIXES), node_id


@pytest.mark.parametrize("node_id", sorted(RESOLVABLE_TYPE_IDS.values()))
def test_inline_and_variable_extraction_agree(node_id):
    inline = extract_node_ids_from_query(
        f'mutation {{ addComment(input: {{subjectId: "{node_id}", body: "x"}}) {{ clientMutationId }} }}'
    )
    assert inline == extract_node_ids_from_variables({"input": {"subjectId": node_id}}) == [node_id]


@pytest.mark.parametrize("node_id", sorted(RESOLVABLE_TYPE_IDS.values()))
def test_inline_id_of_every_resolvable_type_is_resolved_and_blocked_when_external(
    monkeypatch, node_id
):
    monkeypatch.setattr(guardrails, "resolve_node_owner", lambda nid, t: "evil-external-org/x")
    err, _ = _validate({}, f'mutation {{ addComment(input: {{subjectId: "{node_id}", body: "x"}}) {{ id }} }}')
    assert err is not None and "evil-external-org" in err


def test_inline_discussion_comment_on_external_discussion_blocked(typed_resolver):
    q = (
        f'mutation {{ addDiscussionComment(input: {{discussionId: "{EXTERNAL_DISCUSSION}", '
        f'body: "spam"}}) {{ comment {{ url }} }} }}'
    )
    err, owner = _validate({}, q)
    assert err is not None and "evil-external-org" in err
    assert owner is None
    assert EXTERNAL_DISCUSSION in typed_resolver


def test_inline_discussion_comment_on_unresolvable_discussion_blocked(typed_resolver):
    q = 'mutation { addDiscussionComment(input: {discussionId: "D_kwDOUnknown0000000001", body: "x"}) { clientMutationId } }'
    err, _ = _validate({}, q)
    assert err is not None and "D_kwDOUnknown0000000001" in err


def test_inline_review_thread_reply_on_external_thread_blocked(typed_resolver):
    q = (
        f"mutation {{ addPullRequestReviewThreadReply(input: {{pullRequestReviewThreadId: "
        f'"{EXTERNAL_REVIEW_THREAD}", body: "spam"}}) {{ comment {{ url }} }} }}'
    )
    err, owner = _validate({}, q)
    assert err is not None and "evil-external-org" in err
    assert owner is None


def test_add_reaction_on_external_discussion_blocked(typed_resolver):
    q = f'mutation {{ addReaction(input: {{subjectId: "{EXTERNAL_DISCUSSION}", content: HEART}}) {{ clientMutationId }} }}'
    err, _ = _validate({}, q)
    assert err is not None and "evil-external-org" in err


def test_discussion_comment_on_allowed_discussion_allowed(typed_resolver):
    q = (
        "mutation($id: ID!) { addDiscussionComment(input: {discussionId: $id, body: \"ok\"}) "
        "{ comment { url } } }"
    )
    err, owner = _validate({"id": ALLOWED_DISCUSSION}, q)
    assert err is None
    assert owner == "strands-agents/harness-sdk"


def test_strict_mutation_with_no_target_and_no_owner_variable_blocked(no_resolver):
    err, owner = _validate({}, 'mutation { addComment(input: {body: "x"}) { clientMutationId } }')
    assert err is not None and "cannot verify the target repository" in err
    assert owner is None


def test_strict_mutation_not_on_field_allow_list_blocked(typed_resolver):
    err, _ = _validate(
        {"owner": "strands-agents"},
        f'mutation {{ createProjectV2(input: {{ownerId: "{ALLOWED_NODE}", title: "t"}}) '
        "{ clientMutationId } }",
    )
    assert err is not None and "createProjectV2" in err and "allow-list" in err


def test_targetless_mutation_allowed_when_not_strict(no_resolver):
    err, _ = _validate({}, 'mutation { updateUserStatus(input: {message: "afk"}) { clientMutationId } }', strict=False)
    assert err is None


FRAGMENT_FIRST_MUTATION = (
    "fragment F on IssueComment { id } "
    f'mutation {{ addComment(input: {{subjectId: "{EXTERNAL_NODE}", body: "x"}}) '
    "{ commentEdge { node { ...F } } } }"
)


def test_fragment_first_mutation_labeled_query_is_a_mutation():
    assert gh.is_mutation_query(FRAGMENT_FIRST_MUTATION, "query") is True


def test_fragment_first_mutation_labeled_query_blocked(typed_resolver):
    err, _ = validate_owner(
        {}, allowed_owners=ALLOWED, is_mutative=False, strict=True, token="t",
        query=FRAGMENT_FIRST_MUTATION,
    )
    assert err is not None and "evil-external-org" in err


def test_tool_blocks_fragment_first_mutation_labeled_query(github_tool):
    result = github_tool(query_type="query", query=FRAGMENT_FIRST_MUTATION, label="poc", variables={})
    assert result["status"] == "error"
    assert "evil-external-org" in result["content"][0]["text"]


def test_query_label_never_lowers_a_mutation():
    for q in (
        "query Q { viewer { login } } mutation M { addComment(input: {}) { clientMutationId } }",
        "\ufeff, # c\n mutation { addComment(input: {}) { clientMutationId } }",
    ):
        assert gh.is_mutation_query(q, "query") is True
    assert gh.is_mutation_query("query { viewer { login } }", "query") is False
    assert gh.is_mutation_query("{ viewer { login } }", "") is False


def test_unparseable_document_is_treated_as_a_blocked_write(no_resolver):
    assert gh.is_mutation_query("mutation { addComment(", "query") is True
    err, _ = validate_owner(
        {}, allowed_owners=ALLOWED, is_mutative=False, strict=True, token="t",
        query="mutation { addComment(",
    )
    assert err is not None and "not valid" in err


def test_root_fragment_spread_cannot_hide_a_mutation_field(typed_resolver):
    q = (
        "mutation { ...M } "
        f'fragment M on Mutation {{ createProjectV2(input: {{ownerId: "{ALLOWED_NODE}", title: "t"}}) '
        "{ clientMutationId } }"
    )
    err, _ = _validate({}, q)
    assert err is not None and "createProjectV2" in err


CREATE_COMMIT = (
    "mutation($input: CreateCommitOnBranchInput!) { createCommitOnBranch(input: $input) "
    "{ commit { url } } }"
)


def test_nested_repository_name_with_owner_variable_outside_allow_list_blocked(no_resolver):
    variables = {
        "input": {
            "branch": {"repositoryNameWithOwner": "other-org/repo", "branchName": "main"},
            "message": {"headline": "m"},
            "expectedHeadOid": "abc",
        }
    }
    err, _ = _validate(variables, CREATE_COMMIT)
    assert err is not None and "other-org/repo" in err


def test_nested_repository_name_with_owner_inline_outside_allow_list_blocked(no_resolver):
    q = (
        'mutation { createCommitOnBranch(input: {branch: {repositoryNameWithOwner: "other-org/repo", '
        'branchName: "main"}, message: {headline: "m"}, expectedHeadOid: "abc"}) { commit { url } } }'
    )
    err, _ = _validate({}, q)
    assert err is not None and "other-org/repo" in err


def test_nested_repository_name_with_owner_inside_allow_list_allowed(no_resolver):
    variables = {
        "input": {
            "branch": {"repositoryNameWithOwner": "strands-agents/harness-sdk", "branchName": "main"},
            "message": {"headline": "m"},
            "expectedHeadOid": "abc",
        }
    }
    err, owner = _validate(variables, CREATE_COMMIT)
    assert err is None
    assert owner == "strands-agents/harness-sdk"


@pytest.mark.parametrize(
    "nested",
    [
        {"repositoryName": "other-org/repo"},
        {"nameWithOwner": "other-org/repo"},
        {"owner": "other-org", "name": "repo"},
        {"deeper": [{"repositoryOwner": "other-org", "repositoryName": "repo"}]},
    ],
)
def test_nested_repository_names_in_any_shape_are_checked(no_resolver, nested):
    err, _ = _validate({"input": {"x": nested}}, CREATE_COMMIT)
    assert err is not None and "other-org" in err


def test_actor_ids_are_not_repository_targets(typed_resolver):
    q = (
        "mutation($pr: ID!, $users: [ID!]) { requestReviews(input: {pullRequestId: $pr, "
        "userIds: $users}) { clientMutationId } }"
    )
    err, owner = _validate({"pr": ALLOWED_NODE, "users": ["U_kgDOBc3j4w", "MDQ6VXNlcjU4MzIzMQ=="]}, q)
    assert err is None
    assert typed_resolver == [ALLOWED_NODE]
    assert owner == "strands-agents/harness-sdk"


def test_actor_id_as_mutation_target_blocked(typed_resolver):
    err, _ = _validate(
        {}, 'mutation { addComment(input: {subjectId: "U_kgDOBc3j4w", body: "x"}) { clientMutationId } }'
    )
    assert err is not None and "U_kgDOBc3j4w" in err


# --- Review follow-ups: precise id collection, repeated names, odd targets, URLs, throttle target ---

ADD_COMMENT_VARS = (
    "mutation($id: ID!, $body: String!) { addComment(input: {subjectId: $id, body: $body}) "
    "{ clientMutationId } }"
)


@pytest.mark.parametrize(
    "text", ["Update_README", "Needs_Review", "Fix_bug", "TODO_fix_this_later", "CHANGES_REQUESTED", "x_y"]
)
def test_free_text_shaped_like_type_underscore_is_not_a_target(typed_resolver, text):
    """Bodies, titles and headlines are not ids; only GitHub-shaped ids count outside ID keys."""
    err, owner = _validate({"id": ALLOWED_NODE, "body": text}, ADD_COMMENT_VARS)
    assert err is None
    assert typed_resolver == [ALLOWED_NODE]
    assert owner == "strands-agents/harness-sdk"


def test_commit_headline_branch_and_path_text_not_overblocked(no_resolver):
    err, owner = _validate(
        {
            "input": {
                "branch": {"repositoryNameWithOwner": "strands-agents/harness-sdk", "branchName": "Fix_bug"},
                "message": {"headline": "Update_README", "body": "Docs_only change"},
                "expectedHeadOid": "0123456789abcdef0123456789abcdef01234567",
                "fileChanges": {"additions": [{"path": "Docs_dir/README.md", "contents": "SGVsbG8gd29ybGQ="}]},
            }
        },
        CREATE_COMMIT,
    )
    assert err is None
    assert owner == "strands-agents/harness-sdk"


@pytest.mark.parametrize("text", [EXTERNAL_NODE, EXTERNAL_LEGACY_NODE])
def test_github_shaped_id_in_free_text_is_still_checked(typed_resolver, text):
    err, _ = _validate({"id": ALLOWED_NODE, "body": text}, ADD_COMMENT_VARS)
    assert err is not None


def test_every_string_under_an_id_key_is_a_target(typed_resolver):
    """An ID-typed key needs no recognizable id shape to be checked."""
    q = (
        "mutation($id: ID!, $labels: [ID!]!) { addLabelsToLabelable(input: {labelableId: $id, "
        "labelIds: $labels}) { clientMutationId } }"
    )
    err, _ = _validate({"id": ALLOWED_NODE, "labels": ["opaque-label-id"]}, q)
    assert err is not None and "opaque-label-id" in err


def test_in_reply_to_is_an_id_key(typed_resolver):
    q = (
        "mutation($pr: ID!, $reply: ID!) { addPullRequestReviewComment(input: {pullRequestId: $pr, "
        'inReplyTo: $reply, body: "ok"}) { clientMutationId } }'
    )
    err, _ = _validate({"pr": ALLOWED_NODE, "reply": "opaque-comment-id"}, q)
    assert err is not None and "opaque-comment-id" in err


def test_client_mutation_id_is_not_a_target(typed_resolver):
    q = (
        "mutation($id: ID!) { addComment(input: {subjectId: $id, body: \"x\", "
        'clientMutationId: "strandly-run-1"}) { clientMutationId } }'
    )
    err, _ = _validate({"id": ALLOWED_NODE}, q)
    assert err is None


@pytest.mark.parametrize(
    "query",
    [
        # GraphQL forbids repeats; a dict would keep one value, maybe not the one GitHub uses.
        'mutation { addComment(input: {subjectId: "a", subjectId: "b", body: "x"}) { clientMutationId } }',
        'mutation { addComment(input: {subjectId: "a", body: "x"}, input: {subjectId: "b", body: "x"}) '
        "{ clientMutationId } }",
        'mutation($s: ID = "a", $s: ID = "b") { addComment(input: {subjectId: $s, body: "x"}) '
        "{ clientMutationId } }",
    ],
)
def test_repeated_argument_field_or_variable_names_blocked(no_resolver, query):
    err, _ = _validate({}, query)
    assert err is not None and "more than once" in err


def test_non_string_target_value_blocked(typed_resolver):
    """GitHub coerces an Int to ID, so a numeric target cannot be skipped as absent."""
    q = (
        f'mutation {{ submitPullRequestReview(input: {{pullRequestReviewId: "{ALLOWED_NODE}", '
        "pullRequestId: 12345, event: COMMENT}) { clientMutationId } }"
    )
    err, _ = _validate({}, q)
    assert err is not None and "input.pullRequestId" in err


SUB_ISSUE = (
    "mutation($id: ID!, $url: String!) { addSubIssue(input: {issueId: $id, subIssueUrl: $url}) "
    "{ clientMutationId } }"
)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/evil-external-org/code/issues/1",
        "https://GitHub.com/evil-external-org/code.git",
        "https://github.com/strands-agents",  # names no repository
    ],
)
def test_sub_issue_url_outside_allow_list_blocked(typed_resolver, url):
    err, _ = _validate({"id": ALLOWED_NODE, "url": url}, SUB_ISSUE)
    assert err is not None and "not in the allowed list" in err


def test_sub_issue_url_inside_allow_list_allowed(typed_resolver):
    err, _ = _validate({"id": ALLOWED_NODE, "url": "https://github.com/strands-agents/tools/issues/7"}, SUB_ISSUE)
    assert err is None


@pytest.mark.parametrize(
    "name", ["aws/approved-x/../evil", "aws/approved-x/sub", "aws/approved-x ", "aws /approved-x"]
)
def test_repository_name_with_owner_must_be_exactly_owner_slash_repo(no_resolver, name):
    """A glob's ``*`` matches ``/``, so a malformed name must not ride an ``owner/prefix-*`` entry."""
    err, _ = validate_owner(
        {"input": {"branch": {"repositoryNameWithOwner": name, "branchName": "main"}}},
        allowed_owners={"aws/approved-*"},
        is_mutative=True,
        strict=True,
        token="t",
        query=CREATE_COMMIT,
    )
    assert err is not None


def test_reported_target_prefers_one_the_throttle_counts(monkeypatch):
    """A bare-owner (exempt) target in one root field cannot hide a glob-only one in another."""
    monkeypatch.setattr(guardrails, "resolve_node_owner", lambda nid, tok: "strands-agents/harness-sdk")
    q = (
        f'mutation {{ a: addComment(input: {{subjectId: "{ALLOWED_NODE}", body: "x"}}) {{ clientMutationId }} '
        "b: createCommitOnBranch(input: {branch: {repositoryNameWithOwner: \"aws/approved-x\", "
        'branchName: "main"}, message: {headline: "h"}, expectedHeadOid: "abc"}) { commit { url } } }'
    )
    err, target = validate_owner(
        {}, allowed_owners={"strands-agents", "aws/approved-*"}, is_mutative=True, strict=True, token="t", query=q
    )
    assert err is None
    assert target == "aws/approved-x"
