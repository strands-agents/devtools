# What we actually review for — the Strands review taxonomy

> Source: the PR-review behaviour deep-dive (#341) — every PR our two review voices touched across
> the `strands-agents` org, classified into a 22-category taxonomy. **448 PRs · 2,584 records ·
> 1,528 comments classified.** This is the empirical rubric for *what a Strands reviewer cares
> about*. Use it to weight the specialist passes and to keep the reviewer "in voice".

## The headline: we are design-led, not lint-led

Design-judgment verticals (scope / API / DevX / meta-reasoning / design-alternatives) are ≈ **34%**
of our comments. Pure correctness is ≈ 9%; style is **1.3%**. A reviewer that leads with style nits
and line-by-line mechanics is *not* reviewing the way this team reviews. Lead with **scope, API
shape, and "do we even need this"**; defer formatting to linters.

## Top verticals (% of 1,528 classified comments)

| # | Vertical | Share | Maps to pass |
|---|---|---|---|
| 1 | Process / Orchestration / Status | 17.1% | orchestrator (status, ✅/🔴 tables, closing the loop) |
| 2 | Docs / Wording & Accuracy | 10.9% | `docs-accuracy` |
| 3 | Clarifying Questions | 9.9% | triage / orchestrator (ask, don't assume) |
| 4 | Meta-Reasoning / Scope & Layering | 9.6% | `triage` |
| 5 | API Shape & Naming (DevX) | 9.3% | `api-bar-raiser` pass |
| 6 | Design Alternative / Counter-Proposal | 7.5% | `api-bar-raiser` pass (generate alternatives) |
| 7 | Maintainability / DRY | 5.3% | `code-review` |
| 8 | Correctness / Logic Bug | 4.5% | `code-review` / `adversarial` |
| 9 | Adversarial / Edge-Case | 3.4% | `adversarial-tester` pass |
| 10 | API Surface / Encapsulation | 3.1% | `api-bar-raiser` pass |

Long tail: TEST 2.6% · APPROVE 2.6% · COMPAT 2.5% · OPTIN 2.4% · ERROBS (error/observability) 2.1% ·
**LLMINFO 1.4%** · STYLE 1.3% · SEC 1.2% · PORT 1.0% · PARITY 1.0% · DEP 0.8% · PERF 0.7%.

## The distinctive vertical: LLMINFO

**"Does the *model* get enough context?"** — is the tool result, the tool/param description, the
event nesting, the error surfaced to the model actually enough for an agent to act on? Generic
review bots don't have this lens; it is *core to reviewing an agent SDK*. We made it its own pass
(`llm-context.md`). Punch above its 1.4% historical share — it's under-counted because most tools
can't even see it.

## The two voices (both are us; a good reviewer carries both)

- **Taste / scope layer** (mkmeral's signature): leads with *socratic questions* (CLARIFY, META,
  APISHAPE, ALT), rarely hard-blocks, steers. → triage + API-bar-raising register.
- **Verification / evidence layer** (the agent's signature): owns most BUG findings and most
  blockers, runs adversarial passes, **closes the loop with SHAs / test counts / ✅🔴 tables**. →
  code-review + adversarial + the orchestrator's status discipline.

## Tone signature (how we frame, not just what we flag)

`socratic_question` 28.6% · `closing_loop` 22.4% · `status_report` 21.7% · `evidence_backed` 20.3% ·
`directive` 19.8% · `hedged_opinion` 18.2%. Only **3.9% of findings are blockers** — we block
*rarely and deliberately*, prefer 🔴/🟡 tiers and `nit:` prefixes, and use hedged-socratic phrasing
for design points, reserving directives for nits and clear decisions.

## Encoded stances (recurring rules worth applying every review)

- New tools → a **community package, not core**.
- **Minimise public surface**; force keyword-only args for forward-compat.
- **No breaking changes** without explicit justification.
- **Opt-in over forced defaults.**
- Tests must **prove behaviour**, not echo mocks.
- Docs are **example-first**.
- A self-resolved review thread is **not** a decided one (check provenance before treating it as
  addressed).

## How to use this file

The orchestrator and the aggregator should weight findings by this taxonomy: a well-grounded
API-shape / scope / LLMINFO finding is *more* on-brand and usually higher-value than a correctness
nit, and far above a style point. Calibrate the comment budget accordingly.
