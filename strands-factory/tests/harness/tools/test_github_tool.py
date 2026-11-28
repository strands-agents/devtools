"""GitHub tool behavior and security regression tests."""

from __future__ import annotations

import io as _io
import urllib.error as _urlerror

import pytest

from strandly.core.config import GitHubSettings
from strandly.tools import github as gh
from strandly.tools import github_guardrails as guardrails
from strandly.tools.github import (
    make_use_github,
)

_REST_ALLOWED = ("strands-agents", "aws/bedrock-agentcore-*")


def test_parse_rest_path_repo_and_account_shapes():
    assert guardrails.parse_rest_path("/repos/o/r/pulls/1/files") == ("o/r", "pulls")
    assert guardrails.parse_rest_path("/repos/o/r/compare/a...b") == ("o/r", "compare")
    assert guardrails.parse_rest_path("/search/code?q=foo") == (None, "search")
    # Bare repo metadata has no family segment; it must still parse (not 500) and stay repo-scoped.
    assert guardrails.parse_rest_path("/repos/o/r")[0] == "o/r"


@pytest.mark.parametrize(
    "bad",
    [
        "repos/o/r/pulls",  # no leading slash
        "https://evil.example/repos/strands-agents/x",  # host swap
        "/repos/strands-agents/x/../../evil",  # traversal
        "/repos/o",  # owner without repo
        "",  # empty
        "/repos/o/r\\pulls",  # backslash
    ],
)
def test_parse_rest_path_rejects_malformed(bad):
    with pytest.raises(ValueError):
        guardrails.parse_rest_path(bad)


def test_rest_owner_allow_list_enforced_from_the_path():
    # REST carries its target in the path, not a node id — so it needs its own owner extraction.
    allowed = set(_REST_ALLOWED)
    err, target = guardrails.validate_rest_path(
        "/repos/strands-agents/harness-sdk/pulls/1/files", "GET", allowed_owners=allowed
    )
    assert err is None and target == "strands-agents/harness-sdk"

    err, _ = guardrails.validate_rest_path("/repos/evilcorp/x/pulls/1/files", "GET", allowed_owners=allowed)
    assert err is not None and "not in the allowed list" in err


def test_rest_repo_glob_entries_are_honoured():
    # Same matcher as GraphQL: a repo glob grants only matching repos of that owner.
    allowed = set(_REST_ALLOWED)
    ok, _ = guardrails.validate_rest_path(
        "/repos/aws/bedrock-agentcore-sdk-python/compare/a...b", "GET", allowed_owners=allowed
    )
    assert ok is None
    blocked, _ = guardrails.validate_rest_path(
        "/repos/aws/some-other-repo/contents/f", "GET", allowed_owners=allowed
    )
    assert blocked is not None


def test_rest_unknown_resource_family_blocked():
    err, _ = guardrails.validate_rest_path(
        "/repos/strands-agents/x/secrets", "GET", allowed_owners=set(_REST_ALLOWED)
    )
    assert err is not None and "not in the allowed set" in err


def test_rest_account_scoped_writes_confined_to_own_account():
    # An account-scoped path has NO owner to verify, so only families that can act on nothing but
    # the token's own account may be written. Anything else account-scoped stays read-only.
    allowed = set(_REST_ALLOWED)
    for path in ("/gists", "/user/repos"):
        err, target = guardrails.validate_rest_path(path, "POST", allowed_owners=allowed)
        assert err is None and target is None, path
    err, _ = guardrails.validate_rest_path("/search/code", "POST", allowed_owners=allowed)
    assert err is not None and "no repository owner to verify" in err


def test_rest_unsupported_method_blocked():
    err, _ = guardrails.validate_rest_path(
        "/repos/strands-agents/x/pulls", "TRACE", allowed_owners=set(_REST_ALLOWED)
    )
    assert err is not None and "not supported" in err


def test_rest_empty_allow_list_disables_the_owner_check():
    # Consistent with validate_owner: no allow-list configured => owner guardrail OFF.
    err, target = guardrails.validate_rest_path(
        "/repos/anyone/anything/pulls/1/files", "GET", allowed_owners=set()
    )
    assert err is None and target == "anyone/anything"


def test_rest_branch_blocks_before_any_network(monkeypatch):
    def _no_network(*a, **k):
        raise AssertionError("network reached on a blocked REST call")

    monkeypatch.setattr(gh, "_rest_call", _no_network)
    settings = GitHubSettings(
        token="ghp_x", allowed_owners=_REST_ALLOWED, internal_owners=_REST_ALLOWED
    )
    out = gh._rest_branch(
        gh=settings,
        allowed_owners=set(_REST_ALLOWED),
        path="/repos/evilcorp/x/pulls/1/files",
        method="GET",
        body=None,
        accept="",
        use_pat_token=False,
    )
    assert out["status"] == "error"
    assert "not in the allowed list" in out["content"][0]["text"]


def test_rest_branch_returns_diff_text_verbatim(monkeypatch):
    # A .diff response is not JSON — the text body IS the payload and must survive unmangled.
    diff = "diff --git a/x.py b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
    monkeypatch.setattr(gh, "_rest_call", lambda *a, **k: diff)
    settings = GitHubSettings(
        token="ghp_x", allowed_owners=_REST_ALLOWED, internal_owners=_REST_ALLOWED
    )
    out = gh._rest_branch(
        gh=settings,
        allowed_owners=set(_REST_ALLOWED),
        path="/repos/strands-agents/harness-sdk/pulls/1",
        method="GET",
        body=None,
        accept="application/vnd.github.v3.diff",
        use_pat_token=False,
    )
    assert out["status"] == "success"
    assert out["content"][0]["text"] == diff


def test_rest_branch_rejects_arbitrary_accept_header(monkeypatch):
    monkeypatch.setattr(
        gh, "_rest_call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("reached network"))
    )
    settings = GitHubSettings(
        token="ghp_x", allowed_owners=_REST_ALLOWED, internal_owners=_REST_ALLOWED
    )
    out = gh._rest_branch(
        gh=settings,
        allowed_owners=set(_REST_ALLOWED),
        path="/repos/strands-agents/x/pulls/1",
        method="GET",
        body=None,
        accept="application/x-smuggled",
        use_pat_token=False,
    )
    assert out["status"] == "error" and "Accept" in out["content"][0]["text"]


def test_rest_branch_requires_a_token(monkeypatch):
    # Must fail closed BEFORE any network call. The env is scrubbed because _get_token falls back to
    # the ambient env vars (a developer machine has GITHUB_TOKEN set, which would otherwise let this
    # test reach the real API).
    for name in ("STRANDLY_GITHUB_TOKEN", "GITHUB_TOKEN", "PAT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        gh, "_rest_call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("reached network"))
    )
    out = gh._rest_branch(
        gh=GitHubSettings(),  # token=None
        allowed_owners=set(),
        path="/repos/o/r/pulls/1/files",
        method="GET",
        body=None,
        accept="",
        use_pat_token=False,
    )
    assert out["status"] == "error" and "No GitHub token" in out["content"][0]["text"]


def test_rest_path_routes_away_from_graphql(monkeypatch):
    # Setting rest_path must take the REST branch and never touch _graphql.
    def _no_graphql(*a, **k):
        raise AssertionError("GraphQL reached on a REST call")

    monkeypatch.setattr(gh, "_graphql", _no_graphql)
    monkeypatch.setattr(gh, "_rest_call", lambda *a, **k: {"ok": True})
    tool = make_use_github(
        GitHubSettings(token="ghp_x", allowed_owners=_REST_ALLOWED, internal_owners=_REST_ALLOWED)
    )
    out = tool(
        query_type="query",
        query="",
        label="pr files",
        rest_path="/repos/strands-agents/harness-sdk/pulls/1/files",
    )
    assert out["status"] == "success" and "ok" in out["content"][0]["text"]


def test_rest_branch_surfaces_http_error_body(monkeypatch):
    # A raised HTTPError must reach the caller WITH GitHub's response body, not a canned string.
    def _boom(*a, **k):
        fp = _io.BytesIO(b'{"message": "organization requires 2FA"}')
        raise _urlerror.HTTPError("https://api.github.com/x", 403, "Forbidden", {}, fp)

    monkeypatch.setattr(gh, "_rest_call", _boom)
    settings = GitHubSettings(
        token="ghp_x", allowed_owners=_REST_ALLOWED, internal_owners=_REST_ALLOWED
    )
    out = gh._rest_branch(
        gh=settings,
        allowed_owners=set(_REST_ALLOWED),
        path="/user/repository_invitations/1",
        method="PATCH",
        body=None,
        accept="",
        use_pat_token=False,
    )
    assert out["status"] == "error"
    assert "organization requires 2FA" in out["content"][0]["text"]


@pytest.mark.parametrize(
    "settings",
    [
        GitHubSettings(),
        GitHubSettings(
            token="test-token",
            allowed_owners=("strands-agents", "aws/bedrock-agentcore-*"),
            internal_owners=("strands-agents",),
            throttle_enabled=True,
            throttle_limit=7,
        ),
    ],
)
def test_tool_spec_matches_baseline(settings):
    import json
    from pathlib import Path

    baseline = Path(__file__).parents[2] / "fixtures" / "github_tool_spec.json"
    assert make_use_github(settings).tool_spec == json.loads(baseline.read_text())


def test_public_helper_imports_remain_compatible():
    from strandly.tools import github_client as client

    for name in (
        "GITHUB_GRAPHQL_URL", "GITHUB_REST_URL", "resolve_node_owner", "resolve_self_login",
        "count_external_writes", "_graphql", "_request", "_rest_get", "_get_token",
    ):
        assert getattr(gh, name) is getattr(client, name)
    for name in (
        "NODE_ID_PATTERN", "REPO_SCOPED_PREFIXES", "is_mutation_query", "_target_allowed",
        "extract_owner_from_variables", "extract_target_from_variables",
        "extract_node_ids_from_variables", "extract_node_ids_from_query", "validate_owner",
        "enforce_throttle", "invalidate_throttle_cache", "parse_rest_path", "validate_rest_path",
    ):
        assert getattr(gh, name) is getattr(guardrails, name)


@pytest.mark.parametrize("rest", [False, True], ids=["graphql", "rest"])
@pytest.mark.parametrize("failure", ["http", "url", "unexpected"])
def test_tool_preserves_client_error_responses(monkeypatch, rest, failure):
    from strandly.tools import github_client as client

    def fail(req, token, *, timeout):
        assert req.full_url.startswith("https://api.github.com/")
        assert token == "test-token"
        assert timeout == 30
        if failure == "http":
            raise _urlerror.HTTPError(req.full_url, 403, "Forbidden", {}, _io.BytesIO(b"denied"))
        if failure == "url":
            raise _urlerror.URLError("offline")
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(client, "open_github", fail)
    tool = make_use_github(GitHubSettings(token="test-token", allowed_owners=("strands-agents",)))
    result = tool(
        query_type="query", query="query { viewer { login } }", label="error response",
        rest_path="/repos/strands-agents/sdk/issues" if rest else "",
    )
    expected = {
        "http": "HTTP 403: denied",
        "url": "Request error: offline",
        "unexpected": f"GitHub {'REST ' if rest else ''}call failed: unexpected failure",
    }
    assert result == {"status": "error", "content": [{"text": expected[failure]}]}


@pytest.mark.parametrize("rest", [False, True], ids=["graphql", "rest"])
def test_tool_dispatches_through_moved_http_client(monkeypatch, rest):
    import json

    from strandly.tools import github_client as client

    requests = []

    def respond(req, token, *, timeout):
        requests.append((req, token, timeout))
        return _io.BytesIO(b'{"ok": true}' if rest else b'{"data": {"ok": true}}')

    monkeypatch.setattr(client, "open_github", respond)
    tool = make_use_github(GitHubSettings(token="test-token", allowed_owners=("strands-agents",)))
    result = tool(
        query_type="query", query="query { viewer { login } }", label="HTTP dispatch",
        rest_path="/repos/strands-agents/sdk/issues" if rest else "",
    )
    assert result == {"status": "success", "content": [{"text": '{\n  "ok": true\n}'}]}
    assert len(requests) == 1
    request, token, timeout = requests[0]
    assert (token, timeout) == ("test-token", 30)
    assert request.get_header("User-agent") == "meta-harness-github-tool/1.0"
    assert request.get_header("Accept") == "application/vnd.github+json"
    if rest:
        assert request.method == "GET"
        assert request.full_url == "https://api.github.com/repos/strands-agents/sdk/issues"
        assert request.data is None
    else:
        assert request.method == "POST"
        assert request.full_url == "https://api.github.com/graphql"
        assert json.loads(request.data) == {"query": "query { viewer { login } }", "variables": {}}


_FORK_ALLOWED = {"example-org", "partner-org/agent-*"}


def test_parse_rest_path_admits_only_the_forks_collection():
    assert guardrails.parse_rest_path("/repos/o/r/forks") == ("o/r", "forks")
    assert guardrails.parse_rest_path("/repos/o/r/forks?per_page=5") == ("o/r", "forks")
    with pytest.raises(ValueError, match="only /repos"):
        guardrails.parse_rest_path("/repos/o/r/forks/extra")


@pytest.mark.parametrize(
    ("path", "target"),
    [("/repos/example-org/tool/forks", "example-org/tool"),
     ("/repos/partner-org/agent-kit/forks", "partner-org/agent-kit")],
)
def test_rest_fork_of_an_allowed_repo_passes(path, target):
    for body in (None, {}, {"name": "tool-fork", "default_branch_only": True}):
        err, got = guardrails.validate_rest_path(
            path, "POST", allowed_owners=_FORK_ALLOWED, body=body
        )
        assert err is None and got == target


def test_rest_fork_listing_is_a_read():
    err, target = guardrails.validate_rest_path(
        "/repos/example-org/tool/forks?sort=newest", "GET", allowed_owners=_FORK_ALLOWED
    )
    assert err is None and target == "example-org/tool"


@pytest.mark.parametrize("path", ["/repos/other-org/tool/forks", "/repos/partner-org/x/forks"])
def test_rest_fork_of_a_disallowed_repo_blocked(path):
    err, _ = guardrails.validate_rest_path(path, "POST", allowed_owners=_FORK_ALLOWED)
    assert err is not None and "not in the allowed list" in err


def test_rest_fork_without_a_policy_blocked():
    err, _ = guardrails.validate_rest_path(
        "/repos/example-org/tool/forks", "POST", allowed_owners=set()
    )
    assert err is not None and "no repository access policy" in err


@pytest.mark.parametrize("verb", ["PUT", "PATCH", "DELETE"])
def test_rest_fork_other_verbs_blocked(verb):
    err, _ = guardrails.validate_rest_path(
        "/repos/example-org/tool/forks", verb, allowed_owners=_FORK_ALLOWED
    )
    assert err is not None and "only GET and POST" in err


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_rest_fork_sub_paths_blocked(method):
    err, _ = guardrails.validate_rest_path(
        "/repos/example-org/tool/forks/anything", method, allowed_owners=_FORK_ALLOWED
    )
    assert err is not None and "only /repos" in err


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"organization": "other-org"}, "own account"),
        ({"organization": "example-org", "name": "x"}, "own account"),
        ({"owner": "other-org"}, "not allowed"),
        ({"name": "bad/name"}, "not a valid repository name"),
        ({"name": ".."}, "not a valid repository name"),
        ({"name": ""}, "not a valid repository name"),
        ({"name": 7}, "not a valid repository name"),
        ({"default_branch_only": "yes"}, "true or false"),
    ],
)
def test_rest_fork_body_confined_to_own_account(body, reason):
    err, _ = guardrails.validate_rest_path(
        "/repos/example-org/tool/forks", "POST", allowed_owners=_FORK_ALLOWED, body=body
    )
    assert err is not None and reason in err


def test_rest_fork_options_in_a_query_string_blocked():
    err, _ = guardrails.validate_rest_path(
        "/repos/example-org/tool/forks?organization=other-org", "POST",
        allowed_owners=_FORK_ALLOWED,
    )
    assert err is not None and "not a query string" in err


def _fork_settings(**overrides):
    return GitHubSettings(
        token="ghp_x",
        allowed_owners=tuple(sorted(_FORK_ALLOWED)),
        internal_owners=("example-org",),
        throttle_enabled=True,
        throttle_limit=3,
        **overrides,
    )


def _fork(path, body=None):
    return gh._rest_branch(
        gh=_fork_settings(),
        allowed_owners=set(_FORK_ALLOWED),
        path=path,
        method="POST",
        body=body,
        accept="",
        use_pat_token=False,
    )


def test_rest_fork_with_organization_blocked_before_throttle_or_network(monkeypatch):
    def _never(*a, **k):
        raise AssertionError("a refused fork reached the throttle or the network")

    monkeypatch.setattr(gh, "_rest_call", _never)
    monkeypatch.setattr(guardrails, "count_external_writes", _never)
    guardrails.invalidate_throttle_cache()
    out = _fork("/repos/partner-org/agent-kit/forks", {"organization": "other-org"})
    assert out["status"] == "error" and "own account" in out["content"][0]["text"]


def test_rest_fork_of_an_external_repo_is_throttled(monkeypatch):
    def _no_network(*a, **k):
        raise AssertionError("network reached past an exhausted throttle")

    monkeypatch.setattr(gh, "_rest_call", _no_network)
    monkeypatch.setattr(guardrails, "count_external_writes", lambda **k: 3)
    guardrails.invalidate_throttle_cache()
    try:
        out = _fork("/repos/partner-org/agent-kit/forks")
    finally:
        guardrails.invalidate_throttle_cache()
    assert out["status"] == "error" and "SAFETY GUARDRAIL" in out["content"][0]["text"]


def test_rest_fork_goes_out_with_its_checked_body(monkeypatch):
    sent = {}

    def _call(verb, path, token, *, body=None, accept=""):
        sent.update(verb=verb, path=path, body=body)
        return {"full_name": "agent-bot/agent-kit"}

    monkeypatch.setattr(gh, "_rest_call", _call)
    monkeypatch.setattr(guardrails, "count_external_writes", lambda **k: 0)
    guardrails.invalidate_throttle_cache()
    try:
        out = _fork("/repos/partner-org/agent-kit/forks", {"default_branch_only": True})
    finally:
        guardrails.invalidate_throttle_cache()
    assert out["status"] == "success"
    assert sent == {
        "verb": "POST",
        "path": "/repos/partner-org/agent-kit/forks",
        "body": {"default_branch_only": True},
    }
