---
name: release-notes
description: >
  Generate curated release notes between two git refs: categorize merged PRs (Major Features /
  Major Bug Fixes / Minor), give every major feature a code example, and VALIDATE every example by
  actually running it — behavioral assertions, not syntax checks; a failed validation gets an
  inline engineer-review marker, never a dropped feature. TRIGGER when asked to "write/draft
  release notes", "prepare the release", or summarize "what shipped between vX and vY". SKIP for a
  plain diff/changelog question with no release artifact wanted (GitHub's auto-notes already list
  every PR). Produces markdown ready to prepend to GitHub's auto-generated notes + the validation
  evidence.
allowed-tools: bash file_editor use_github think spawn
---

# Release Notes

Use this to turn "what merged between `<base>` and `<head>`" into notes a user actually wants to
read. GitHub's auto-generated release notes already list *every* PR ("What's Changed" + "New
Contributors") — this skill's job is the curated layer on top: the 3–8 features that matter, each
with a **working, validated** code example, and the critical fixes. The differentiator is
validation: an example that was never run is a guess wearing a code fence.

## How to run it

Spawn a subagent with the writer role — the procedure is long and benefits from a fresh context
that isn't carrying the rest of your session:

```
spawn(
  prompt="Draft release notes for <owner>/<repo> between <base-ref> and <head-ref>. <Anything the "
         "user flagged: features to highlight, a draft release that already exists, deadline>.",
  system_prompt="skills/release-notes/assets/roles/release-notes-writer.md",
)
```

The subagent inherits the harness toolset: `use_github` for releases/compare/PR metadata, `bash` +
`file_editor` to clone the repo at `<head-ref>` and *run* the example validations in the sandbox.
Validating examples against live Bedrock is common for Strands features — that works when the
sandbox carries the scoped CI AWS credentials and **must** follow the `e2e-test` skill's enforced
boundary (`ManagedBy=strandly` tags, `strandly-managed-*` bucket names, no IAM roles, delete what
you create).

## The four principles (the skill in brief)

1. **Merged code is the source of truth.** PR descriptions are written at open time and go stale
   under review — cross-reference review threads and the final diff before trusting any example or
   claim from a description.
2. **Validation is mandatory.** Every example gets a behavioral test (asserts outputs, state,
   types — not "it imports") that is actually run. Try Bedrock, deps, mocks, and simplification
   before giving up.
3. **Never drop a feature over failed validation.** After *documented* attempts, ship the example
   with an inline `⚠️ NEEDS ENGINEER VALIDATION` marker + what was tried and the real error.
4. **Deliverables live in the report, not the sandbox.** The sandbox is ephemeral — validation
   code, the notes, and exclusions all go into the returned report (and to GitHub only if the user
   asks).

## What you get back

Three blocks: (1) **validation evidence** — per-feature test code + pass output, or the documented
failed attempts; (2) the **release notes markdown** — `## Major Features` (prose + example each),
optional `## Major Bug Fixes`, ending with a `---` separator for GitHub's auto-notes; (3)
**exclusions** — anything demoted/omitted and why. The categorization is stated explicitly so the
user can re-shuffle ("move #123 to Major Features") and re-run. Nothing is published — it drafts;
posting the release (or a comment) happens only when the user says so.
