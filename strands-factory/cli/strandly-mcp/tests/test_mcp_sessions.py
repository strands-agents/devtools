import pytest
from _home_isolation import _isolated_home  # noqa: F401  (autouse: tmp HOME)

from strandly_mcp import sessions

DEFAULT = "mcp-default"


def test_canonical_scheme():
    assert sessions.canonical_session_id("strands-agents/sdk-python", "pr", 1951) == (
        "gh-strands-agents-sdk-python-b00b0499-pr-1951"
    )
    assert sessions.canonical_session_id("/o/r/", "issue", "7") == "gh-o-r-96458419-issue-7"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_blank_resolves_to_the_default_thread(value):
    assert sessions.resolve_session(value, DEFAULT) == DEFAULT


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/strands-agents/sdk-python/pull/1951", "gh-strands-agents-sdk-python-b00b0499-pr-1951"),
        ("https://github.com/o/r/issues/7", "gh-o-r-96458419-issue-7"),
        ("https://github.com/o/r/discussions/12", "gh-o-r-96458419-disc-12"),
        ("https://github.com/o/r/pull/1951/", "gh-o-r-96458419-pr-1951"),
        ("https://github.com/o/r/pull/1951/files", "gh-o-r-96458419-pr-1951"),
        ("https://github.com/o/r/issues/7#issuecomment-123", "gh-o-r-96458419-issue-7"),
        ("https://github.com/o/r/pull/1951?diff=split#r1", "gh-o-r-96458419-pr-1951"),
        ("https://www.github.com/o/r/pull/1951", "gh-o-r-96458419-pr-1951"),
        ("http://GitHub.com/o/r/pull/1951", "gh-o-r-96458419-pr-1951"),
        ("github.com/o/r/pull/1951", "gh-o-r-96458419-pr-1951"),
        ("  https://github.com/o/r/pull/1951  ", "gh-o-r-96458419-pr-1951"),
        ("https://github.com/Strands-Agents/SDK-Python/pull/1951", "gh-strands-agents-sdk-python-b00b0499-pr-1951"),
        ("https://github.com/acme/labs-tool/issues/12", "gh-acme-labs-tool-7d6b2707-issue-12"),
        ("https://github.com/acme-labs/tool/issues/12", "gh-acme-labs-tool-65ef6050-issue-12"),
    ],
)
def test_github_item_urls_resolve_to_the_canonical_thread(url, expected):
    assert sessions.resolve_session(url, DEFAULT) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r",
        "https://github.com/o/r/commits/abc",
        "https://github.com/o/r/pull/",
        "https://github.com/o/r/pull/abc",
        "https://github.com/o/r/pull/0",
        "https://github.com/o/r/pulls/12",
        "https://github.com/o",
        "https://github.com",
    ],
)
def test_other_github_urls_are_rejected_with_the_accepted_shape(url):
    with pytest.raises(ValueError, match=r"github\.com/<owner>/<repo>/\(issues\|pull\|discussions\)/<number>"):
        sessions.resolve_session(url, DEFAULT)


@pytest.mark.parametrize(
    "value",
    ["gh-o-r-96458419-pr-1", "my-thread", "mcp-abc123", "team/design notes", "https://gitlab.com/o/r/-/issues/1"],
)
def test_anything_else_is_a_verbatim_thread_id(value):
    assert sessions.resolve_session(value, DEFAULT) == value


def test_default_session_id_is_private_and_fresh():
    a, b = sessions.default_session_id(), sessions.default_session_id()
    assert a.startswith("mcp-") and b.startswith("mcp-") and a != b
