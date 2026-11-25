#!/usr/bin/env python3
"""Package exactly the tracked files of this folder, plus the reviewed dashboard build.

In a git checkout the package inputs are ``git ls-files``; every artifact records that list in
``package-files.txt``, so a build without git (an extracted sdist, a materialized workspace)
packages the same files and never an operator checkout wholesale. ``python build.py web`` builds
the dashboard from those inputs in a clean directory and records the output inventory in
``dashboard/web-next/dist.json``; package builds reject stale inputs, changed or extra outputs and
links.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ImportError:  # `python build.py web` needs only the standard library.
    BuildHookInterface = None  # type: ignore[assignment,misc]

RECORDED = "package-files.txt"
WEB = "dashboard/web-next"
WEB_MANIFEST = f"{WEB}/dist.json"
# Import packages in the wheel, by source prefix: the harness and the client.
RUNTIME_ROOTS = {"harness/src/strandly/": "strandly/", "cli/src/strandly_cli/": "strandly_cli/"}


def package_files(root: Path) -> list[str]:
    """The folder's package inputs, relative to ``root``.

    Raises:
        ValueError: ``root`` is neither a git checkout nor a built artifact.
    """
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--", "."], cwd=root, capture_output=True, check=True,
        ).stdout.decode()
    except (OSError, subprocess.CalledProcessError):
        listed = ""
    # The root .gitignore is VCS metadata, not a package input.
    files = sorted(name for name in listed.split("\0") if name and name != ".gitignore")
    if files:
        return files
    recorded = root / RECORDED
    if recorded.is_file():
        return recorded.read_text().splitlines()
    raise ValueError(f"No package inputs in {root}: build from a git checkout or a built artifact")


def _runtime_name(target: str) -> str | None:
    for prefix, package in RUNTIME_ROOTS.items():
        if target.startswith(prefix):
            return package + target.removeprefix(prefix)
    return None


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)


def web_inputs(root: Path) -> dict[str, bytes]:
    """Dashboard sources by path relative to the web app; never ``dist/`` or local env files."""
    selected = {}
    for target in package_files(root):
        if not target.startswith(WEB + "/") or target.startswith(WEB + "/dist/") or target == WEB_MANIFEST:
            continue
        relative = Path(target).relative_to(WEB)
        if ".." in relative.parts or relative.is_absolute() or relative.name in {".env", ".npmrc"}:
            raise ValueError(f"Private/unsafe web build input: {target}")
        path = root / target
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or linked web input: {target}")
        selected[relative.as_posix()] = path.read_bytes()
    if "package-lock.json" not in selected or "package.json" not in selected:
        raise ValueError("Web build requires package.json and package-lock.json")
    return selected


def web_input_digest(root: Path, source: dict[str, bytes] | None = None) -> str:
    source = source if source is not None else web_inputs(root)
    digest = hashlib.sha256(Path(__file__).read_bytes())
    for name, data in sorted(source.items()):
        digest.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()


def validate_web_assets(root: Path) -> dict[str, str]:
    """The reviewed dashboard outputs (``dist/`` path to sha256), after checking them.

    Raises:
        ValueError: The outputs are missing, stale, changed, extra or linked.
    """
    path = root / WEB_MANIFEST
    if not path.is_file():
        raise ValueError("Build reviewed web assets first: python build.py web")
    manifest = json.loads(path.read_text())
    if manifest.get("format") != 1 or manifest.get("source_sha256") != web_input_digest(root):
        raise ValueError("Web provenance is stale: run python build.py web")
    expected = manifest.get("files", {})
    if not isinstance(expected, dict) or "index.html" not in expected:
        raise ValueError("Invalid generated web inventory")
    dist = root / WEB / "dist"
    if dist.is_symlink():
        raise ValueError("Linked generated web directory")
    actual = set()
    for file in dist.rglob("*"):
        if file.is_symlink():
            raise ValueError(f"Linked generated output: {file}")
        if file.is_file():
            actual.add(file.relative_to(dist).as_posix())
    if actual != set(expected):
        raise ValueError(f"Undeclared/missing web output: extra={sorted(actual - set(expected))}, missing={sorted(set(expected) - actual)}")
    for name, digest in expected.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Unsafe output path: {name}")
        if hashlib.sha256((dist / relative).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Changed generated web output: {name}")
    return expected


def build_web(root: Path) -> None:
    """Build the dashboard from its package inputs in a clean directory (needs node and npm)."""
    source = web_inputs(root)
    source_hash = web_input_digest(root, source)
    with tempfile.TemporaryDirectory(prefix="strandly-web-build-") as directory:
        work = Path(directory)
        for name, data in source.items():
            output = work / name
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
        home = work / ".build-home"
        home.mkdir()
        # No checkout .env/.npmrc or process VITE_* values reach the build.
        environment = {"PATH": os.environ["PATH"], "HOME": str(home), "TZ": "UTC", "LANG": "C.UTF-8"}
        cache = subprocess.check_output(["npm", "config", "get", "cache"], text=True).strip()
        environment["npm_config_cache"] = cache
        subprocess.run(["npm", "ci"], cwd=work, env=environment, check=True)
        subprocess.run(["npm", "run", "build"], cwd=work, env=environment, check=True)
        dist = work / "dist"
        files = {}
        for file in sorted(dist.rglob("*")):
            if file.is_symlink():
                raise ValueError(f"Build emitted a symlink: {file}")
            if file.is_file():
                files[file.relative_to(dist).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
        if "index.html" not in files:
            raise ValueError("Build emitted no index.html")
        destination = root / WEB / "dist"
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(dist, destination)
        (root / WEB_MANIFEST).write_text(json.dumps({"format": 1, "source_sha256": source_hash, "files": files}, indent=2, sort_keys=True) + "\n")
    validate_web_assets(root)
    print(f"Reviewed web build: {len(files)} outputs from {len(source)} package inputs")


if BuildHookInterface is not None:

    class CustomBuildHook(BuildHookInterface):
        def initialize(self, version, build_data):
            if version == "editable":
                return
            root = Path(self.root)
            files = package_files(root)
            # Hatch's sdist defaults force-include VCS ignore files and conventional build
            # scripts; only the package inputs may ship.
            force_include = build_data["force_include"] = {}
            # Force-including a source file at a different wheel path removes it from Hatch's
            # regular traversal, so the wheel's copies are staged in their own directories.
            self.assets = tempfile.TemporaryDirectory(prefix="strandly-build-assets-")
            assets = Path(self.assets.name)
            self.runtime = tempfile.TemporaryDirectory(prefix="strandly-build-runtime-")
            runtime = Path(self.runtime.name)
            recorded = Path(tempfile.mkdtemp(prefix="strandly-build-list-", dir=self.runtime.name)) / RECORDED
            recorded.write_text("".join(f"{name}\n" for name in files))
            generated = [f"{WEB}/dist/{name}" for name in validate_web_assets(root)] + [WEB_MANIFEST]
            for target in files + generated:
                path = root / target
                if not path.is_file() or path.is_symlink():
                    raise ValueError(f"Missing or symlinked package input: {target}")
                if self.target_name == "sdist":
                    force_include[str(path)] = target
                elif self.target_name == "wheel":
                    _copy(path, assets / target)
                    runtime_name = _runtime_name(target)
                    if runtime_name is not None:
                        _copy(path, runtime / runtime_name)
            if self.target_name == "sdist":
                force_include[str(recorded)] = RECORDED
            elif self.target_name == "wheel":
                shutil.copyfile(recorded, assets / RECORDED)
                force_include[str(assets)] = "strandly/_deployment"
                force_include[str(runtime / "strandly")] = "strandly"
                force_include[str(runtime / "strandly_cli")] = "strandly_cli"

        def finalize(self, version, build_data, artifact_path):
            if version != "editable":
                self.assets.cleanup()
                self.runtime.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build or check the reviewed dashboard assets.")
    parser.add_argument("command", choices=["web"])
    parser.add_argument("--check", action="store_true", help="validate the existing build only")
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    if args.check:
        print(f"Validated {len(validate_web_assets(here))} generated web outputs")
    else:
        build_web(here)
