"""Autouse fixture isolating ``~`` for every strandly-mcp test module.

Not a ``conftest.py``: the repo root's ``tests/conftest.py`` is imported as the top-level module
``conftest`` (``from conftest import FakeModel``), and a second one here would shadow it when both
suites run in one pytest session. Each ``test_mcp_*.py`` imports this fixture instead."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point ``~`` at a temp dir so no test can read or overwrite the developer's real
    ``~/.strandly/auth.json`` (a test once left fixture credentials there)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
