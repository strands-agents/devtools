# Release dispatcher

Triggers the manual `workflow_dispatch` release workflows across the strands
repos from one place, reading state remotely (no clones). When the user says
"release" (or asks what's ready to release), drive the flow below.

`release_dispatch.py` owns all the mechanics — published versions (PyPI/npm),
commits-since (GitHub API), dispatch, run URL, run state. Do the conversation
around it; never re-derive versions, tags, or `gh` commands yourself.

Targets (all but the last three release from the `harness-sdk` monorepo, each
via its own workflow file; commits-since counts only commits touching the
package dir):

| Target | Package | Dir |
|---|---|---|
| **harness-python** | PyPI `strands-agents` | `strands-py/` |
| **harness-typescript** | npm `@strands-agents/sdk` | `strands-ts/` |
| **harness-py** | PyPI `strands-harness` | `harness-py/` |
| **harness-ts** | npm `@strands-agents/harness` | `harness-ts/` |
| **harness-cli** | npm `@strands-agents/cli` | `strands-cli/` |
| **mcp** | PyPI `strands-agents-mcp-server` | `strands-mcp/` |
| **shell**, **tools**, **evals** | own repos | whole repo |

## Before starting

Check `gh auth status` includes the `workflow` scope. If not, tell the user to
run `gh auth refresh -s workflow` and stop.

## Flow

1. **Status.** Run `./release_dispatch.py status`. Each line shows the target,
   its latest published version, and — on the right — whether there are new
   commits since the last release (with subjects), nothing new, or
   undetermined, plus the bump conventional commits imply (`feat` → minor,
   `fix` → patch; `!` is flagged BREAKING). For harness-cli it also shows the
   CLI's `@strands-agents/harness` range on main against the latest published
   harness-ts and flags a range that doesn't admit it. Relay this and note
   which targets have nothing to release.

2. **Pick targets.** Ask which to release — any subset. Don't assume all; don't
   refuse a "nothing new" / undetermined target if the user insists (the
   workflow's own `scan-commits` is the real guard).

3. **Version per target.** State the current published version and the implied
   bump explicitly — "harness-python is at **1.50.0**; commits imply a
   **minor** — what should the new version be?" — and ask the user to **type**
   the new version. Never fill it in for them. On a BREAKING flag, point at
   harness-sdk `team/FEATURE_LIFECYCLE.md` (pay-for-play breaks can ship in a
   minor) and let the user decide. The script rejects anything that isn't a
   single major/minor/patch increment of the published version.

4. **Dispatch.** Confirm the exact command, then run
   `./release_dispatch.py dispatch <target> <version>`. This is a **real
   release**: the workflow runs the full test/build/inspect suite, then pauses
   for a reviewer to approve the `release-gate` environment before it tags and
   publishes — so the dispatch alone ships nothing. Give the user the printed
   run URL and tell them to approve there when ready. The script validates the
   version (bare semver, a single increment of the published version) and
   prints any error — surface it, don't work around it.

Repeat 3–4 per selected target, **one at a time**, confirming each.

5. **Track.** `./release_dispatch.py pending <run-url> ...` prints each run as
   published (with release URL), waiting at its gate, in progress, or failed.

## After the batch

In this order, once every gate in the batch is approved and published:

1. **Changelog.** Run `gh workflow run changelog-sync.yml -R
   strands-agents/harness-sdk` **once** for the whole batch, then merge the
   single `docs(changelog): sync release notes` PR it opens. (tools is not a
   changelog source.)
2. **Docs.** Run `gh workflow run docs-deploy-github-pages.yml -R
   strands-agents/harness-sdk --ref main` — without `skip_changelog_check` —
   then confirm <https://strandsagents.com/changelog/> returns 200.
3. **Cleanup.**
   - Stale gate runs: a run publishes the commit it **started** on; approving
     it later never picks up newer merges. Cancel it and re-dispatch instead.
   - Stale `changelog-sync` PRs: close any older than the one you merged.
   - Old announcement discussions are closed automatically by the release
     workflows (being added in a separate harness-sdk PR) — nothing to do.

## Options to offer when relevant

- Preview only (build + inspect, no approval/publish): add `--preview`.
- Release a specific commit (ancestor of main): `--sha <commit>`.
- Fork testing instead of upstream `strands-agents`: `--owner <user>`.

## Guardrails

- Confirm before every dispatch; one target at a time.
- The user types every version — the implied bump is a hint, never the answer.
- If a run fails at `scan-commits`, it's usually: version not greater than the
  last release, no commits since, or a `--sha` not on main. Report the run's
  error rather than re-dispatching blindly.
- If a run fails **after** `scan-commits` (e.g. a test), it needs a code fix:
  land it, then re-dispatch from the new main. Re-running the failed run
  rebuilds the same commit and fails the same way.

---

Also here: **`notes/`** is the `release-notes` composite action (grouped-by-type
release notes), consumed by release workflows as
`strands-agents/devtools/scripts/release/notes@main`. Not to be confused with
`../strands_release_helper.py`, an older tool that clones + tests + auto-bumps
for the previous template-based release process.
