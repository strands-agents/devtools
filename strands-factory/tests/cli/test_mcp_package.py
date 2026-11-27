"""The harness ↔ ``strandly-mcp`` seam.

The wheel must not depend on the harness, so it vendors ``strandly.core.session_ids`` at build time
as ``strandly_mcp._session_primitives``. These tests pin the thin package's session ids to the
harness's and check that the harness shims expose the objects the CLI's tests monkeypatch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import strandly_mcp.agentcore as mcp_agentcore
import strandly_mcp.sessions as mcp_sessions
from strandly.core import session_ids
from strandly.core.constants import RUNTIME_SESSION_ID_MIN_LEN
from strandly.ops.runtime_client import _parse_sse


@pytest.mark.parametrize(
    "repo, kind, number",
    [
        ("strands-agents/sdk-python", "pr", 1951),
        ("example-user/example-operator-repo", "issue", 478),
        ("o/r", "disc", "12"),
        ("/o/r/", "issue", 1),
    ],
)
def test_canonical_session_id_parity(repo, kind, number):
    assert mcp_sessions.canonical_session_id(repo, kind, number) == session_ids.canonical_session_id(
        repo, kind, number
    )


@pytest.mark.parametrize("raw", ["mcp-abc", "gh-o-r-pr-1", "a/b c", "x" * 40, "///", "s"])
def test_runtime_session_id_parity(raw):
    assert mcp_agentcore.runtime_session_id(raw) == session_ids.runtime_session_id(raw)
    assert mcp_agentcore.sanitize_session_id(raw) == session_ids.sanitize_session_id(raw)
    assert mcp_agentcore.RUNTIME_SESSION_ID_MIN_LEN == RUNTIME_SESSION_ID_MIN_LEN


def test_sse_parser_parity():
    lines = [b"data: {\"kind\": \"text\", \"text\": \"hi\"}", b"", b": c", b"data: raw", "data:"]
    assert list(mcp_agentcore._parse_sse(iter(lines))) == list(_parse_sse(iter(lines)))


def test_the_thin_package_never_imports_the_harness():
    # A clean-venv check of the built wheel proves this; this is the in-tree early warning.
    import pkgutil
    import sys

    import strandly_mcp

    before = {m for m in sys.modules if m.startswith("strandly")}
    for info in pkgutil.walk_packages(strandly_mcp.__path__, prefix="strandly_mcp."):
        __import__(info.name)
    import ast
    from pathlib import Path

    for path in Path(strandly_mcp.__path__[0]).rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                root = name.split(".")[0]
                assert root not in {"strandly", "strands", "strands_harness", "strands_tools"}, (
                    f"{path.name} imports {name}"
                )
    assert {m for m in sys.modules if m.startswith("strandly")} >= before


def test_thin_package_license_matches_canonical_root():
    root = Path(__file__).resolve().parents[2]
    assert (root / "cli/strandly-mcp/LICENSE").read_bytes() == (root / "LICENSE").read_bytes()
