# Release dispatcher

Triggers the manual `workflow_dispatch` release workflows across the strands
repos from one place, reading state remotely (no clones). When the user says
"release" (or asks what's ready to release), drive the flow below.

`release_dispatch.py` owns all the mechanics — published versions (PyPI/npm),
commits-since (GitHub API), dispatch, run URL. Do the conversation around it;
never re-derive versions, tags, or `gh` commands yourself.

Targets: **harness-python**, **harness-typescript**, **shell**, **tools**,
**evals**. harness-python and harness-typescript both release from the
`sdk-python` monorepo via different workflow files.

## Before starting

Check `gh auth status` includes the `workflow` scope. If not, tell the user to
run `gh auth refresh -s workflow` and stop.

## Flow

1. **Status.** Run `./release_dispatch.py status`. Each line shows the target,
   its latest published version, and — on the right — whether there are new
   commits since the last release (with subjects), nothing new, or undetermined.
   Relay this and note which targets have nothing to release.

2. **Pick targets.** Ask which to release — any subset. Don't assume all; don't
   refuse a "nothing new" / undetermined target if the user insists (the
   workflow's own `scan-commits` is the real guard).

3. **Version per target.** State the current published version explicitly in
   the prompt — "harness-python is at **1.50.0**; what should the new version
   be?" — and ask the user to **type** the new version. Never auto-bump or
   suggest one. The script rejects anything that isn't a single major/minor/
   patch increment of the published version.

4. **Dispatch.** Confirm the exact command, then run
   `./release_dispatch.py dispatch <target> <version>`. This is a **real
   release**: the workflow runs the full test/build/inspect suite, then pauses
   for a reviewer to approve the `release-gate` environment before it tags and
   publishes — so the dispatch alone ships nothing. Give the user the printed
   run URL and tell them to approve there when ready. The script validates the
   version (bare semver, a single increment of the published version) and
   prints any error — surface it, don't work around it.

Repeat 3–4 per selected target, **one at a time**, confirming each.

## Options to offer when relevant

- Preview only (build + inspect, no approval/publish): add `--preview`.
- Release a specific commit (ancestor of main): `--sha <commit>`.
- Fork testing instead of upstream `strands-agents`: `--owner <user>`.

## Guardrails

- Confirm before every dispatch; one target at a time.
- The user types every version — never guess.
- If a run fails at `scan-commits`, it's usually: version not greater than the
  last release, no commits since, or a `--sha` not on main. Report the run's
  error rather than re-dispatching blindly.

---

Also here: **`notes/`** is the `release-notes` composite action (grouped-by-type
release notes), consumed by release workflows as
`strands-agents/devtools/scripts/release/notes@main`. Not to be confused with
`../strands_release_helper.py`, an older tool that clones + tests + auto-bumps
for the previous template-based release process.
