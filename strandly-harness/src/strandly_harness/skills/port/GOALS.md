# Goals: port

Critic acceptance criteria when the `port` skill is active. Verify each with tools (read the
transcript's spawns, re-read the PR/branch, spot-check the traceability matrix against the code) —
do not trust the narrative.

- **Only a completed feature was ported.** The source was merged, tested, and documented before the
  port started. Porting an incomplete/unmerged source is a failure — the skill should have stopped.
- **Behavior was preserved, not "improved."** The translation reproduces source behavior as-is; it
  did not redesign, fix unrelated code, or add features. Idiomatic-over-literal is fine; silent
  behavior changes are not.
- **Validation was independent.** The translation-equivalence check (source→target test mapping) and
  the general code review came from a **different agent** than the implementer — not the same
  context that wrote the code. A self-validated port does NOT clear this skill.
- **General review actually ran, not just equivalence.** For a non-trivial port, the correctness
  reviewer was spawned; and when the port introduces/reshapes a public API in the target language,
  the api-bar-raiser was spawned (its cross-language pattern check is the point). Translated code is
  new code — a source→target mapping alone is not a review.
- **The traceability matrix is real and grounded.** Every source test maps to a target test (or is
  an explicit `missing-behavior` finding) with `file:line`s that actually exist. Spot-check one row
  against the code — if it doesn't hold, that's a critic failure.
- **Evidence is captured verbatim, not asserted.** Test/lint/type-check output in the PR is the real
  runner output, and any new sensitive surface (credentials/network/subprocess/deserialization not
  in the source) is flagged.
- **The PR is review-ready.** It carries the review artifacts (traceability matrix, captured output,
  structural map, decision log, dependency/capability delta, sensitive-surface diff, open questions)
  so a human can approve the translation with confidence — not a bare diff.
- **Public-repo safety:** posting/pushing to a repo the agent doesn't own was confirmed or
  explicitly authorized first.
