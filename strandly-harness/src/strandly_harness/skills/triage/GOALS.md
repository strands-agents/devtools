# Goals: triage

Critic acceptance criteria when the `triage` skill is active. This is a go/no-go *gate* run BEFORE
investing in implementation — its deliverable is a judgment, so it is often correctly a BYPASS at
the task level; still verify the judgment is sound and evidence-grounded.

- **A verdict is present:** one of `accept` / `defer` / `redirect` / `reject` / `escalate`.
- **The judgment is grounded in evidence the actor actually gathered** (issue/PR/code/docs it read),
  not speculation. The transcript shows a research-only pass — ideally a `spawn` with
  `system_prompt="skills/triage/assets/roles/triage.md"` — that questioned the premise: is the problem real, in
  scope, at the right layer, not already solved, and is the approach sound?
- **When the verdict is not `accept`, an alternative or next step is given** (what to do instead of
  building).
- **The gate was respected.** If triage returned `defer`/`redirect`/`reject`, the actor did NOT
  proceed to implement anyway — it stopped and acted on the verdict. (Implementing past a non-accept
  triage verdict is a RETRY-worthy gap.)
