#!/usr/bin/env python3
"""Release dispatcher for the strands repos.

Triggers the manual `workflow_dispatch` release workflows across repos from one
place, so a releaser doesn't have to open each repo's Actions → "Run workflow"
UI. This is the gh-dispatch model: it reads state remotely (PyPI/npm for the
published version, the GitHub compare API for commits since) and dispatches the
release workflow. It does NOT clone repos, run tests, or compute a version —
the releaser types the version, and the dispatched workflow runs the tests and
gates on the in-run `release-gate` approval before publishing.

(Distinct from `strands_release_helper.py`, which clones + tests + auto-bumps
for the older template-based release flow.)

Deterministic plumbing only — the interactive repo selection and version
prompts live in the CLAUDE.md driver that calls this. Two subcommands:

  release_dispatch.py status [target ...] [--json]
      For each target: latest published version, whether there are new commits
      on main since that release, and the commit subjects. `--json` emits a
      machine-readable array for the driver to render.

  release_dispatch.py dispatch <target> <version> [--preview] [--sha SHA]
      Validate the version (bare semver, greater than published), dispatch the
      target's release workflow, and print the run URL. Real release by default
      (dry_run=false → runs the suite, then waits for release-gate approval);
      --preview sets dry_run=true (build + inspect only, no approval/publish).

OWNER defaults to the upstream org `strands-agents`; override with --owner or
the OWNER env var for fork testing. Requires `gh` authenticated with the
`workflow` scope.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

# ── Target registry ──────────────────────────────────────────────────────
# Identify a target by its logical name. `repo` is the name under $OWNER;
# `workflow` is the dispatch file (sdk-python has two, so never key on repo).
# `version` sources the latest published version: "pypi:<name>" or "npm:<name>"
# — the registry is authoritative and avoids malformed-tag surprises. The tag
# for the commits-since compare is derived as `prefix + version`.
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

TARGETS = {
    "harness-python": {
        "repo": "sdk-python",
        "workflow": "release-python.yml",
        "prefix": "python/v",
        "version": "pypi:strands-agents",
        "has_integ": True,
    },
    "harness-typescript": {
        "repo": "sdk-python",
        "workflow": "release-typescript.yml",
        "prefix": "typescript/v",
        "version": "npm:@strands-agents/sdk",
        "has_integ": True,
    },
    "shell": {
        "repo": "shell",
        "workflow": "release.yml",
        "prefix": "v",
        "version": "pypi:strands-shell",
        "has_integ": False,
    },
    "tools": {
        "repo": "tools",
        "workflow": "release.yml",
        "prefix": "v",
        "version": "pypi:strands-agents-tools",
        "has_integ": True,
    },
    "evals": {
        "repo": "evals",
        "workflow": "release.yml",
        "prefix": "v",
        "version": "pypi:strands-agents-evals",
        "has_integ": True,
    },
}


def owner() -> str:
    return os.environ.get("OWNER") or "strands-agents"


def fetch_json(url: str) -> dict:
    """GET a URL and parse JSON. Raises urllib errors on failure."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def latest_published_version(source: str) -> str:
    """Resolve the latest published version from PyPI or npm.

    `source` is "pypi:<name>" or "npm:<name>". Returns the version string, or
    "" if the package/registry lookup fails (e.g. never published yet).
    """
    kind, _, name = source.partition(":")
    try:
        if kind == "pypi":
            return fetch_json(f"https://pypi.org/pypi/{name}/json")["info"]["version"]
        if kind == "npm":
            # Scoped names (@scope/pkg) must percent-encode the slash.
            enc = name.replace("/", "%2F")
            return fetch_json(f"https://registry.npmjs.org/{enc}/latest")["version"]
    except (urllib.error.URLError, KeyError, ValueError):
        return ""
    return ""


def gh(args: list[str]) -> tuple[int, str, str]:
    """Run a gh command, returning (returncode, stdout, stderr)."""
    proc = subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=60
    )
    return proc.returncode, proc.stdout, proc.stderr


def commits_since(repo: str, tag: str) -> dict:
    """Compare `tag`..main via the GitHub API.

    Returns {ahead: int, subjects: [str]} on success, or {error: str} if the
    tag is missing (never released at that version) or the compare fails.
    """
    code, out, err = gh(
        ["api", f"repos/{owner()}/{repo}/compare/{tag}...main",
         "--jq", "{ahead: .ahead_by, subjects: [.commits[].commit.message | split(\"\\n\")[0]]}"]
    )
    if code != 0:
        # 404 → tag not found on this owner (common on forks / first release).
        return {"error": (err.strip() or "compare failed").splitlines()[-1]}
    try:
        data = json.loads(out)
    except ValueError:
        return {"error": "could not parse compare output"}
    # Newest-first from the API; reverse to chronological for display.
    data["subjects"] = list(reversed(data.get("subjects") or []))
    return data


def status(target_names: list[str]) -> list[dict]:
    rows = []
    for name in target_names:
        t = TARGETS[name]
        version = latest_published_version(t["version"])
        row = {
            "target": name,
            "repo": f"{owner()}/{t['repo']}",
            "workflow": t["workflow"],
            "version": version,
            "prefix": t["prefix"],
        }
        if version:
            tag = f"{t['prefix']}{version}"
            row["tag"] = tag
            cmp = commits_since(t["repo"], tag)
            if "error" in cmp:
                row["has_changes"] = None
                row["note"] = cmp["error"]
            else:
                row["ahead"] = cmp["ahead"]
                row["has_changes"] = cmp["ahead"] > 0
                row["subjects"] = cmp["subjects"]
        else:
            row["has_changes"] = None
            row["note"] = "no published version found"
        rows.append(row)
    return rows


def print_status_human(rows: list[dict]) -> None:
    for r in rows:
        if r["has_changes"] is None:
            tail = f"  ({r.get('note', 'unknown')})"
        elif r["has_changes"]:
            tail = f"  {r['ahead']} new commit(s) since {r['tag']}"
        else:
            tail = f"  no new commits since {r['tag']}"
        print(f"{r['target']:<20} latest: {r['version'] or 'n/a':<10}{tail}")
        for s in r.get("subjects", [])[:20]:
            print(f"    - {s}")


def dispatch(name: str, version: str, preview: bool, sha: str) -> int:
    t = TARGETS[name]
    # Validate format up front; the workflow re-checks, but fail fast.
    if not SEMVER_RE.match(version):
        print(f"error: '{version}' is not bare semver (MAJOR.MINOR.PATCH, no 'v').", file=sys.stderr)
        return 2
    published = latest_published_version(t["version"])
    if published and version == published:
        print(f"error: {version} is already the published version.", file=sys.stderr)
        return 2
    if published:
        higher = sorted([published, version], key=lambda v: [int(x) for x in v.split(".")])[-1]
        if higher != version:
            print(f"error: {version} is not greater than published {published}.", file=sys.stderr)
            return 2

    args = [
        "workflow", "run", t["workflow"],
        "-R", f"{owner()}/{t['repo']}", "--ref", "main",
        "-f", f"version={version}",
        "-f", f"dry_run={'true' if preview else 'false'}",
    ]
    if sha:
        args += ["-f", f"sha={sha}"]
    if t["has_integ"]:
        args += ["-f", "run_integ_tests=false"]

    mode = "PREVIEW (dry run)" if preview else "REAL RELEASE (waits for release-gate approval)"
    print(f"Dispatching {name} v{version} → {owner()}/{t['repo']} / {t['workflow']}  [{mode}]")
    code, out, err = gh(args)
    if code != 0:
        print(err.strip() or "dispatch failed", file=sys.stderr)
        return 1

    # gh workflow run prints nothing useful; fetch the run it just created.
    code, out, _ = gh([
        "run", "list", "-R", f"{owner()}/{t['repo']}",
        "--workflow", t["workflow"], "-L", "1",
        "--json", "url,status", "--jq", ".[0].url",
    ])
    print(f"Run: {out.strip()}" if code == 0 and out.strip() else "Dispatched (run URL not yet available).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Dispatch strands release workflows.")
    p.add_argument("--owner", help="GitHub org/user owning the repos (default: $OWNER or strands-agents).")
    # Not required: a bare invocation defaults to `status` (all targets) —
    # the natural entry point for the release flow.
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("status", help="Show published version + commits since, per target.")
    s.add_argument("targets", nargs="*", help="Targets to show (default: all).")
    s.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    d = sub.add_parser("dispatch", help="Dispatch a target's release workflow.")
    d.add_argument("target", choices=list(TARGETS))
    d.add_argument("version", help="Bare semver to release, e.g. 0.3.0.")
    d.add_argument("--preview", action="store_true", help="Dry run: build + inspect only, no approval/publish.")
    d.add_argument("--sha", default="", help="Optional commit SHA (must be an ancestor of main).")

    args = p.parse_args()
    if args.owner:
        os.environ["OWNER"] = args.owner

    if args.cmd in (None, "status"):
        # Bare invocation → status for all targets.
        names = getattr(args, "targets", None) or list(TARGETS)
        unknown = [n for n in names if n not in TARGETS]
        if unknown:
            print(f"error: unknown target(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
        rows = status(names)
        if getattr(args, "json", False):
            print(json.dumps(rows, indent=2))
        else:
            print_status_human(rows)
        return 0

    if args.cmd == "dispatch":
        return dispatch(args.target, args.version, args.preview, args.sha)

    return 0


if __name__ == "__main__":
    sys.exit(main())
