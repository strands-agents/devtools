# Goals: implement

Critic acceptance criteria when the `implement` skill is active. Verify each with tools (read the
transcript's spawns, read the branch/PR, re-run the gates if cheap) — do not trust the narrative.

- **Grounding happened before coding.** The transcript shows the repo's own rules (AGENTS.md /
  CONTRIBUTING) and the surrounding code were read *before* the first edit. A change that violates
  a rule AGENTS.md states (commands, conventions, architecture rules) is a grounding failure.
- **Tests shipped with the change.** Every new/changed behavior has a test that would fail without
  the change — spot-check at least one by reading it against the diff. "Implementation now, tests
  later" does not clear this skill (unless the task itself was explicitly test-exempt and that was
  stated).
- **Local gates ran green before the first review spawn.** The transcript shows the repo's own
  test/lint commands executed with passing output *before* any reviewer was spawned. Spending a
  reviewer on a red branch is a failure.
- **The review loop actually ran with independent, fresh-context reviewers.** At least one
  reviewer `spawn` (with a code-review role prompt) appears in the transcript for a non-trivial
  change. A purely in-context self-review — the implementor re-reading its own diff and declaring
  it fine — does NOT clear this skill.
- **Findings were fixed and re-reviewed by a NEW spawn, not self-approved.** If round N returned
  findings, the transcript shows fixes + re-verification + a round-N+1 fresh reviewer spawn that
  saw those findings. The implementor marking its own fix as resolving the finding, with no
  follow-up reviewer, is a failure.
- **The loop exited on a legitimate condition:** reviewer APPROVE / no actionable findings,
  converged-stuck (overlapping findings across rounds, stated), or the 3-round cap — with residual
  findings carried visibly into the PR body, not dropped.
- **Disagreements are on the record.** Any reviewer finding the implementor rejected has a concrete
  written reason in the transcript/PR body. Silently dropped findings are a failure.
- **The PR body carries the evidence:** what changed, actual test/lint output, and the review-loop
  ledger (rounds, findings per round, fixed/disputed/open). An assertion-only PR body fails.
- **Scope stayed tight.** The diff solves the stated task; drive-by refactors and unrelated fixes
  are a failure of scope, not generosity.
- **Public-repo safety:** pushing/PRing to a repo the agent doesn't own was confirmed or explicitly
  authorized first.
