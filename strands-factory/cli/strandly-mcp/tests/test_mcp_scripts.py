"""The release helpers under scripts/ (stdlib-only; exercised via importlib since it's not a package)."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

from _home_isolation import _isolated_home  # noqa: F401  (autouse: tmp HOME)

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_write_build_info_renders_a_valid_module(tmp_path, monkeypatch):
    mod = _load("write_build_info")
    target = tmp_path / "_build_info.py"
    monkeypatch.setattr(mod, "TARGET", target)
    monkeypatch.setenv("STRANDLY_MCP_PORTAL_URL", "https://portal.example.net/")
    assert mod.main() == 0
    ns: dict = {}
    exec(target.read_text(), ns)  # noqa: S102 — our own generated module
    assert ns["PORTAL_URL"] == "https://portal.example.net"  # trailing slash dropped

    monkeypatch.setenv("STRANDLY_MCP_PORTAL_URL", "")
    assert mod.main() == 0
    ns = {}
    exec(target.read_text(), ns)  # noqa: S102
    assert ns["PORTAL_URL"] is None


def test_write_build_info_rejects_non_https(tmp_path, monkeypatch):
    mod = _load("write_build_info")
    monkeypatch.setattr(mod, "TARGET", tmp_path / "x.py")
    monkeypatch.setenv("STRANDLY_MCP_PORTAL_URL", "http://portal")
    assert mod.main() == 1


def test_committed_build_info_matches_the_renderer():
    # The committed default must be exactly what the script writes for "no portal", or the release
    # build would produce an unexpected diff in the wheel.
    mod = _load("write_build_info")
    committed = (SCRIPTS.parent / "src" / "strandly_mcp" / "_build_info.py").read_text()
    assert committed == mod.render(None)


def test_package_version_agrees_everywhere():
    import strandly_mcp

    pyproject = (SCRIPTS.parent / "pyproject.toml").read_text()
    version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    assert strandly_mcp.__version__ == version
