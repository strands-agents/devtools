#!/usr/bin/env python3
"""Build the agent runtime's code bundle, the directory AgentRuntimeStack ships as a code asset.

The bundle holds ``main.py``, ``src/`` and the locked dependencies installed for Linux arm64
(``--from-lock`` exports exact pins and hashes from ``uv.lock``, with the workspace
``strandly-mcp`` built as a wheel), so it builds the same way on any host.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PYTHON_VERSION = "3.12"
#: Tried in order, like the toolkit packager (newer glibc tags as fallbacks).
PLATFORMS = ("aarch64-manylinux2014", "aarch64-manylinux_2_17", "aarch64-manylinux_2_28")
#: AgentCore direct-code-deploy limit (uncompressed).
MAX_UNCOMPRESSED_BYTES = 750 * 1024 * 1024
#: What the runtime imports from the checkout, relative to :data:`SOURCE_ROOT`; copied to the
#: bundle root under the same names. ``main.py`` puts ``src/`` on ``sys.path``.
SOURCE_ROOT = "harness"
SOURCE_ALLOWLIST = ("main.py", "src")
_HARDCODED_SHEBANG = re.compile(r"^#!(?!/usr/bin/env\b)(.+python[0-9.]*)\s*$")
PORTABLE_SHEBANG = "#!/usr/bin/env python3"


def fix_shebangs(bin_dir: Path) -> int:
    """Rewrite absolute-interpreter shebangs under ``bin_dir``. Returns how many were fixed."""
    if not bin_dir.is_dir():
        return 0
    fixed = 0
    for entry in sorted(bin_dir.iterdir()):
        if not entry.is_file():
            continue
        try:
            text = entry.read_text()
        except (UnicodeDecodeError, OSError):
            continue  # a native binary (uv, monty) — nothing to rewrite
        first, sep, rest = text.partition("\n")
        if _HARDCODED_SHEBANG.match(first):
            entry.write_text(PORTABLE_SHEBANG + sep + rest)
            fixed += 1
    return fixed


def copy_sources(repo_root: Path, out: Path) -> None:
    """Copy the allow-listed runtime source into ``out`` (no caches, no tests)."""
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info", ".DS_Store")
    for rel in SOURCE_ALLOWLIST:
        src = repo_root / SOURCE_ROOT / rel
        if src.is_dir():
            shutil.copytree(src, out / rel, ignore=ignore, dirs_exist_ok=True)
        elif src.is_file():
            shutil.copy2(src, out / rel)
        else:
            raise FileNotFoundError(f"{src} is missing — run from the repo root")


def copy_addon_sources(out: Path) -> None:
    source = os.environ.get("STRANDLY_BUILD_ADDON_SOURCE")
    if source:
        path = Path(source).resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"Configured addon source is missing: {path}")
        shutil.copytree(path, out / "src", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def strip_local_install_metadata(out: Path) -> None:
    """Do not ship local wheel/source locations or uv build-cache provenance in deployment assets."""
    for metadata in out.glob("*.dist-info"):
        removed = set()
        for name in ("direct_url.json", "uv_cache.json"):
            path = metadata / name
            if path.exists():
                path.unlink()
                removed.add(f"{metadata.name}/{name}")
        record = metadata / "RECORD"
        if removed and record.is_file():
            with record.open(newline="") as stream:
                rows = [row for row in csv.reader(stream) if row and row[0] not in removed]
            with record.open("w", newline="") as stream:
                csv.writer(stream).writerows(rows)


def bundle_size(out: Path) -> int:
    return sum(p.stat().st_size for p in out.rglob("*") if p.is_file())


def install_dependencies(requirements: Path, out: Path) -> None:
    last: subprocess.CalledProcessError | None = None
    for platform in PLATFORMS:
        cmd = [
            "uv", "pip", "install", "--target", str(out),
            "--python-version", PYTHON_VERSION,
            "--python-platform", platform,
            "--only-binary", ":all:",
            "-r", str(requirements),
        ]  # fmt: skip
        print(f">> {' '.join(cmd)}", flush=True)
        try:
            subprocess.run(cmd, check=True)
            return
        except subprocess.CalledProcessError as exc:
            last = exc
            print(f"!! install for {platform} failed; trying the next platform tag", flush=True)
    assert last is not None
    raise last


#: The runtime extras exported from ``uv.lock`` (base deps + these).
EXPORT_EXTRAS = ("agentcore", "mcp", "s3", "observability")
#: Packages the runtime cannot start (or trace) without; an export missing one is refused.
REQUIRED_PINS = (
    "strands-agents",
    "strands-harness",
    "anthropic",
    "pydantic",
    "bedrock-agentcore",
    "mcp",
    "strands-agents-mcp-server",
    "uv",
    "boto3",
    "aws-opentelemetry-distro",
)
_MCP_EDITABLE = "-e ./cli/strandly-mcp"


def export_requirements(repo_root: Path, dest: Path, wheel_dir: Path) -> None:
    """Pin ``dest`` from ``uv.lock`` (what ``strandly deploy`` and a deploy pipeline build from).

    ``uv export --frozen`` the runtime extras (exact ``==`` pins + sha256 hashes), refuse an export
    missing a :data:`REQUIRED_PINS` package, then replace the editable workspace member
    ``strandly-mcp`` with a locally built wheel + its sha256 (``--target`` installs cannot take a
    source tree).

    Raises:
        RuntimeError: On a missing required pin or a surviving editable requirement.
    """
    cmd = ["uv", "export", "--frozen", "--format", "requirements-txt", "--no-emit-project",
           "--no-dev", "-o", str(dest)]  # fmt: skip
    for extra in EXPORT_EXTRAS:
        cmd += ["--extra", extra]
    subprocess.run(cmd, check=True, cwd=repo_root)
    text = dest.read_text()
    pinned = {
        re.split(r"[\[=;\s]", line.strip(), maxsplit=1)[0].lower()
        for line in text.splitlines()
        if "==" in line
    }
    missing = [pkg for pkg in REQUIRED_PINS if pkg not in pinned]
    if missing:
        raise RuntimeError(f"uv export produced no pin for {missing} (is uv.lock complete?)")
    if _MCP_EDITABLE in text.splitlines():
        wheel_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["uv", "build", "--wheel", "-o", str(wheel_dir), "cli/strandly-mcp"],
            check=True,
            cwd=repo_root,
        )
        wheel = next(wheel_dir.glob("strandly_mcp-*.whl"))
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        text = "\n".join(
            f"{wheel} --hash=sha256:{digest}" if line == _MCP_EDITABLE else line
            for line in text.splitlines()
        )
        dest.write_text(text + "\n")
    if any(line.startswith("-e ") for line in dest.read_text().splitlines()):
        raise RuntimeError(f"an editable requirement survived in {dest}")


def build(repo_root: Path, requirements: Path, out: Path) -> int:
    if not requirements.is_file():
        raise FileNotFoundError(f"{requirements} not found (a deploy pipeline writes it with `uv export`)")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    install_dependencies(requirements, out)
    strip_local_install_metadata(out)
    fixed = fix_shebangs(out / "bin")
    copy_sources(repo_root, out)
    copy_addon_sources(out)
    for cache in out.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    size = bundle_size(out)
    print(f">> bundle {out}: {size / 1024 / 1024:.1f} MiB uncompressed, {fixed} shebang(s) fixed")
    if size > MAX_UNCOMPRESSED_BYTES:
        print(
            f"!! bundle exceeds AgentCore's {MAX_UNCOMPRESSED_BYTES // 1024 // 1024} MB "
            "uncompressed code limit",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("infra/build/agent"))
    parser.add_argument("--requirements", type=Path, default=None)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--from-lock",
        action="store_true",
        help="pin requirements from uv.lock (the default, and what a deploy pipeline does)",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    if args.requirements is not None and not args.from_lock:
        return build(repo_root, args.requirements.resolve(), args.out.resolve())
    with tempfile.TemporaryDirectory(prefix="strandly-agent-req-") as tmp:
        pinned = Path(tmp) / "requirements.txt"
        export_requirements(repo_root, pinned, Path(tmp) / "wheels")
        return build(repo_root, pinned, args.out.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
