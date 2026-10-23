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
prompts live in the CLAUDE.md driver that calls this. Three subcommands:

  release_dispatch.py status [target ...] [--json]
      For each target: latest published version, whether there are new commits
      on main since that release (monorepo targets: only commits touching the
      package dir), the commit subjects, and the bump conventional commits
      imply (informational — the releaser still types the version). `--json`
      emits a machine-readable array for the driver to render.

  release_dispatch.py dispatch <target> <version> [--preview] [--sha SHA]
      Validate the version (bare semver, greater than published), dispatch the
      target's release workflow, and print the run URL. Real release by default
      (dry_run=false → runs the suite, then waits for release-gate approval);
      --preview sets dry_run=true (build + inspect only, no approval/publish).

  release_dispatch.py pending <run-url-or-id> ...
      For each dispatched run: published (with release URL), waiting at
      release-gate, in progress, or failed.

OWNER defaults to the upstream org `strands-agents`; override with --owner or
the OWNER env var for fork testing. Requires `gh` authenticated with the
`workflow` scope.
"""

from __future__ import annotations

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
# `workflow` is the dispatch file (harness-sdk has several, so never key on
# repo). `version` sources the latest published version: "pypi:<name>" or
# "npm:<name>" — the registry is authoritative and avoids malformed-tag
# surprises. The tag for the commits-since compare is `prefix + version`.
# `path` (monorepo targets only) scopes commits-since to the package dir.
# `inputs` are extra workflow_dispatch inputs beyond version/sha/dry_run —
# per-target differences live here as data, not as branches in dispatch().
# `dep_check` (optional) reports a dependency range in a file on main against
# another target's published version.
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

NO_INTEG = {"run_integ_tests": "false"}

TARGETS = {
    "harness-python": {
        "repo": "harness-sdk",
        "workflow": "release-python.yml",
        "prefix": "python/v",
        "version": "pypi:strands-agents",
        "path": "strands-py/",
        "inputs": NO_INTEG,
    },
    "harness-typescript": {
        "repo": "harness-sdk",
        "workflow": "release-typescript.yml",
        "prefix": "typescript/v",
        "version": "npm:@strands-agents/sdk",
        "path": "strands-ts/",
        "inputs": NO_INTEG,
    },
    "harness-py": {
        "repo": "harness-sdk",
        "workflow": "release-harness-py.yml",
        "prefix": "harness-python/v",
        "version": "pypi:strands-harness",
        "path": "harness-py/",
        "inputs": NO_INTEG,
    },
    "harness-ts": {
        "repo": "harness-sdk",
        "workflow": "release-harness-ts.yml",
        "prefix": "harness-typescript/v",
        "version": "npm:@strands-agents/harness",
        "path": "harness-ts/",
        "inputs": NO_INTEG,
    },
    "harness-cli": {
        "repo": "harness-sdk",
        "workflow": "release-harness-cli.yml",
        "prefix": "harness-cli/v",
        "version": "npm:@strands-agents/cli",
        "path": "strands-cli/",
        "inputs": NO_INTEG,
        "dep_check": {
            "file": "strands-cli/package.json",
            "dep": "@strands-agents/harness",
            "target": "harness-ts",
        },
    },
    "mcp": {
        "repo": "harness-sdk",
        "workflow": "release-mcp.yml",
        "prefix": "mcp/v",
        "version": "pypi:strands-agents-mcp-server",
        "path": "strands-mcp/",
        "inputs": {},  # release-mcp.yml has no run_integ_tests input
    },
    "shell": {
        "repo": "shell",
        "workflow": "release.yml",
        "prefix": "v",
        "version": "pypi:strands-shell",
        "inputs": {},
    },
    "tools": {
        "repo": "tools",
        "workflow": "release.yml",
        "prefix": "v",
        "version": "pypi:strands-agents-tools",
        "inputs": NO_INTEG,
    },
    "evals": {
        "repo": "evals",
        "workflow": "release.yml",
        "prefix": "v",
        "version": "pypi:strands-agents-evals",
        "inputs": NO_INTEG,
    },
}

# Default repo for `pending` when given a bare run id instead of a URL.
DEFAULT_RUN_REPO = "harness-sdk"


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


def gh_json(args: list[str]):
    """Run a gh command expected to print JSON. Returns (data, error)."""
    code, out, err = gh(args)
    if code != 0:
        return None, (err.strip() or "gh failed").splitlines()[-1]
    try:
        return json.loads(out), ""
    except ValueError:
        return None, "could not parse gh output"


def path_shas(repo: str, path: str, since: str) -> set[str]:
    """SHAs of main commits touching `path` committed at/after `since`.

    The compare API can't filter by path, so the commits API (which can) is
    bounded by the tag commit's date and intersected with the compare range.
    """
    code, out, _ = gh(
        ["api", "--paginate",
         f"repos/{owner()}/{repo}/commits?sha=main&path={path}&since={since}&per_page=100",
         "--jq", ".[].sha"]
    )
    return set(out.split()) if code == 0 else set()


def commits_since(repo: str, tag: str, path: str = "") -> dict:
    """Compare `tag`..main via the GitHub API, optionally scoped to `path`.

    Returns {ahead: int, subjects: [str]} on success, or {error: str} if the
    tag is missing (never released at that version) or the compare fails.
    """
    data, err = gh_json(
        ["api", f"repos/{owner()}/{repo}/compare/{tag}...main",
         "--jq", "{ahead: .ahead_by, since: .base_commit.commit.committer.date,"
                 " commits: [.commits[] | {sha, subject: (.commit.message | split(\"\\n\")[0])}]}"]
    )
    if data is None:
        # 404 → tag not found on this owner (common on forks / first release).
        return {"error": err}
    commits = data.get("commits") or []
    if path:
        # The compare list caps at 250 commits; past that the path count is a
        # lower bound, which still answers "is there anything to release".
        touched = path_shas(repo, path, data["since"])
        commits = [c for c in commits if c["sha"] in touched]
        data["ahead"] = len(commits)
    # Reverse the compare order so the newest commits display first.
    return {"ahead": data["ahead"], "subjects": [c["subject"] for c in reversed(commits)]}


CC_RE = re.compile(r"^(?P<type>\w+)(\([^)]*\))?(?P<bang>!)?:")
BUMP_RANK = {"feat": ("minor", 2), "fix": ("patch", 1)}


def implied_bump(subjects: list[str]) -> dict:
    """The bump conventional commits imply: feat → minor, fix → patch.

    `!` is flagged as breaking rather than mapped to major — whether it needs
    a major is a judgment call (harness-sdk team/FEATURE_LIFECYCLE.md).
    """
    matches = [m for m in map(CC_RE.match, subjects) if m]
    ranked = [BUMP_RANK.get(m["type"], ("none", 0)) for m in matches]
    bump = max(ranked, key=lambda r: r[1], default=("none", 0))[0]
    return {"bump": bump, "breaking": any(m["bang"] for m in matches)}


def range_admits(spec: str, version: str) -> bool | None:
    """Whether an npm range (exact, ~x.y.z, ^x.y.z) admits `version`.

    None if the range isn't one of those simple forms.
    """
    m = re.match(r"^([~^]?)(\d+)\.(\d+)\.(\d+)$", spec.strip())
    if not m or not SEMVER_RE.match(version):
        return None
    op, base = m[1], tuple(int(x) for x in m.groups()[1:])
    v = tuple(int(x) for x in version.split("."))
    # Leading components that must match: exact=3, tilde=2, caret=first
    # non-zero component (^0.3.1 → 0.3.x, ^1.2.3 → 1.x).
    fixed = {"": 3, "~": 2}.get(op) or next((i + 1 for i, x in enumerate(base) if x), 3)
    return v[:fixed] == base[:fixed] and v >= base


def dep_status(t: dict) -> dict | None:
    """Range of `dep_check.dep` in `dep_check.file` on main vs. its published version."""
    check = t.get("dep_check")
    if not check:
        return None
    pkg, err = gh_json(
        ["api", f"repos/{owner()}/{t['repo']}/contents/{check['file']}?ref=main",
         "-H", "Accept: application/vnd.github.raw+json"]
    )
    deps = {**(pkg or {}).get("devDependencies", {}), **(pkg or {}).get("dependencies", {})}
    published = latest_published_version(TARGETS[check["target"]]["version"])
    spec = deps.get(check["dep"], "")
    return {
        "dep": check["dep"], "range": spec or f"? ({err or 'not found'})",
        "published": published, "admits": range_admits(spec, published) if spec else None,
    }


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
            cmp = commits_since(t["repo"], tag, t.get("path", ""))
            if "error" in cmp:
                row["has_changes"] = None
                row["note"] = cmp["error"]
            else:
                row["ahead"] = cmp["ahead"]
                row["has_changes"] = cmp["ahead"] > 0
                row["subjects"] = cmp["subjects"]
                row["implied"] = implied_bump(cmp["subjects"])
        else:
            row["has_changes"] = None
            row["note"] = "no published version found"
        dep = dep_status(t)
        if dep:
            row["dep_check"] = dep
        rows.append(row)
    return rows


def status_tail(r: dict) -> str:
    if r["has_changes"] is None:
        return f"  ({r.get('note', 'unknown')})"
    scope = f" in {TARGETS[r['target']]['path']}" if TARGETS[r["target"]].get("path") else ""
    if not r["has_changes"]:
        return f"  no new commits{scope} since {r['tag']}"
    imp = r["implied"]
    breaking = ", BREAKING (!) — check FEATURE_LIFECYCLE.md" if imp["breaking"] else ""
    return f"  {r['ahead']} new commit(s){scope} since {r['tag']}  [implied: {imp['bump']}{breaking}]"


def dep_line(d: dict) -> str:
    verdict = {True: "ok", False: "DOES NOT ADMIT latest", None: "can't evaluate"}[d["admits"]]
    return f"    dep {d['dep']} {d['range']} on main; published {d['published'] or 'n/a'} → {verdict}"


def print_status_human(rows: list[dict]) -> None:
    for r in rows:
        print(f"{r['target']:<20} latest: {r['version'] or 'n/a':<10}{status_tail(r)}")
        if "dep_check" in r:
            print(dep_line(r["dep_check"]))
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
        # Require a single-step increment (major, minor, or patch) so a typo
        # like 1.48.0 -> 1.84.0 fails here instead of at scan (or worse, at the
        # approver). Mirrors the scan-commits check in the release workflows.
        maj, minor, pat = (int(x) for x in published.split("."))
        valid = {f"{maj + 1}.0.0", f"{maj}.{minor + 1}.0", f"{maj}.{minor}.{pat + 1}"}
        if version not in valid:
            print(
                f"error: {version} is not a single increment of published {published} "
                f"(expected one of: {', '.join(sorted(valid))}).",
                file=sys.stderr,
            )
            return 2

    args = [
        "workflow", "run", t["workflow"],
        "-R", f"{owner()}/{t['repo']}", "--ref", "main",
        "-f", f"version={version}",
        "-f", f"dry_run={'true' if preview else 'false'}",
    ]
    if sha:
        args += ["-f", f"sha={sha}"]
    for key, value in t["inputs"].items():
        args += ["-f", f"{key}={value}"]

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


RUN_URL_RE = re.compile(r"github\.com/([^/]+/[^/]+)/actions/runs/(\d+)")


def release_url(repo: str, workflow_path: str, head_sha: str) -> str:
    """URL of the release whose tag (target's prefix) points at `head_sha`."""
    t = next((t for t in TARGETS.values() if workflow_path.endswith("/" + t["workflow"])
              and repo.endswith("/" + t["repo"])), None)
    if not t:
        return ""
    code, out, _ = gh(
        ["api", f"repos/{repo}/git/matching-refs/tags/{t['prefix']}",
         "--jq", f'.[] | select(.object.sha == "{head_sha}") | .ref']
    )
    tag = out.split()[-1].removeprefix("refs/tags/") if code == 0 and out.split() else ""
    return f"https://github.com/{repo}/releases/tag/{tag}" if tag else ""


def run_state(run: dict, repo: str) -> str:
    if run["status"] == "waiting":
        # Name the environment rather than assume release-gate.
        envs, _ = gh_json(["api", f"repos/{repo}/actions/runs/{run['id']}/pending_deployments",
                           "--jq", "[.[].environment.name]"])
        return f"waiting at {', '.join(envs or ['an environment'])} (approve on the run page)"
    if run["status"] != "completed":
        return f"in progress ({run['status']})"
    if run["conclusion"] != "success":
        return f"failed ({run['conclusion']})"
    url = release_url(repo, run["path"], run["head_sha"])
    return f"published → {url}" if url else "succeeded, no release tag found (preview?)"


def pending(runs: list[str]) -> int:
    rc = 0
    for ref in runs:
        m = RUN_URL_RE.search(ref)
        repo, run_id = (m[1], m[2]) if m else (f"{owner()}/{DEFAULT_RUN_REPO}", ref)
        run, err = gh_json(["api", f"repos/{repo}/actions/runs/{run_id}"])
        if run is None:
            print(f"{ref}: error: {err}")
            rc = 1
            continue
        print(f"{run['name']:<28} {run['html_url']}\n    {run_state(run, repo)}")
    return rc


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

    pd = sub.add_parser("pending", help="Show where dispatched release runs are (gate / published / failed).")
    pd.add_argument("runs", nargs="+", help=f"Run URLs or ids (bare ids default to {DEFAULT_RUN_REPO}).")

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

    if args.cmd == "pending":
        return pending(args.runs)

    return 0


if __name__ == "__main__":
    sys.exit(main())
