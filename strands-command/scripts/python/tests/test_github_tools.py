"""Tests for github_tools PR helpers."""
from unittest.mock import patch

from .. import github_tools
from ..github_tools import MIN_PARTIAL_HEAD_CHARS, TOTAL_DIFF_BUDGET_CHARS


def test_get_pr_files_inlines_full_diff_under_budget():
    """A large patch (well over the old 50-line cap) is inlined in full."""
    big_patch = "\n".join(f"+line {i}" for i in range(120))
    files = [{
        "filename": "a.py", "status": "modified",
        "additions": 120, "deletions": 0, "changes": 120, "patch": big_patch,
    }]

    with patch.object(github_tools, "_github_request", lambda *a, **k: files):
        out = github_tools.get_pr_files(1, repo="o/r")

    assert "truncated" not in out
    assert "line 0" in out
    assert "line 119" in out  # tail of the diff is present, not cut at 50


def test_get_pr_files_inlines_head_of_over_budget_file():
    """A single over-budget file shows a head slice and is listed for on-demand fetch."""
    huge = "\n".join(f"+line{i}" for i in range(50000))
    files = [{
        "filename": "big.py", "status": "modified",
        "additions": 50000, "deletions": 0, "changes": 50000, "patch": huge,
    }]

    with patch.object(github_tools, "_github_request", lambda *a, **k: files):
        out = github_tools.get_pr_files(1, repo="o/r")

    assert "Diff (head only):" in out
    assert "+line0" in out  # head content present
    assert "more chars omitted" in out
    assert "partially shown" in out  # noted in overflow listing
    assert "fetch" in out.lower()  # on-demand fetch instruction present


def test_get_pr_files_mixed_inline_and_overflow():
    """Earlier files are inlined in full while a later large file overflows."""
    small = "\n".join(f"+s{i}" for i in range(5))
    huge = "\n".join(f"+h{i}" for i in range(50000))
    files = [
        {"filename": "small.py", "status": "modified", "additions": 5,
         "deletions": 0, "changes": 5, "patch": small},
        {"filename": "huge.py", "status": "modified", "additions": 50000,
         "deletions": 0, "changes": 50000, "patch": huge},
    ]

    with patch.object(github_tools, "_github_request", lambda *a, **k: files):
        out = github_tools.get_pr_files(1, repo="o/r")

    assert "+s4" in out  # small file fully inlined
    assert "huge.py" in out
    assert "huge.py" in out.split("Some diffs were omitted")[1]  # listed in overflow


def test_get_pr_files_defers_whole_file_when_budget_nearly_exhausted():
    """A later file is fully deferred (not partially shown) once <MIN_PARTIAL_HEAD_CHARS remains."""
    # First file ~99,500 chars: fully inlined, leaving remaining < 2,000.
    first = "x" * 99_500
    # Second file larger than the remaining budget, but remaining is below
    # MIN_PARTIAL_HEAD_CHARS so no head slice is shown.
    second = "y" * 10_000
    files = [
        {"filename": "first.py", "status": "modified", "additions": 1,
         "deletions": 0, "changes": 1, "patch": first},
        {"filename": "second.py", "status": "modified", "additions": 1,
         "deletions": 0, "changes": 1, "patch": second},
    ]

    with patch.object(github_tools, "_github_request", lambda *a, **k: files):
        out = github_tools.get_pr_files(1, repo="o/r")

    # First file's full patch is present.
    assert first in out
    # Second file is fully deferred.
    assert "(diff omitted to stay within budget" in out
    # Overflow listing names the second file without the partial suffix.
    listing = out.split("Some diffs were omitted")[1]
    assert "second.py" in listing
    assert "second.py (+1 -0)" in listing
    assert "second.py" not in listing.replace("second.py (+1 -0)", "")  # only the plain entry


def test_get_pr_files_inlines_patch_exactly_at_budget_boundary():
    """A patch whose length is exactly the budget is fully inlined (guards <= boundary)."""
    patch_str = "z" * TOTAL_DIFF_BUDGET_CHARS
    files = [{
        "filename": "exact.py", "status": "modified",
        "additions": 1, "deletions": 0, "changes": 1, "patch": patch_str,
    }]

    with patch.object(github_tools, "_github_request", lambda *a, **k: files):
        out = github_tools.get_pr_files(1, repo="o/r")

    assert patch_str in out
    assert "omitted" not in out.lower()
    assert "head only" not in out.lower()


def test_get_pr_files_defers_file_when_head_trim_is_empty():
    """A patch starting with a newline must not render an empty 'head only' diff."""
    # First file leaves remaining ~2,400 (above MIN_PARTIAL_HEAD_CHARS).
    first = "a" * 97_600
    # Second file starts with a newline; trimming to the last newline within the
    # slice would leave an empty head, so it must be fully deferred instead.
    second = "\n" + "x" * 5000
    files = [
        {"filename": "first.py", "status": "modified", "additions": 1,
         "deletions": 0, "changes": 1, "patch": first},
        {"filename": "second.py", "status": "modified", "additions": 1,
         "deletions": 0, "changes": 1, "patch": second},
    ]

    with patch.object(github_tools, "_github_request", lambda *a, **k: files):
        out = github_tools.get_pr_files(1, repo="o/r")

    # Sanity: enough budget remained that a head slice would have been attempted.
    assert (TOTAL_DIFF_BUDGET_CHARS - len(first)) >= MIN_PARTIAL_HEAD_CHARS
    # Second file is fully deferred, not shown with an empty head.
    assert "(diff omitted to stay within budget" in out
    # No empty "Diff (head only):" block (header immediately followed by the omitted line).
    assert "Diff (head only):\n\n   ..." not in out


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
