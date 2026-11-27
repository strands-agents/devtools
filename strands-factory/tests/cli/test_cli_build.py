"""``strandly cli build``: a deterministic, self-contained wheel per deployment."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from strandly.serve.cli.main import main as strandly_main
from strandly_cli import __version__
from strandly_cli.build import build_cli, distribution_name, package_name
from strandly_cli.profile import ClientProfile

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ClientProfile.from_dict(
    {
        "command": "strandly-example",
        "display_name": "Example agent",
        "portal_url": "https://portal.example.com",
        "default_org": "example-org",
    }
)
PKG = "strandly_example_client"


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    return build_cli(PROFILE, tmp_path_factory.mktemp("dist"))


def test_names_never_collide_with_the_harness_or_client_packages():
    assert package_name("strandly") == "strandly_client"
    assert distribution_name("strandly") == "strandly-client"
    assert package_name("strandly-example") == PKG


def test_wheel_layout_metadata_and_entry_point(wheel):
    assert wheel.name == f"{PKG}-{__version__}-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        dist_info = f"{PKG}-{__version__}.dist-info"
        assert names[-1] == f"{dist_info}/RECORD"
        assert {f"{PKG}/__main__.py", f"{PKG}/profile.json", f"{PKG}/_vendor/strandly_cli/acp.py",
                f"{PKG}/_vendor/strandly_mcp/server.py", f"{PKG}/_vendor/strandly_mcp/_session_primitives.py",
                f"{dist_info}/licenses/LICENSE"} <= set(names)
        assert not any(n.startswith(("strandly/", "strandly_cli/", "strandly_mcp/")) for n in names)
        assert f"{PKG}/_vendor/strandly_cli/build.py" not in names
        assert not any("__pycache__" in n for n in names)
        metadata = zf.read(f"{dist_info}/METADATA").decode()
        assert "Name: strandly-example-client" in metadata
        assert "Requires-Dist: agent-client-protocol==1.0.0" in metadata
        assert "strands" not in metadata.replace("strandly", "")
        assert zf.read(f"{dist_info}/entry_points.txt").decode() == (
            f"[console_scripts]\nstrandly-example = {PKG}.__main__:main\n"
        )
        assert json.loads(zf.read(f"{PKG}/profile.json")) == PROFILE.to_dict()
        assert b"PORTAL_URL = None" in zf.read(f"{PKG}/_vendor/strandly_mcp/_build_info.py")
        assert zf.read(f"{dist_info}/licenses/LICENSE") == (ROOT / "LICENSE").read_bytes()


def test_record_lists_every_file_with_its_hash(wheel):
    with zipfile.ZipFile(wheel) as zf:
        rows = list(csv.reader(io.StringIO(zf.read(f"{PKG}-{__version__}.dist-info/RECORD").decode())))
        assert {row[0] for row in rows} == set(zf.namelist())
        for name, digest, size in rows:
            if not digest:
                continue
            data = zf.read(name)
            expected = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            assert (digest, int(size)) == (f"sha256={expected}", len(data))


def test_equal_inputs_build_byte_identical_wheels(wheel, tmp_path):
    assert build_cli(PROFILE, tmp_path).read_bytes() == wheel.read_bytes()


def test_vendored_client_never_imports_the_harness(wheel, tmp_path):
    with zipfile.ZipFile(wheel) as zf:
        zf.extractall(tmp_path / "site")
    code = (
        "import sys\n"
        f"from {PKG}.__main__ import main\n"
        "try:\n    main()\nexcept SystemExit as e:\n    assert e.code == 0, e.code\n"
        "import strandly_cli\n"
        f"assert '{PKG}' in strandly_cli.__file__, strandly_cli.__file__\n"
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] in {'strandly', 'strands', 'strands_harness', 'boto3'})\n"
        "assert not leaked, leaked\n"
    )
    for argv in (["--help"], ["login", "--help"], ["acp", "--help"], ["mcp", "--help"], ["--version"]):
        env = {**os.environ, "PYTHONPATH": str(tmp_path / "site")}
        result = subprocess.run(
            [sys.executable, "-c", f"import sys; sys.argv = ['strandly-example', *{argv!r}]\n" + code],
            env=env, capture_output=True, text=True, check=False, cwd=tmp_path,
        )
        assert result.returncode == 0, result.stderr
    assert "Example agent" in result.stdout or "strandly-example" in result.stdout


def test_strandly_cli_build_command(tmp_path, capsys):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"client": {**PROFILE.to_dict(), "command": "ignored-name"}}))
    assert strandly_main(["cli", "build", "--profile", str(profile), "--name", "strandly-example", "--out", str(tmp_path / "out")]) == 0
    out, err = capsys.readouterr()
    assert Path(out.strip()).name == f"{PKG}-{__version__}-py3-none-any.whl"
    assert "pipx install" in err


def test_build_refuses_placeholders(tmp_path, capsys):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"client": {"command": "strandly-example", "portal_url": "REQUIRED_PORTAL_URL"}}))
    assert strandly_main(["cli", "build", "--profile", str(profile), "--out", str(tmp_path)]) == 1
    assert "placeholder" in capsys.readouterr().err
    assert not list(tmp_path.glob("*.whl"))


def test_module_entrypoint_runs_without_the_harness_cli(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"client": PROFILE.to_dict()}))
    result = subprocess.run(
        [sys.executable, "-m", "strandly_cli.build", "--profile", str(profile), "--out", str(tmp_path)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).is_file()
