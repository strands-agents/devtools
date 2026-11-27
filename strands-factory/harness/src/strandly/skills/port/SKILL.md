---
name: port
description: >
  Port a completed feature from one language to the other in the Strands SDK monorepo and open a
  review-ready PR with a traceability matrix and captured test output. TRIGGER when asked to
  "translate" or "port" a feature across languages, or given a translation issue/PR URL. SKIP for
  changes that aren't cross-language feature ports.
allowed-tools: shell read write edit use_github subagent
---

# Port

Reproduce the source feature's behaviour in the target language. The source tests are the spec.
Read `skills/port/references/guidance.md` once (construct mappings, gotchas) and the source and
target packages' `docs/PORTING.md` / `docs/TESTING.md`.

**Mode B — iterate on an existing port PR:** read the PR and *all* review comments (they are the
spec), fix, re-run the target tests, push. No full workflow.

**Mode A — new port:**

1. **Scope.** Read the issue and all comments. Identify the source files (implementation + unit
   and integration tests), target language, and constraints. Source not merged/tested/documented →
   stop and say so. Ambiguous → ask.
2. **Plan (you, inline, a few lines).** Where each source file lands, construct mappings, language
   gaps with a proposed resolution, implementation order. Check the target's conventions first
   (`rg` for the closest existing analog and match it).
3. **Implement.** Run the `port/implementer` role (`agent_type`) as a `subagent` with the plan, the source file
   paths, and the relevant guidance section; or do it yourself for <5 files. Every source test gets
   a target test; public symbols get house-style docstrings; user-facing docs the source has are
   mirrored. Lint/format before hand-off.
4. **Validate — independently.** Run the `port/validator` role (`agent_type`) as a `subagent` (must be a
   different agent than the implementer): source→target test matrix, tests/lint/type-check run with
   verbatim output, sensitive-surface diff. Translated code is still new code: for a non-trivial
   port also run the `code-review/reviewer` role (`agent_type`) as a `subagent` for correctness, and for a
   public API introduced/reshaped in the target, the `code-review/api-bar-raiser` role
   (`model="advanced"`) — all in parallel.
   FINDINGS → fix and re-validate once; then proceed with open questions listed.
5. **PR.** Branch `port/<feature>-to-<lang>`, fill the repo's PR template, link the source issue.
   Body (detail collapsed): traceability matrix (source test → target test → pass/fail; missing
   rows visible), captured runner output, structural map, decision log (deviations + why), new
   dependencies / capabilities, sensitive-surface diff, lint/type-check output, open questions.
   Confirm before pushing to a repo you don't own.

Translate, don't improve: no unrelated fixes, no redesign. Idiomatic over literal when confident;
otherwise literal and flagged. Learned a new mapping or gotcha? Append it to `guidance.md`.
