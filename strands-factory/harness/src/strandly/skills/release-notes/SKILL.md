---
name: release-notes
description: >
  Curated release notes between two git refs: categorize merged PRs (Major Features / Major Bug
  Fixes / Minor), give every major feature a code example, and VALIDATE each example by running it
  — a failed validation gets an inline engineer-review marker, never a dropped feature. TRIGGER on
  "write/draft release notes", "prepare the release", or "what shipped between vX and vY". SKIP for
  a plain diff/changelog question with no release artifact wanted.
allowed-tools: shell read write edit use_github subagent
---

# Release Notes

GitHub already lists every PR; this skill writes the curated layer on top — the 3–8 features that
matter, each with a working, validated example, plus critical fixes.

Run the `release-notes/release-notes-writer` role (`agent_type`) as a `subagent` with the repo, base and head
refs, and anything the user flagged (features to highlight, an existing draft release). One pass.
Live-Bedrock validation follows the `e2e-test` boundary (`ManagedBy=<test-tag>`, `<test-prefix>-*`,
no IAM roles, clean up).

Principles: merged code is the source of truth (PR descriptions go stale under review); every
example is validated by a behavioural test that actually ran; a feature is never dropped over failed
validation — it ships with `⚠️ NEEDS ENGINEER VALIDATION` and the documented attempts; deliverables
live in the returned report, not the sandbox. Nothing is published unless the user says so.

You get back: validation evidence per feature, the release-notes markdown (ending in `---` for
GitHub's auto-notes), and exclusions with reasons — categorization stated so the user can reshuffle.
