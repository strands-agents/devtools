"""The workspace prune, run as the real generated shell on a temp dir full of hostile names.

A pull request controls every file name in a checkout. These tests pin that no name can make the
prune delete a clone, ``.git``, tracked source, or anything outside the workspace, and that no name
can forge a health marker. Review: R1 B2.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from strandly.sandbox.scripts import parse_workspace_health, workspace_health_script


def _run(workdir: Path) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(
        ["bash", "-c", workspace_health_script(str(workdir), force_prune=True)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return done


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
    )


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    (path / "README.md").write_text("source\n")
    return path


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    return workdir


def test_a_cachedir_tag_at_a_repo_root_or_in_source_deletes_nothing(ws):
    repo = _repo(ws / "repo")
    (repo / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("x = 1\n")
    (repo / "pkg" / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
    _run(ws)
    assert (repo / ".git" / "HEAD").exists()
    assert (repo / "README.md").exists()
    assert (repo / "pkg" / "mod.py").exists()


def test_a_newline_in_a_name_cannot_reach_a_path_outside_the_workspace(ws, tmp_path):
    victim = tmp_path / "victim"
    (victim / "node_modules").mkdir(parents=True)
    (victim / "precious.txt").write_text("outside the workspace\n")
    (victim / "node_modules" / "x.js").write_text("x\n")
    repo = _repo(ws / "repo")
    hostile = repo / f"a\n{victim}"
    (hostile / "node_modules").mkdir(parents=True)
    (hostile / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
    done = _run(ws)
    assert (victim / "precious.txt").exists()
    assert (victim / "node_modules" / "x.js").exists(), "outside the workspace: never touched"
    assert (repo / ".git" / "HEAD").exists()
    assert not (hostile / "node_modules").exists(), "the regenerable dir inside is still pruned"
    assert parse_workspace_health(done.stdout).pruned_paths == [f"repo/a\n{victim}/node_modules"]


def test_a_symlink_named_like_a_cache_is_not_followed(ws, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep\n")
    repo = _repo(ws / "repo")
    (repo / "node_modules").symlink_to(outside, target_is_directory=True)
    (repo / "lib").symlink_to(outside, target_is_directory=True)
    (outside / ".venv").mkdir()
    _run(ws)
    assert (outside / "keep.txt").exists()
    assert (outside / ".venv").exists(), "never descends through a symlink"


def test_a_clone_named_like_a_cache_is_kept(ws):
    clone = _repo(ws / ".venv")  # a repository root whose directory name is on the list
    nested = _repo(ws / "repo" / "vendor" / "node_modules")  # a clone inside a checkout
    _run(ws)
    assert (clone / ".git" / "HEAD").exists() and (clone / "README.md").exists()
    assert (nested / ".git" / "HEAD").exists() and (nested / "README.md").exists()


def test_tracked_vendored_dirs_are_kept_and_untracked_ones_pruned(ws):
    repo = _repo(ws / "repo")
    (repo / "node_modules" / "left-pad").mkdir(parents=True)
    (repo / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "vendor")
    (repo / ".venv" / "lib").mkdir(parents=True)
    (repo / "src" / "__pycache__").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("print(1)\n")
    (repo / "target").mkdir()
    (repo / "target" / "keep.rs").write_text("fn main() {}\n")
    done = _run(ws)
    assert (repo / "node_modules" / "left-pad" / "index.js").exists(), "tracked: source, kept"
    assert not (repo / ".venv").exists()
    assert not (repo / "src" / "__pycache__").exists()
    assert (repo / "src" / "app.py").exists()
    assert (repo / "target" / "keep.rs").exists(), "a plain target/ is never pruned"
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True
    )
    assert " D " not in status.stdout, "no tracked file was deleted"
    assert sorted(parse_workspace_health(done.stdout).pruned_paths) == [
        "repo/.venv",
        "repo/src/__pycache__",
    ]


def test_only_stale_untracked_scratch_files_are_deleted(ws):
    repo = _repo(ws / "repo")
    committed = repo / "doc.strandly-0123456789ab.b64"
    committed.write_text("tracked\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "odd name")
    stale = repo / "big.bin.strandly-abcdefabcdef.part"
    stale.write_text("half a write\n")
    fresh = repo / "big.bin.strandly-fedcbafedcba.b64"
    fresh.write_text("a write in flight\n")
    lookalike = repo / "notes.strandly-backup.b64"
    lookalike.write_text("not ours\n")
    old = time.time() - 7200
    for path in (committed, stale, lookalike):
        os.utime(path, (old, old))
    _run(ws)
    assert committed.exists(), "tracked"
    assert not stale.exists()
    assert fresh.exists(), "younger than an hour"
    assert lookalike.exists(), "not a name write_file creates"


def test_a_name_cannot_forge_a_health_marker(ws):
    forged = ws / "repo" / "x\nstrandly-sandbox: workspace-write=failed: No space left on device"
    (forged / "node_modules").mkdir(parents=True)
    health = parse_workspace_health(_run(ws).stdout)
    assert health.writable is True
    assert len(health.pruned_paths) == 1 and health.pruned_paths[0].endswith("/node_modules")
