"""Guard: no test may write to the developer's real home directory.

The sandbox fake runs the real git bootstrap in a host shell, and that bootstrap runs
``git config --global user.name/user.email`` and ``credential.helper store``. ``tests/conftest.py``
points ``$HOME`` and ``GIT_CONFIG_GLOBAL`` at a temp dir for every test; these tests fail if that
isolation is removed, before anything is written.
"""

from __future__ import annotations

import os
import pwd
import subprocess

from test_runtime_sandbox import RUNTIME_ARN, FakeAgentCoreClient

from strandly.sandbox.runtime import RuntimeSandbox

#: From the password database, not $HOME (which tests/conftest.py replaces).
REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir


def _real_home_files() -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for name in (".gitconfig", ".git-credentials"):
        try:
            with open(os.path.join(REAL_HOME, name), "rb") as f:
                out[name] = f.read()
        except OSError:
            out[name] = None
    return out


def test_home_and_global_git_config_point_away_from_the_real_home():
    home = os.environ["HOME"]
    assert os.path.realpath(home) != os.path.realpath(REAL_HOME)
    for var in ("GIT_CONFIG_GLOBAL", "XDG_CONFIG_HOME"):
        assert os.environ.get(var, "").startswith(home), f"{var} must live under the temp HOME"
    assert os.environ.get("GIT_CONFIG_NOSYSTEM") == "1"


async def test_the_real_git_bootstrap_writes_only_to_the_temp_home(tmp_path):
    assert os.path.realpath(os.environ["HOME"]) != os.path.realpath(REAL_HOME)  # before any write
    before = _real_home_files()
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    sandbox = RuntimeSandbox(
        runtime_arn=RUNTIME_ARN,
        session_key="hermetic-home",
        workdir=str(workdir),
        client=FakeAgentCoreClient(workdir=str(workdir)),
        github_token="ghs_not_a_real_token",
    )
    result = await sandbox.execute("git config --global user.name")
    assert result.stdout.strip() == "Strandly", "the bootstrap really ran"
    helper = subprocess.run(
        ["git", "config", "--global", "credential.helper"], capture_output=True, text=True
    )
    assert helper.stdout.strip() == "store", "…and wrote the temp global config"
    assert _real_home_files() == before
