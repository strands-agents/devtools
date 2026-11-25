---
name: github
description: >
  Be a good citizen of someone else's repo: fill the target repo's issue TEMPLATE before opening an
  issue (never a bare ad-hoc body), comment with substance, and recommend labels from the repo's own
  set. TRIGGER before any GitHub write you compose yourself — filing an issue, commenting, applying
  labels. SKIP for pure reads, and when a more specific skill owns the interaction (code-review
  posts its review; first-response drafts its reply).
allowed-tools: shell read write edit use_github
---

# GitHub etiquette

Everything you write is public and attributed to a repo you usually don't own. Match its
conventions.

**Filing an issue.** Query `repository.issueTemplates { name about title body }`. If empty, list
`.github/ISSUE_TEMPLATE/` on disk — the query misses YAML issue forms; a form's `body:` fields are
the sections. Pick the template matching the kind, apply its title prefix, fill *every* section
("n/a" rather than deleting), keep its labels, invent nothing. Bare title + body only when both
sources are confirmed empty (for a bug: expected vs actual, minimal repro, environment).

**Commenting.** Add value or don't post: a concrete repro, fix, risk, or the exact info you need.
One consolidated comment, in the repo's register.

**Labels** come from `repository.labels`, never invented; recommend them, apply only when the task
authorises it.

**Confirm before writing** to a repo you don't own unless told to proceed.
