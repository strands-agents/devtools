---
name: implement
description: >
  Implement a code change end to end — ground in the repo's rules, ship tests with the change, pass
  local gates, get ONE independent fresh-context review, fix, and open the PR. TRIGGER when asked
  to "implement", "build", "add", "fix", or "code" a feature/bugfix, or when handed an issue to turn
  into a PR. SKIP for pure reviews (code-review), cross-language ports (port), and trivial one-line
  edits where a review pass is overhead.
allowed-tools: shell read write edit use_github subagent
---

# Implement

Ground → plan → code + tests → local gates → one independent review → fix → PR. The review is the
contract: your context is biased by having written the code, so you don't approve your own work.

1. **Ground.** Read the issue *and all comments* (later comments change the spec). Read `AGENTS.md`
   (root + nested near the files you'll touch) and `CONTRIBUTING`: commands, conventions,
   architecture rules. `rg` for existing helpers and tests in the area — reimplementing what exists
   is a guaranteed finding. Ambiguity that changes the design → ask before building.
2. **Plan** in a few lines: files, behaviour change, the tests that prove it, risks. No separate
   planning subagent.
3. **Code + tests.** Match the repo, not your habits. Every changed behaviour gets a test that fails
   without the change. Smallest diff that solves the task — no drive-by refactors, speculative
   knobs, helpers for two-line bodies, or narrating comments (≤~10% of the diff, ≤2 lines each).
   Changed an architecture rule or convention → update `AGENTS.md` with it.
4. **Local gates green** — the repo's own test/lint/type-check commands — before spending a
   reviewer. Keep the output; the PR body cites it.
5. **One independent review.** Commit to a branch, then run
   the `code-review/reviewer` role (`agent_type`) as a `subagent` with: branch + base, the issue link and acceptance
   criteria, and the gate output. Add the `code-review/api-bar-raiser` role
   (`model="advanced"`) only if you changed a public API; `code-review/adversarial-tester` only for risky
   logic (parsing, concurrency, error paths). Run them in parallel.
6. **Fix and re-verify.** Fix every real finding (plus the test that would have caught it); re-run
   the gates. Disagree with a finding → write the reason in the PR body, don't drop it silently.
   A **second** review round only if round one had a 🔴; never more than two. Residual open
   points, and any review pass that couldn't complete, go in the PR body — never silently skipped.
7. **PR.** Push, open the PR (`Fixes #N`), fill the repo's PR template. Body: what changed and why,
   gate output, review findings fixed / disputed / open. Confirm before pushing to a repo you don't
   own.
