"""A Lambda layer with the AWS SDK pinned in ``uv.lock``.

Python Lambdas otherwise run the runtime's bundled boto3, which lags PyPI; the read API needs a
botocore that knows the AgentCore Memory ``json`` payload member. The layer is built at synth from the
lock's pure-Python wheels and its asset hash is the digest of the pins, so it rolls exactly when
they change.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import tomllib

_REPO_ROOT = Path(__file__).resolve().parents[2]
UV_LOCK = _REPO_ROOT / "uv.lock"
BUILD_ROOT = _REPO_ROOT / "infra" / "build" / "lambda-sdk"

#: Bumped when the layer's layout/install flags change, so the cached dir + asset hash roll too.
_LAYOUT_VERSION = "1"

#: ``tzdata`` for Lambdas that resolve IANA timezones (``zoneinfo`` falls back to it: the python3.12
#: runtime is not guaranteed to ship /usr/share/zoneinfo). Same version as
#: ``infra/scripts/build-poller-package.sh``; the hash is the PyPI wheel's sha256.
TZDATA_REQUIREMENT = (
    "tzdata==2026.6 "
    "--hash=sha256:0a10eec6e13ef16b627fabed9a5f2ba61a62b1c67f327305f4e7e412a70b4aec\n"
)


def _closure(packages: dict[str, dict], root: str) -> list[str]:
    seen: list[str] = []
    stack = [root]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.append(name)
        # Marker-guarded deps (e.g. ``python_version < '3.10'``) never apply to the 3.12 runtime;
        # uv.lock records them with a ``marker`` key — skip those rather than evaluate markers.
        stack.extend(d["name"] for d in packages[name].get("dependencies", []) if "marker" not in d)
    return sorted(seen)


def lock_requirements(lock_path: Path = UV_LOCK, root: str = "boto3") -> str:
    """``root``'s dependency closure from ``uv.lock`` as a ``--require-hashes`` requirements file.

    Raises when a package in the closure is locked at more than one version (a fork the lock
    resolved per-marker), has no wheel, or ships a non-pure wheel — each would make the layer
    ambiguous or platform-specific, which this builder deliberately does not handle.
    """
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    by_name: dict[str, list[dict]] = {}
    for pkg in lock.get("package", []):
        by_name.setdefault(pkg["name"], []).append(pkg)
    forked = sorted(n for n, entries in by_name.items() if len(entries) > 1)
    packages = {n: entries[0] for n, entries in by_name.items()}
    lines: list[str] = []
    for name in _closure(packages, root):
        if name in forked:
            raise RuntimeError(f"{name} is locked at several versions in {lock_path}; pin one")
        pkg = packages[name]
        wheels = pkg.get("wheels") or []
        if not wheels:
            raise RuntimeError(f"{name} {pkg['version']} has no wheel in {lock_path}")
        for wheel in wheels:
            if not wheel["url"].endswith("-none-any.whl"):
                raise RuntimeError(f"{name} wheel {wheel['url']} is not pure Python")
        hashes = " ".join(f"--hash={w['hash']}" for w in wheels)
        lines.append(f"{name}=={pkg['version']} {hashes}")
    return "\n".join(lines) + "\n"


def requirements_key(requirements: str) -> str:
    """The layer's cache key and CDK asset hash: digest of the pins + the layout version."""
    return hashlib.sha256(f"{_LAYOUT_VERSION}\n{requirements}".encode()).hexdigest()


def _install_cmd(requirements: Path, target: Path) -> list[str]:
    common = ["--no-deps", "--require-hashes", "--target", str(target), "-r", str(requirements)]
    if shutil.which("uv"):
        # uv never byte-compiles by default — the tree stays free of timestamped .pyc files.
        return ["uv", "pip", "install", "--python-version", "3.12", *common]
    if importlib.util.find_spec("pip") is None:
        raise RuntimeError(
            "cannot build the Lambda SDK layer: neither `uv` is on PATH nor `pip` is importable from "
            f"{sys.executable}. Install uv (https://docs.astral.sh/uv/) or run the CDK app with a "
            "Python that has pip (e.g. the infra venv: python -m venv .venv && .venv/bin/pip install "
            "-r requirements.txt)."
        )
    return [
        sys.executable, "-m", "pip", "install", "--no-compile", "--only-binary=:all:",
        "--python-version", "3.12", "--implementation", "cp", "--platform", "manylinux2014_x86_64",
        "--disable-pip-version-check", "--no-input", *common,
    ]


def sdk_layer_dir(
    lock_path: Path = UV_LOCK, build_root: Path = BUILD_ROOT, *, extra: str = ""
) -> tuple[Path, str]:
    """The built layer directory (contains ``python/``) and its asset hash; builds it when absent.

    ``extra`` is more ``--require-hashes`` lines installed alongside the SDK (e.g.
    :data:`TZDATA_REQUIREMENT`); it is part of the key. The install lands in a temp dir that is
    renamed into place, so an interrupted or concurrent build never leaves a half-populated cache
    entry behind.
    """
    requirements = lock_requirements(lock_path) + extra
    key = requirements_key(requirements)
    layer_dir = build_root / key[:16]
    if (layer_dir / "python" / "botocore").is_dir():
        return layer_dir, key
    build_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".building-", dir=build_root))
    try:
        req_file = staging / "requirements.txt"
        req_file.write_text(requirements, encoding="utf-8")
        target = staging / "layer" / "python"
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PIP_TARGET")}
        subprocess.run(_install_cmd(req_file, target), check=True, env=env, stdout=subprocess.DEVNULL)
        for junk in target.rglob("__pycache__"):
            shutil.rmtree(junk, ignore_errors=True)
        shutil.rmtree(target / "bin", ignore_errors=True)
        (target / ".lock").unlink(missing_ok=True)  # uv's install lock file, not package content
        try:
            (staging / "layer").rename(layer_dir)
        except OSError:
            if not (layer_dir / "python" / "botocore").is_dir():  # lost a race to a complete build
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return layer_dir, key
