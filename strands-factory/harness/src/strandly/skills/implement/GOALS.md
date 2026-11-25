# Goals: implement

Verify with tools (transcript, branch/PR, re-run gates if cheap).

- AGENTS.md / repo rules were read before the first edit; the change doesn't violate a stated rule.
- Every changed behaviour has a test that fails without the change (spot-check one).
- Local gates ran green before the reviewer subagent.
- At least one independent reviewer `subagent` for a non-trivial change; a 🔴 finding was fixed and
  re-checked (second round) rather than self-approved. No more than two rounds.
- Rejected findings have a written reason in the PR body; no silent drops.
- PR body carries gate output and the findings ledger; diff scope matches the task.
- Pushing/PRing to a repo the agent doesn't own was confirmed first.
