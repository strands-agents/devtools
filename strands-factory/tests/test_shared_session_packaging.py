"""Build real distributions; never let workspace imports stand in for isolation."""

from __future__ import annotations

import os
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SHARED = _ROOT / "harness/src/strandly/core/session_ids.py"


def _run(*args: str, cwd: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
    assert result.returncode == 0, f"{args!r}\n{result.stdout}\n{result.stderr}"


@pytest.fixture(scope="module")
def wheels(tmp_path_factory):
    work = tmp_path_factory.mktemp("session-wheels")
    result = {}
    for name, project in (("root", _ROOT), ("mcp", _ROOT / "cli/strandly-mcp")):
        dist = work / name
        _run("uv", "build", str(project), "--out-dir", str(dist), cwd=work)
        # Rebuild in a directory with NO repository-relative source available.
        extracted = work / f"{name}-sdist"
        extracted.mkdir()
        with tarfile.open(next(dist.glob("*.tar.gz"))) as archive:
            archive.extractall(extracted, filter="data")
        project_copy = next(extracted.iterdir())
        _run("uv", "build", "--wheel", str(project_copy), "--out-dir", str(dist / "rebuilt"), cwd=work)
        wheel = next((dist / "rebuilt").glob("*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            member = "strandly/core/session_ids.py" if name == "root" else "strandly_mcp/_session_primitives.py"
            assert archive.read(member) == _SHARED.read_bytes()
            result[name] = (wheel, set(archive.namelist()))
    assert not result["root"][1] & result["mcp"][1], "The wheels must not own any of the same files"
    return result


@pytest.mark.parametrize("order", [("root", "mcp"), ("mcp", "root")])
def test_wheels_coexist_and_uninstall_independently(wheels, tmp_path, order):
    env = tmp_path / "env"
    _run("uv", "venv", str(env), cwd=tmp_path)
    python = str(env / "bin/python")
    for name in order:
        _run("uv", "pip", "install", "--python", python, "--no-deps", str(wheels[name][0]), cwd=tmp_path)
    checks = {
        "root": "from strandly.core.session_ids import runtime_session_id; "
                "from strandly.core.session_store_reader import snapshot_key; "
                "assert snapshot_key('..') == 'session/session/scopes/agent/default/snapshots/snapshot_latest.json'",
        "mcp": "from strandly_mcp.agentcore import runtime_session_id",
    }
    tail = "; assert runtime_session_id('a/b') == 'a-b-' + '0' * 29"
    _run(python, "-I", "-c", checks["root"] + "; " + checks["mcp"] + tail, cwd=tmp_path)
    names = {"root": "strands-factory", "mcp": "strandly-mcp"}
    _run("uv", "pip", "uninstall", "--python", python, names[order[0]], cwd=tmp_path)
    removed_module = "strandly" if order[0] == "root" else "strandly_mcp"
    _run(python, "-I", "-c",
         f"import importlib.util; assert importlib.util.find_spec({removed_module!r}) is None",
         cwd=tmp_path)
    _run(python, "-I", "-c", checks[order[1]] + tail, cwd=tmp_path)
