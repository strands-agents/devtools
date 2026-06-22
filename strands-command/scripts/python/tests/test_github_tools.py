"""Tests for github_tools PR helpers."""
from unittest.mock import patch

from .. import github_tools


def test_get_pr_files_returns_full_diff_untruncated():
    """The full patch is returned, with no per-file line cap.

    Guards the original bug: get_pr_files used to truncate each file's patch to
    50 lines, hiding the tail. Oversized results are now managed by the agent's
    ContextOffloader plugin rather than truncated here.
    """
    big_patch = "\n".join(f"+line {i}" for i in range(120))
    files = [{
        "filename": "a.py", "status": "modified",
        "additions": 120, "deletions": 0, "changes": 120, "patch": big_patch,
    }]

    with patch.object(github_tools, "_github_request", lambda *a, **k: files):
        out = github_tools.get_pr_files(1, repo="o/r")

    assert "truncated" not in out
    assert "line 0" in out
    assert "line 119" in out  # tail is present, not cut at 50


def test_get_pr_files_inlines_every_changed_file():
    """All changed files are inlined in full, in API order."""
    files = [
        {"filename": "small.py", "status": "modified", "additions": 5,
         "deletions": 0, "changes": 5, "patch": "\n".join(f"+s{i}" for i in range(5))},
        {"filename": "big.py", "status": "modified", "additions": 5000,
         "deletions": 0, "changes": 5000, "patch": "\n".join(f"+b{i}" for i in range(5000))},
    ]

    with patch.object(github_tools, "_github_request", lambda *a, **k: files):
        out = github_tools.get_pr_files(1, repo="o/r")

    assert "small.py" in out and "+s4" in out
    assert "big.py" in out and "+b4999" in out  # full tail of the large file present
    assert "omitted" not in out.lower()


def test_get_pr_files_handles_binary_file():
    """Files without a patch (binary) are reported, not crashed on."""
    files = [{
        "filename": "logo.png", "status": "added",
        "additions": 0, "deletions": 0, "changes": 0,
    }]

    with patch.object(github_tools, "_github_request", lambda *a, **k: files):
        out = github_tools.get_pr_files(1, repo="o/r")

    assert "logo.png" in out
    assert "Binary file or no diff available" in out


def test_get_pr_files_returns_error_string_on_request_failure():
    """A request error is surfaced as-is rather than formatted as a file list."""
    with patch.object(github_tools, "_github_request", lambda *a, **k: "Error: boom"):
        out = github_tools.get_pr_files(1, repo="o/r")

    assert out == "Error: boom"
