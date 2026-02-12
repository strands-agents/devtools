#!/usr/bin/env python3
"""
Strands Agents Release Helper

Automates the release preparation process for all Strands Agents repositories.

WHAT IT DOES:
  1. Clones all repos (or pulls if already cloned)
  2. Gets latest tag and commits since that tag
  3. Auto-determines version bump (MAJOR/MINOR/PATCH) from commit messages
  4. Runs integration tests for each repo
  5. Generates release parameters and release report

USAGE:
  python3 strands_release_helper.py                     # Full run with tests
  python3 strands_release_helper.py --skip-tests        # Just changelogs, no tests
  python3 strands_release_helper.py --skip-tests --parallel  # Fast parallel mode
  python3 strands_release_helper.py --repos sdk-python,tools  # Specific repos
  python3 strands_release_helper.py --work-dir /tmp/release  # Custom work dir

OUTPUT:
  {work_dir}/
    ├── release_report.md   # Human-readable summary
    ├── release_params.txt  # Copy-paste ready release parameters
    ├── logs/               # Full test output logs
    │   ├── sdk-python_tests.log
    │   ├── sdk-typescript_tests.log
    │   └── ...
    └── {repo}/             # Cloned repositories

REPOS COVERED:
  - sdk-python      (hatch run test-integ)
  - sdk-typescript  (npm run test:integ)
  - tools           (hatch run test-integ)
  - agent-sop       (hatch test)
  - agent-builder   (hatch test)
  - evals           (hatch run test-integ)
  - mcp-server      (hatch test)

VERSION BUMP LOGIC:
  For 1.x+ versions (standard semver):
    - MAJOR: Commits with "BREAKING" in message
    - MINOR: Commits starting with "feat:"
    - PATCH: All other commits (fix, perf, refactor, etc.)
  
  For 0.x versions (pre-1.0 semver):
    - MINOR: Commits with "BREAKING" in message
    - PATCH: All other commits (including features)
    - This allows controlling when to go 1.0 while still using semver
  
  - NONE: No commits since last tag

NOTES:
  - Tests run sequentially by default (parallel can cause port conflicts)
  - --parallel only works with --skip-tests
  - Test output streams live to console and saves to logs/
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


def ensure_uv_installed():
    """Ensure uv is installed, installing it if necessary."""
    if shutil.which("uv"):
        print("uv is already installed.")
        return

    print("uv not found. Installing uv...")
    code, stdout, stderr = run_cmd("curl -LsSf https://astral.sh/uv/install.sh | sh")
    if code != 0:
        sys.exit(1)

    # Add ~/.local/bin to PATH (where uv installer places the binary)
    local_bin = Path.home() / ".local" / "bin"
    if str(local_bin) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{local_bin}{os.pathsep}{os.environ.get('PATH', '')}"

    if not shutil.which("uv"):
        # Also try ~/.cargo/bin (alternative install location)
        cargo_bin = Path.home() / ".cargo" / "bin"
        if str(cargo_bin) not in os.environ.get("PATH", ""):
            os.environ["PATH"] = f"{cargo_bin}{os.pathsep}{os.environ.get('PATH', '')}"

    if not shutil.which("uv"):
        print("ERROR: uv installation succeeded but binary not found in PATH.")
        sys.exit(1)

    print("uv installed successfully.")


def setup_environment(work_dir: Path):
    """Set up a virtual environment using uv and install hatch."""
    ensure_uv_installed()

    venv_path = work_dir / ".venv"

    # Create virtual environment (skip if it already exists)
    if venv_path.exists():
        print("Virtual environment already exists, reusing it.")
    else:
        print("Creating virtual environment with uv...")
        code, stdout, stderr = run_cmd("uv venv", cwd=work_dir)
        if code != 0:
            print(f"Failed to create venv: {stderr}")
            sys.exit(1)

    # Activate the venv by modifying environment variables
    # (equivalent to: source .venv/bin/activate)
    venv_bin = venv_path / "bin"
    os.environ["VIRTUAL_ENV"] = str(venv_path)
    os.environ["PATH"] = f"{venv_bin}{os.pathsep}{os.environ.get('PATH', '')}"
    os.environ.pop("PYTHONHOME", None)

    # Install hatch
    print("Installing hatch via uv pip...")
    code, stdout, stderr = run_cmd("uv pip install hatch", cwd=work_dir)
    if code != 0:
        print(f"Failed to install hatch: {stderr}")
        sys.exit(1)

    print("Environment setup complete.\n")


os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
os.environ['AWS_REGION'] = 'us-east-1'

# Repository configurations
REPOS = {
    "sdk-python": {
        "url": "https://github.com/strands-agents/sdk-python",
        "test_cmd": "hatch run test-integ",
        "release_params": {
            "version": ["strands-sdk-python-version", "strands-agent-sdk-python-version"],
            "changelog": ["strands-agent-sdk-python-changelog"],
        },
    },
    "sdk-typescript": {
        "url": "https://github.com/strands-agents/sdk-typescript",
        "test_cmd": "npm install && npm run test:integ",
        "release_params": {
            "version": ["strands-sdk-typescript-version", "strands-agent-sdk-typescript-version"],
            "changelog": ["strands-agent-sdk-typescript-changelog"],
        },
    },
    "tools": {
        "url": "https://github.com/strands-agents/tools",
        "test_cmd": "hatch run test-integ",
        "release_params": {
            "version": ["strands-tools-version", "strands-agent-tools-version"],
            "changelog": ["strands-agent-tool-changelog"],
        },
    },
    "agent-sop": {
        "url": "https://github.com/strands-agents/agent-sop",
        "test_cmd": "cd python && hatch test",
        "release_params": {
            "version": ["strands-agent-sop-version"],
            "changelog": ["strands-agent-sop-changelog"],
        },
    },
    "agent-builder": {
        "url": "https://github.com/strands-agents/agent-builder",
        "test_cmd": "hatch test",
        "release_params": {
            "version": ["strands-agent-builder-version"],
            "changelog": ["strands-agent-builder-changelog"],
        },
    },
    "evals": {
        "url": "https://github.com/strands-agents/evals",
        "test_cmd": "hatch run test-integ",
        "release_params": {
            "version": ["strands-evals-version", "strands-agent-evals-version"],
            "changelog": ["strands-agent-sdk-evals-changelog"],
        },
    },
    "mcp-server": {
        "url": "https://github.com/strands-agents/mcp-server",
        "test_cmd": "hatch test",
        "release_params": {
            "version": ["strands-mcp-server-version", "strands-agent-mcp-server-version"],
            "changelog": ["strands-agent-mcp-server-changelog"],
        },
    },
}


@dataclass
class Commit:
    sha: str
    message: str
    author: str
    date: str

    @property
    def type(self) -> str:
        """Extract commit type (feat, fix, etc.)"""
        match = re.match(r"^(\w+)(?:\([^)]+\))?:", self.message)
        return match.group(1).lower() if match else "other"

    @property
    def first_line(self) -> str:
        return self.message.split("\n")[0]


@dataclass
class RepoResult:
    name: str
    current_version: str = ""
    new_version: str = ""
    bump_type: str = ""
    commits: list[Commit] = field(default_factory=list)
    changelog: str = ""
    test_passed: Optional[bool] = None
    test_output: str = ""
    error: str = ""


def run_cmd(cmd: str, cwd: Optional[Path] = None, timeout: int = 1800, stream: bool = False) -> tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)"""
    try:
        if stream:
            # Stream output in real-time
            process = subprocess.Popen(
                cmd,
                shell=True,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            output_lines = []
            for line in process.stdout:
                print(f"    {line}", end="")
                output_lines.append(line)
            process.wait(timeout=timeout)
            return process.returncode, "".join(output_lines), ""
        else:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s"
    except Exception as e:
        return -1, "", str(e)


def clone_repo(name: str, url: str, work_dir: Path) -> bool:
    """Clone a repository (shallow clone for speed)"""
    repo_path = work_dir / name
    if repo_path.exists():
        print(f"  {name}: Already exists, pulling latest...")
        code, _, err = run_cmd("git fetch --tags && git pull", cwd=repo_path)
        return code == 0

    print(f"  {name}: Cloning...")
    code, _, err = run_cmd(f"git clone --depth 100 --no-single-branch {url} {name}", cwd=work_dir)
    if code != 0:
        print(f"  {name}: Clone failed - {err}")
        return False

    # Fetch all tags
    run_cmd("git fetch --tags", cwd=repo_path)
    return True


def get_latest_tag(repo_path: Path) -> str:
    """Get the latest semver tag"""
    code, stdout, _ = run_cmd(
        "git tag -l 'v*' --sort=-version:refname | head -1",
        cwd=repo_path,
    )
    if code == 0 and stdout.strip():
        return stdout.strip()

    # Fallback: try without 'v' prefix
    code, stdout, _ = run_cmd(
        "git tag -l '[0-9]*' --sort=-version:refname | head -1",
        cwd=repo_path,
    )
    return stdout.strip() if code == 0 else ""


def get_commits_since_tag(repo_path: Path, tag: str) -> list[Commit]:
    """Get all commits since a tag"""
    if not tag:
        # No tag, get last 50 commits
        cmd = 'git log -50 --pretty=format:"%H|%s|%an|%ai"'
    else:
        cmd = f'git log {tag}..HEAD --pretty=format:"%H|%s|%an|%ai"'

    code, stdout, _ = run_cmd(cmd, cwd=repo_path)
    if code != 0 or not stdout.strip():
        return []

    commits = []
    for line in stdout.strip().split("\n"):
        if "|" in line:
            parts = line.split("|", 3)
            if len(parts) >= 4:
                commits.append(Commit(sha=parts[0], message=parts[1], author=parts[2], date=parts[3]))
    return commits


def determine_version_bump(commits: list[Commit], current_version: str) -> tuple[str, str]:
    """Determine version bump type and new version based on commits.
    
    For 0.x versions, semantic versioning shifts down one level:
    - Breaking changes → MINOR (not MAJOR)
    - Features → PATCH (not MINOR)
    - Fixes → PATCH
    
    This allows controlling when to go 1.0 while still using semver.
    """
    version = current_version.lstrip("v")
    
    if not commits:
        return "NONE", current_version

    # Parse current version first to check if pre-1.0
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        return "PATCH", current_version

    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
    is_pre_1_0 = major == 0

    # Check commit types
    has_breaking = any("BREAKING" in c.message.upper() or c.message.startswith("!") for c in commits)
    has_feat = any(c.type == "feat" for c in commits)

    # Determine bump type and calculate new version
    if is_pre_1_0:
        # Pre-1.0: shift everything down one level
        if has_breaking:
            bump_type = "MINOR"
            new_version = f"{major}.{minor + 1}.0"
        else:
            # Both features and fixes are PATCH in 0.x
            bump_type = "PATCH"
            new_version = f"{major}.{minor}.{patch + 1}"
    else:
        # Post-1.0: standard semver
        if has_breaking:
            bump_type = "MAJOR"
            new_version = f"{major + 1}.0.0"
        elif has_feat:
            bump_type = "MINOR"
            new_version = f"{major}.{minor + 1}.0"
        else:
            bump_type = "PATCH"
            new_version = f"{major}.{minor}.{patch + 1}"

    return bump_type, f"v{new_version}"


def generate_changelog(commits: list[Commit], new_version: str) -> str:
    """Generate a formatted changelog from commits"""
    # Ensure version displays with 'v' prefix
    display_version = new_version if new_version.startswith("v") else f"v{new_version}"
    
    if not commits:
        return f"## {display_version} - No changes since last release"

    # Group commits by type
    groups = {"feat": [], "fix": [], "other": []}
    for c in commits:
        if c.type == "feat":
            groups["feat"].append(c)
        elif c.type in ("fix", "perf"):
            groups["fix"].append(c)
        else:
            groups["other"].append(c)

    lines = [f"## {display_version} Changelog ({len(commits)} commits)"]

    if groups["feat"]:
        lines.append("\n### Features")
        for c in groups["feat"]:
            lines.append(f"- {c.first_line}")

    if groups["fix"]:
        lines.append("\n### Fixes")
        for c in groups["fix"]:
            lines.append(f"- {c.first_line}")

    if groups["other"]:
        lines.append("\n### Other")
        for c in groups["other"]:
            lines.append(f"- {c.first_line}")

    return "\n".join(lines)


def run_tests(name: str, repo_path: Path, test_cmd: str, log_dir: Path) -> tuple[bool, str]:
    """Run integration tests for a repo"""
    if not test_cmd:
        return True, "No tests configured"

    print(f"  {name}: Running tests...")
    print(f"    Command: {test_cmd}")
    print(f"    Log: {log_dir / f'{name}_tests.log'}")
    print()
    
    code, stdout, stderr = run_cmd(test_cmd, cwd=repo_path, timeout=1800, stream=True)

    output = stdout + "\n" + stderr
    
    # Save full log
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{name}_tests.log"
    log_file.write_text(output)
    
    passed = code == 0

    # Extract summary from output
    summary_lines = []
    for line in output.split("\n")[-50:]:
        if any(x in line.lower() for x in ["passed", "failed", "error", "test"]):
            summary_lines.append(line)

    return passed, "\n".join(summary_lines[-20:]) if summary_lines else output[-2000:]


def process_repo(name: str, config: dict, work_dir: Path, skip_tests: bool, log_dir: Path) -> RepoResult:
    """Process a single repository"""
    result = RepoResult(name=name)
    repo_path = work_dir / name

    try:
        # Clone
        if not clone_repo(name, config["url"], work_dir):
            result.error = "Failed to clone"
            return result

        # Get version info
        result.current_version = get_latest_tag(repo_path)
        result.commits = get_commits_since_tag(repo_path, result.current_version)
        result.bump_type, result.new_version = determine_version_bump(result.commits, result.current_version)
        result.changelog = generate_changelog(result.commits, result.new_version)

        # Run tests
        if not skip_tests and config["test_cmd"]:
            result.test_passed, result.test_output = run_tests(name, repo_path, config["test_cmd"], log_dir)
        elif not config["test_cmd"]:
            result.test_passed = True
            result.test_output = "No tests configured"

    except Exception as e:
        result.error = str(e)

    return result


def generate_report(results: list[RepoResult], work_dir: Path) -> str:
    """Generate the final release report"""
    lines = [
        "# Strands Agents Release Report",
        f"\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Working Directory: {work_dir}",
        "\n---\n",
    ]

    # Summary table
    lines.append("## Summary\n")
    lines.append("| Repository | Current | New | Bump | Commits | Tests |")
    lines.append("|------------|---------|-----|------|---------|-------|")

    all_tests_pass = True
    for r in results:
        test_status = "✅" if r.test_passed else ("⏭️ SKIP" if r.test_passed is None else "❌")
        if r.test_passed is False:
            all_tests_pass = False
        lines.append(
            f"| {r.name} | {r.current_version or 'N/A'} | {r.new_version} | {r.bump_type} | {len(r.commits)} | {test_status} |"
        )

    # Test status
    lines.append(f"\n## Test Status\n")
    lines.append(f"**{{{{are-test-passing}}}} = {'YES' if all_tests_pass else 'NO'}**\n")

    # Changelogs
    lines.append("\n---\n")
    lines.append("## Changelogs\n")
    for r in results:
        lines.append(f"### {r.name}\n")
        lines.append(f"**{r.current_version or 'N/A'}** → **{r.new_version}**\n")
        lines.append(r.changelog)
        lines.append("\n")

    # Test details (failures only)
    failures = [r for r in results if r.test_passed is False]
    if failures:
        lines.append("\n---\n")
        lines.append("## Test Failures\n")
        for r in failures:
            lines.append(f"### {r.name}\n")
            lines.append("```")
            lines.append(r.test_output[:3000])
            lines.append("```\n")

    return "\n".join(lines)


def generate_release_params(results: list[RepoResult]) -> str:
    """Generate release parameters file"""
    lines = ["# Release Parameters", f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]

    all_tests_pass = all(r.test_passed is not False for r in results)
    lines.append(f"{{{{are-tests-passing}}}} = {'YES' if all_tests_pass else 'NO'}")
    lines.append("")

    for r in results:
        config = REPOS.get(r.name, {})
        params = config.get("release_params", {})

        # Version params
        for param in params.get("version", []):
            lines.append(f"{{{{{param}}}}} = {r.new_version}")

        # Changelog params
        for param in params.get("changelog", []):
            lines.append(f"{{{{{param}}}}} = ")
            lines.append(r.changelog)
            lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Strands Agents Release Helper")
    parser.add_argument("--skip-tests", action="store_true", help="Skip running integration tests")
    parser.add_argument("--repos", type=str, help="Comma-separated list of repos to process (e.g., sdk-python,tools)")
    parser.add_argument("--work-dir", type=str, default="./release_work", help="Working directory for clones")
    parser.add_argument("--parallel", action="store_true", help="Run repos in parallel (tests may conflict)")
    args = parser.parse_args()

    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    log_dir = work_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Set up uv environment (install uv if needed, create venv, install hatch)
    setup_environment(work_dir)

    print(f"Strands Agents Release Helper")
    print(f"Working directory: {work_dir}")
    print(f"Test logs: {log_dir}")
    print(f"Skip tests: {args.skip_tests}")
    print()

    # Filter repos if specified
    repos_to_process = REPOS
    if args.repos:
        repo_names = [r.strip() for r in args.repos.split(",")]
        repos_to_process = {name: REPOS[name] for name in repo_names if name in REPOS}
        invalid = [name for name in repo_names if name not in REPOS]
        if invalid:
            print(f"Warning: Unknown repos ignored: {invalid}")

    results = []

    if args.parallel and args.skip_tests:
        # Parallel processing (only safe without tests)
        print("Processing repos in parallel...")
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(process_repo, name, config, work_dir, args.skip_tests, log_dir): name
                for name, config in repos_to_process.items()
            }
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                print(f"  ✓ {result.name}: {result.current_version} → {result.new_version} ({len(result.commits)} commits)")
    else:
        # Sequential processing
        print("Processing repos sequentially...")
        for name, config in repos_to_process.items():
            print(f"\n{'='*60}")
            print(f"[{name}]")
            print(f"{'='*60}")
            result = process_repo(name, config, work_dir, args.skip_tests, log_dir)
            results.append(result)
            if result.error:
                print(f"  ✗ Error: {result.error}")
            else:
                test_str = ""
                if result.test_passed is True:
                    test_str = " | Tests: ✅"
                elif result.test_passed is False:
                    test_str = " | Tests: ❌"
                print(f"\n  ✓ {result.current_version} → {result.new_version} ({len(result.commits)} commits){test_str}")

    # Sort results by repo order
    repo_order = list(REPOS.keys())
    results.sort(key=lambda r: repo_order.index(r.name) if r.name in repo_order else 999)

    # Generate outputs
    print("\n" + "=" * 60)
    print("Generating reports...")

    report = generate_report(results, work_dir)
    report_path = work_dir / "release_report.md"
    report_path.write_text(report)
    print(f"  Report: {report_path}")

    release_params = generate_release_params(results)
    params_path = work_dir / "release_params.txt"
    params_path.write_text(release_params)
    print(f"  Release Params: {params_path}")

    # Print summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    all_pass = all(r.test_passed is not False for r in results)
    print(f"Tests passing: {'YES ✅' if all_pass else 'NO ❌'}")
    print()
    for r in results:
        status = "✅" if r.test_passed else ("⏭️" if r.test_passed is None else "❌")
        print(f"  {r.name}: {r.current_version} → {r.new_version} ({r.bump_type}) {status}")

    print(f"\nReports saved to: {work_dir}")


if __name__ == "__main__":
    main()
