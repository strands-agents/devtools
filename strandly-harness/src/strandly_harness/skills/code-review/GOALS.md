# Goals: code-review

Critic acceptance criteria when the `code-review` skill is active. Verify each with tools
(read the transcript's spawns, re-read the posted review, spot-check findings against the code) —
do not trust the narrative.

- **The pipeline actually ran as stages, not one blob.** The transcript shows a `triage` pass
  first, then independent specialist `spawn`s, then an aggregation pass before any post. A single
  in-context review with no independent passes does NOT clear this skill.
- **Routed passes were spawned by default, and any inline pass read its role file.** The passes
  triage routed should mostly show up as independent `spawn`s in the transcript — that independence
  is the point. Skipping a pass triage marked irrelevant is fine (routing), not a failure. Running a
  pass *inline* instead of spawning is acceptable only for a genuinely small/trivial change, and
  only if the transcript shows the role file was `Read`/`cat`'d first (approximating a pass from
  memory is a failure). A larger PR whose specialist passes were all self-run in the orchestrator's
  biased context — with no independent spawns — does not clear this skill.
- **Triage gated and routed.** There is an explicit go/no-go and a routing decision (which passes
  were run and which were skipped, with a reason). Running every pass on a docs-only or trivial PR
  is a failure of routing, not thoroughness.
- **Context was built before judging** for any non-trivial PR — callers/callees/related tests/repo
  rules were gathered and carried into the specialist passes, not reviewed diff-in-isolation.
- **Each specialist finding is grounded** in `file:line`; every 🔴 blocker has a runnable repro or a
  deterministic file:line safety failure. Spot-check at least one finding against the actual code —
  if it doesn't hold, that's a false positive and a critic failure.
- **Aggregation/suppression happened.** Findings were de-duplicated, ranked by severity×confidence,
  low-confidence/ungrounded ones were dropped, and a comment budget was respected. A raw dump of
  every pass's output is a failure.
- **Precision over recall.** No posted comment is a pedantic nit led with, a restatement of the
  code, generic best-practice advice, or a style point a linter would catch. If you can find a
  posted comment that fails the value gate, the skill did not clear.
- **The output is a coherent review:** a TL;DR verdict (approve / changes-requested), severity-
  tagged inline findings with `suggestion` blocks where a fix exists, and a collapsed per-pass
  breakdown — not a wall of text.
- **No re-nagging** on a PR update: only the new diff was reviewed; resolved/dismissed threads were
  not repeated.
- **Public-repo safety:** posting to a repo the agent doesn't own was confirmed or explicitly
  authorized first.
