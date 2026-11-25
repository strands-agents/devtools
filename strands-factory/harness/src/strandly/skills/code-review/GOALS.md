# Goals: code-review

Verify with tools: transcript `subagent` calls, the posted review, one finding checked in the code.

- Routing names the passes run and why; none unwarranted or silently skipped.
- Routed passes ran as independent `subagent` calls, or from the read role file with the warm run
  disclosed. A non-trivial PR reviewed only in the orchestrator's context fails.
- The reviewer pass covered each dimension the diff touched: scope vs the issue thread, tests,
  model-facing surface, docs accuracy/parity/rendering, repo conventions.
- Each posted finding has `file:line`, quoted evidence, the caller and input (for 🔴 and 🟡, a real
  caller via the public surface), expected and observed behavior, impact, and a respectful fix
  (`suggestion` block when concrete); optional advice stays optional. Each 🔴 shows the failure
  (repro or deterministic). The checked finding holds.
- Repro or source-only evidence with revisions, controls and limits is collapsed in the same
  comment. No invented runs; material limits stay visible.
- Inline findings are deduped and tiered; ⚪, pre-existing, unreachable, "for the record",
  nothing-to-do, linter-owned and restatement items are not inline.
- The TL;DR has a verdict and collapsed detail; it and each finding fit 15 visible lines.
- A filed pre-existing-bug issue is maintainer-actionable, in the right repo, new and proportionate.
- Regression claims compare same-input base/head probes plus a success control, or leave
  attribution unverified. A failed prerequisite is a repro only if it is the tested operation.
  Coverage mutants are caught by a probe and missed by existing tests.
- Behavioral reviews (runtime-protecting test changes too) cover affected-package suites,
  boundaries, failures, sequences and counted stability/concurrency runs. Claims stay within what
  ran; user limits, blocked checks and untested configurations are stated.
- Updates don't re-nag; a fixes-only follow-up leads with previous-finding status.
- Posting to a repo the agent doesn't own was confirmed first.
- Exactly one valid `work-product` block last: `kind: "review"`, posted and suppressed findings,
  `evidence_ledger`.
