"""Hermetic tests for the GitHub URL context injector — no live network (a fake ``graphql``).

Covers URL/fragment parsing (incl. malformed/non-GitHub), per-kind enrichment (issue/PR/discussion),
deep-linking the triggering item, multi-URL, truncation, and fail-soft on bad URL / HTTP error.
"""

from __future__ import annotations

from typing import Any

import pytest

from strandly_harness.core.config import Config
from strandly_harness.core.context import RuntimeContext
from strandly_harness.plugins.github_threads import fetch as gc

# ---------------------------------------------------------------------------
# URL + fragment parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, kind, owner, repo, number",
    [
        ("https://github.com/o/r/issues/123", "issue", "o", "r", 123),
        ("https://github.com/o/r/pull/45", "pull", "o", "r", 45),
        ("https://github.com/o/r/discussions/7", "discussion", "o", "r", 7),
        ("https://www.github.com/o/r/issues/1", "issue", "o", "r", 1),
        ("https://github.com/o/r/pull/45/files", "pull", "o", "r", 45),  # trailing path tolerated
    ],
)
def test_parse_canonical_urls(url, kind, owner, repo, number):
    ref = gc.parse_github_url(url)
    assert ref is not None
    assert (ref.kind, ref.owner, ref.repo, ref.number) == (kind, owner, repo, number)


@pytest.mark.parametrize(
    "url, frag_kind, frag_id",
    [
        ("https://github.com/o/r/pull/45#issuecomment-999", "issuecomment", 999),
        ("https://github.com/o/r/pull/45#pullrequestreview-12", "review", 12),
        ("https://github.com/o/r/pull/45#discussion_r88", "review_comment", 88),
        ("https://github.com/o/r/discussions/7#discussioncomment-5", "discussion_comment", 5),
        ("https://github.com/o/r/issues/123", None, None),  # no fragment
    ],
)
def test_parse_fragments(url, frag_kind, frag_id):
    ref = gc.parse_github_url(url)
    assert ref is not None
    assert ref.fragment_kind == frag_kind
    assert ref.fragment_id == frag_id


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "https://gitlab.com/o/r/issues/1",  # wrong host
        "https://evil-github.com/o/r/issues/1",  # look-alike host
        "https://github.com/o/r",  # too short
        "https://github.com/o/r/commits/abc",  # not a thread segment
        "https://github.com/o/r/issues/notanumber",
        "https://github.com/o/r/issues/0",  # non-positive
        None,  # non-string
    ],
)
def test_parse_rejects_bad_urls(url):
    assert gc.parse_github_url(url) is None


# ---------------------------------------------------------------------------
# Enrichment per kind (fake graphql seam)
# ---------------------------------------------------------------------------


def _wrap(field: str, node: dict[str, Any]) -> dict[str, Any]:
    return {"data": {"repository": {field: node}}}


def test_issue_enrichment_with_comments_and_linked():
    node = {
        "number": 123,
        "title": "A bug",
        "body": "the body",
        "state": "OPEN",
        "url": "https://github.com/o/r/issues/123",
        "createdAt": "2024-01-01",
        "author": {"login": "alice"},
        "comments": {
            "totalCount": 1,
            "nodes": [
                {"databaseId": 1, "author": {"login": "bob"}, "body": "a comment", "createdAt": "x"}
            ],
        },
        "timelineItems": {
            "nodes": [
                {"source": {"number": 9, "title": "fix", "state": "MERGED", "url": "u"}}
            ]
        },
    }

    def fake_graphql(query, variables, token):
        assert variables == {"owner": "o", "name": "r", "number": 123}
        return _wrap("issue", node)

    out = gc.build_github_context(
        "https://github.com/o/r/issues/123", token="t", graphql=fake_graphql
    )
    assert "🎫 ISSUE o/r#123: A bug" in out
    assert "the body" in out
    assert "Comment #1** by @bob" in out
    assert "#9: fix (MERGED)" in out


def test_pr_enrichment_full_shape():
    node = {
        "number": 45,
        "title": "A PR",
        "body": "pr body",
        "state": "OPEN",
        "url": "u",
        "createdAt": "x",
        "author": {"login": "alice"},
        "baseRefName": "main",
        "headRefName": "feature",
        "reviews": {
            "totalCount": 1,
            "nodes": [
                {
                    "databaseId": 12,
                    "author": {"login": "rev"},
                    "state": "APPROVED",
                    "body": "lgtm",
                    "createdAt": "x",
                }
            ],
        },
        "comments": {"totalCount": 0, "nodes": []},
        "reviewThreads": {
            "totalCount": 1,
            "nodes": [
                {
                    "isResolved": False,
                    "comments": {
                        "nodes": [
                            {
                                "databaseId": 88,
                                "author": {"login": "rev"},
                                "body": "nit here",
                                "path": "src/x.py",
                                "line": 10,
                            }
                        ]
                    },
                }
            ],
        },
        "closingIssuesReferences": {
            "totalCount": 1,
            "nodes": [{"number": 5, "title": "the issue", "state": "OPEN", "url": "u"}],
        },
    }

    out = gc.build_github_context(
        "https://github.com/o/r/pull/45", token="t", graphql=lambda *a: _wrap("pullRequest", node)
    )
    assert "🔀 PULL REQUEST o/r#45" in out
    assert "feature → main" in out
    assert "Review #1** by @rev — APPROVED" in out
    assert "Thread #1** [🔴 Unresolved] by @rev on `src/x.py:10`" in out
    assert "Fixes #5: the issue (OPEN)" in out


def test_discussion_enrichment_not_treated_as_issue():
    node = {
        "number": 7,
        "title": "A discussion",
        "body": "disc body",
        "url": "u",
        "createdAt": "x",
        "author": {"login": "alice"},
        "comments": {
            "totalCount": 1,
            "nodes": [
                {
                    "databaseId": 5,
                    "author": {"login": "bob"},
                    "body": "a reply",
                    "createdAt": "x",
                    "replies": {
                        "nodes": [
                            {"databaseId": 6, "author": {"login": "carol"}, "body": "nested"}
                        ]
                    },
                }
            ],
        },
    }
    captured = {}

    def fake_graphql(query, variables, token):
        captured["query"] = query
        return _wrap("discussion", node)

    out = gc.build_github_context(
        "https://github.com/o/r/discussions/7", token="t", graphql=fake_graphql
    )
    # Routed to the discussion query, not the issue query.
    assert "discussion(number" in captured["query"]
    assert "💭 DISCUSSION o/r#7" in out
    assert "Reply #1** by @bob" in out
    assert "↳ @carol: nested" in out


# ---------------------------------------------------------------------------
# Deep-linking the triggering item
# ---------------------------------------------------------------------------


def test_deeplink_marks_triggering_comment():
    node = {
        "number": 123,
        "title": "t",
        "body": "b",
        "state": "OPEN",
        "url": "u",
        "createdAt": "x",
        "author": {"login": "a"},
        "comments": {
            "totalCount": 2,
            "nodes": [
                {"databaseId": 1, "author": {"login": "x"}, "body": "first"},
                {"databaseId": 999, "author": {"login": "y"}, "body": "the trigger"},
            ],
        },
        "timelineItems": {"nodes": []},
    }
    out = gc.build_github_context(
        "https://github.com/o/r/issues/123#issuecomment-999",
        token="t",
        graphql=lambda *a: _wrap("issue", node),
    )
    # Only the matching comment gets the marker, and a trailing trigger note is added.
    assert "👉 **TRIGGERING ITEM** — Comment #2** by @y" in out
    assert "first" in out
    assert out.count("TRIGGERING ITEM") == 1
    assert "triggered by a specific issuecomment (id 999)" in out


def test_deeplink_note_suppressed_when_fragment_matches_nothing():
    # Fragment pins comment id 999, but no rendered node has that databaseId (e.g. it's beyond the
    # fetched page) — no 👉 marker is placed, so the trailing "see 👉 above" note must be suppressed.
    node = {
        "number": 123,
        "title": "t",
        "body": "b",
        "state": "OPEN",
        "url": "u",
        "createdAt": "x",
        "author": {"login": "a"},
        "comments": {
            "totalCount": 1,
            "nodes": [{"databaseId": 1, "author": {"login": "x"}, "body": "first"}],
        },
        "timelineItems": {"nodes": []},
    }
    out = gc.build_github_context(
        "https://github.com/o/r/issues/123#issuecomment-999",
        token="t",
        graphql=lambda *a: _wrap("issue", node),
    )
    assert "TRIGGERING ITEM" not in out
    assert "triggered by a specific" not in out


# ---------------------------------------------------------------------------
# Multi-URL, truncation, fail-soft
# ---------------------------------------------------------------------------


def test_multi_url_joins_blocks():
    issue = {"number": 1, "title": "I", "body": "b", "state": "OPEN", "author": {"login": "a"},
             "comments": {"nodes": []}, "timelineItems": {"nodes": []}}
    disc = {"number": 2, "title": "D", "body": "b", "author": {"login": "a"},
            "comments": {"nodes": []}}

    def fake_graphql(query, variables, token):
        if "discussion(number" in query:
            return _wrap("discussion", disc)
        return _wrap("issue", issue)

    out = gc.build_github_context(
        ["https://github.com/o/r/issues/1", "https://github.com/o/r/discussions/2"],
        token="t",
        graphql=fake_graphql,
    )
    assert "🎫 ISSUE o/r#1" in out
    assert "💭 DISCUSSION o/r#2" in out
    assert "\n\n---\n\n" in out  # blocks separated


def test_truncation_caps_long_body():
    long_body = "x" * (gc.MAX_BODY_CHARS + 500)
    node = {"number": 1, "title": "t", "body": long_body, "state": "OPEN", "author": {"login": "a"},
            "comments": {"nodes": []}, "timelineItems": {"nodes": []}}
    out = gc.build_github_context(
        "https://github.com/o/r/issues/1", token="t", graphql=lambda *a: _wrap("issue", node)
    )
    assert "[truncated 500 chars]" in out
    assert long_body not in out


def test_failsoft_on_bad_url():
    out = gc.build_github_context(
        "https://gitlab.com/o/r/issues/1", token="t", graphql=lambda *a: pytest.fail("no call")
    )
    assert "Not an enrichable GitHub URL" in out


def test_failsoft_on_http_error():
    def boom(query, variables, token):
        raise RuntimeError("HTTP 503")

    out = gc.build_github_context(
        "https://github.com/o/r/issues/1", token="t", graphql=boom
    )
    assert "GitHub context unavailable for o/r#1" in out
    assert "RuntimeError" in out


def test_failsoft_on_graphql_errors():
    out = gc.build_github_context(
        "https://github.com/o/r/issues/1",
        token="t",
        graphql=lambda *a: {"errors": [{"message": "nope"}]},
    )
    assert "GraphQL errors" in out


def test_failsoft_on_missing_node():
    out = gc.build_github_context(
        "https://github.com/o/r/issues/1",
        token="t",
        graphql=lambda *a: {"data": {"repository": {"issue": None}}},
    )
    assert "not found" in out


# ---------------------------------------------------------------------------
# Wiring: enrichment is the GitHubContextInjector plugin, NOT a tool.
# `use_github` already covers any URL the agent wants to fetch on demand, so
# there is deliberately no `inject_github_context` tool (issue #346 owner feedback).
# ---------------------------------------------------------------------------


def test_no_inject_github_context_tool_factory():
    # The on-demand tool was removed; only the pure core remains for the plugin to reuse.
    assert not hasattr(gc, "make_inject_github_context")
    assert hasattr(gc, "build_github_context")


@pytest.mark.asyncio
async def test_no_inject_github_context_tool_when_github_enabled(fake_model, tmp_path):
    from strandly_harness.core.agent import build_agent

    agent = await build_agent(
        Config(values={"STRANDLY_GITHUB_TOKEN": "ghp_x"}),
        RuntimeContext(cwd=str(tmp_path)),
        model=fake_model,
    )
    names = set(agent.tool_names)
    # No dedicated context-fetch tool — use_github is the on-demand GitHub surface.
    assert "inject_github_context" not in names
    assert "use_github" in names


# ---------------------------------------------------------------------------
# Token-optional: anonymous REST fallback (no token) — public issues/PRs.
# Discussions are GraphQL-only → a short "needs a token" note. (issue #346)
# ---------------------------------------------------------------------------


class _FakeREST:
    """Fake ``github._rest_get(path, token)`` — substring-routed, records calls (token asserted "")."""

    def __init__(self, routes):
        self.calls = []
        self._routes = routes

    def __call__(self, path, token):
        self.calls.append((path, token))
        for needle, value in self._routes.items():
            if needle in path:
                return value
        return {}


def test_rest_fallback_issue_no_token():
    rest = _FakeREST(
        {
            "/issues/1/comments": [
                {"id": 7, "user": {"login": "bob"}, "body": "rest comment", "created_at": "x"}
            ],
            "/issues/1": {
                "number": 1,
                "title": "Public bug",
                "body": "rest body",
                "state": "open",
                "user": {"login": "alice"},
                "html_url": "https://github.com/o/r/issues/1",
            },
        }
    )
    out = gc.build_github_context(
        "https://github.com/o/r/issues/1",
        token="",  # no token → REST path
        graphql=lambda *a: pytest.fail("graphql must not be called without a token"),
        rest=rest,
    )
    assert "🎫 ISSUE o/r#1: Public bug" in out
    assert "rest body" in out
    assert "Comment #1** by @bob" in out
    assert "Enriched anonymously via GitHub REST" in out
    # All calls anonymous.
    assert rest.calls and all(tok == "" for _, tok in rest.calls)


def test_rest_fallback_pull_no_token_with_reviews_and_review_comments():
    rest = _FakeREST(
        {
            "/pulls/45/reviews": [
                {"id": 12, "user": {"login": "rev"}, "state": "APPROVED", "body": "lgtm"},
                {"id": 99, "user": {"login": "me"}, "state": "PENDING", "body": ""},  # filtered
            ],
            "/pulls/45/comments": [
                {"id": 88, "user": {"login": "rev"}, "body": "nit", "path": "src/x.py", "line": 10}
            ],
            "/pulls/45": {
                "number": 45,
                "title": "A PR",
                "body": "pr body",
                "state": "open",
                "user": {"login": "alice"},
                "html_url": "u",
                "head": {"ref": "feature"},
                "base": {"ref": "main"},
            },
            "/issues/45/comments": [
                {"id": 5, "user": {"login": "x"}, "body": "discuss", "created_at": "x"}
            ],
        }
    )
    out = gc.build_github_context(
        "https://github.com/o/r/pull/45", token="", graphql=lambda *a: pytest.fail("no graphql"), rest=rest
    )
    assert "🔀 PULL REQUEST o/r#45: A PR" in out
    assert "feature → main" in out
    assert "Review #1** by @rev — APPROVED" in out
    assert "PENDING" not in out  # the viewer's empty PENDING review is filtered out
    assert "Review comment #1** by @rev on `src/x.py:10`" in out
    assert "Comment #1** by @x" in out
    assert "Enriched anonymously via GitHub REST" in out


def test_rest_fallback_discussion_no_token_needs_token_note():
    out = gc.build_github_context(
        "https://github.com/o/r/discussions/7",
        token="",
        graphql=lambda *a: pytest.fail("no graphql"),
        rest=lambda *a: pytest.fail("discussions are GraphQL-only — REST must not be called"),
    )
    assert "GitHub context unavailable for o/r#7" in out
    assert "configure a GitHub token to enrich discussions" in out


def test_rest_fallback_deeplinks_triggering_comment():
    rest = _FakeREST(
        {
            "/issues/1/comments": [
                {"id": 7, "user": {"login": "bob"}, "body": "first", "created_at": "x"},
                {"id": 999, "user": {"login": "y"}, "body": "the trigger", "created_at": "x"},
            ],
            "/issues/1": {
                "number": 1, "title": "t", "body": "b", "state": "open",
                "user": {"login": "alice"}, "html_url": "u",
            },
        }
    )
    out = gc.build_github_context(
        "https://github.com/o/r/issues/1#issuecomment-999", token="", graphql=lambda *a: None, rest=rest
    )
    assert "👉 **TRIGGERING ITEM** — Comment #2** by @y" in out
    assert out.count("TRIGGERING ITEM") == 1
    assert "triggered by a specific issuecomment (id 999)" in out


def test_rest_fallback_not_found_note():
    # An anonymous REST 404 (private/missing) → fail-soft note, never raises.
    def boom(path, token):
        raise RuntimeError("HTTP Error 404: Not Found")

    out = gc.build_github_context(
        "https://github.com/o/r/issues/1", token="", graphql=lambda *a: None, rest=boom
    )
    assert "GitHub context unavailable for o/r#1" in out
    assert "RuntimeError" in out


def test_rest_fallback_missing_node_note():
    # REST returns no usable object (e.g. {} for a private repo) → not-found note.
    out = gc.build_github_context(
        "https://github.com/o/r/issues/1",
        token="",
        graphql=lambda *a: None,
        rest=lambda path, token: {},
    )
    assert "not found via anonymous REST" in out


def test_token_path_ignores_rest_seam():
    # With a token, enrichment goes through GraphQL; the REST seam must not be touched.
    node = {"number": 1, "title": "t", "body": "b", "state": "OPEN", "author": {"login": "a"},
            "comments": {"nodes": []}, "timelineItems": {"nodes": []}}
    out = gc.build_github_context(
        "https://github.com/o/r/issues/1",
        token="ghp_x",
        graphql=lambda *a: _wrap("issue", node),
        rest=lambda *a: pytest.fail("rest must not be called when a token is present"),
    )
    assert "🎫 ISSUE o/r#1" in out
    assert "Enriched anonymously" not in out
