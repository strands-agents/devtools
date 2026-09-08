---
name: implement
description: >
  Implement a code change end to end — from a task/issue to a review-converged, PR-ready branch.
  It grounds itself in the repo's own rules (AGENTS.md / CONTRIBUTING) before writing anything,
  ships tests with the change, runs local readiness (tests + lint), and then enters an
  INDEPENDENT REVIEW LOOP: spawn a fresh-context reviewer, fix every finding, spawn another fresh
  reviewer, and repeat until the reviewer is satisfied (converged) or the iteration cap is hit.
  TRIGGER when asked to "implement", "build", "add", "fix", or "code" a feature/bugfix, or when
  handed an issue to turn into a PR. SKIP for pure reviews (use code-review), cross-language ports
  (use port), or trivial one-line edits where a review loop is overhead.
allowed-tools: bash file_editor use_github think spawn
---

# Implement

You are the **implementor** — this is the skill for turning a task into a merged-quality change.
The shape is: *ground → plan → implement (with tests) → verify locally → independent review loop →
PR*. The part that makes this skill worth having is the back end: your own context is **biased by
having written the code**, so the change is not done when *you* think it's done — it's done when a
**fresh-context reviewer with no stake in the code** stops finding real problems. That convergence
loop is this skill's contract; skipping it is the #1 way this skill silently degrades.

## The one rule that matters most

**You may not approve your own work.** Every non-trivial change goes through at least one
independent, fresh-context review pass before a PR is opened, and every real finding gets fixed and
re-reviewed by *another* fresh reviewer — not by you re-reading your own fix. The implementor's
warm context is exactly the context that misses its own bugs.

## Workflow

```
1. Ground        — read the task AND the repo's own rules (AGENTS.md first)
2. Plan          — small written plan: files, behavior, tests, risks
3. Implement     — code + tests, matching repo conventions
4. Verify        — run the repo's own gates locally (tests, lint, type-check)
        │
5. Review loop   — spawn fresh-context reviewer(s)          ┐
        │  findings? → fix → verify → spawn ANOTHER reviewer ┘  (repeat)
        ▼  clean / converged / cap hit
6. Finish        — branch pushed, PR opened, loop history reported
```

### 1. Ground (always, first — before writing any code)

- **Read the task fully.** The issue body AND all comments (`use_github`) — later comments often
  change the spec. Extract: what to build, acceptance criteria, constraints, out-of-scope.
- **Read the repo's own rules.** `AGENTS.md` at the repo root (and any nested ones near the files
  you'll touch), plus `CONTRIBUTING.md` / `README` build-and-test sections. These are the source of
  truth for conventions, commands, and architecture rules — a change that fights them will bounce
  in review no matter how correct it is. Find them:

  ```
  bash("find . -maxdepth 3 -iname 'AGENTS.md' -o -iname 'CONTRIBUTING*' | head")
  ```

- **Read the neighborhood.** Before adding anything, `rg`/`grep` for existing helpers, similar
  features, and the tests that cover the area — reimplementing something that exists is a
  guaranteed review finding.
- If the task is ambiguous on a point that changes the design, **ask before building** — one
  clarifying question beats a wrong implementation and a wasted review cycle.

### 2. Plan

Write a short plan before coding (a few lines is fine for a small change): files to touch, the
behavior change, **which tests will prove it**, and known risks. For a large/multi-file task,
consider spawning the plan as its own pass so the design gets fresh eyes before you're invested:

```
spawn(
  prompt="Plan the implementation of <task>. Repo rules: <AGENTS.md highlights>. Constraints: "
         "<...>. Produce: files to create/change, the approach, the test plan, risks/alternatives.",
)
```

### 3. Implement — code AND tests

- **Match the repo, don't import your habits.** Style, structure, naming, and error handling come
  from the surrounding code and AGENTS.md, not from generic best practice.
- **Tests are part of the change, not a follow-up.** Every new/changed behavior gets a test that
  would fail without the change. No test → the change isn't done. If the repo has a stated testing
  convention (fakes vs mocks, no-network, fixtures), follow it exactly.
- **Smallest change that solves the task.** No drive-by refactors, no speculative knobs, no
  unrelated fixes — they blow up the review diff and stall convergence.
- **Document the why.** A non-obvious choice gets a comment/docstring; if the repo keeps a living
  guide (AGENTS.md), update it when your change alters an architecture rule or convention.

### 4. Verify locally (before ANY review)

Run the repo's own gates — the commands AGENTS.md/CONTRIBUTING name (e.g. `pytest`, `ruff check .`,
a type-checker, a build). All of them must pass **before** you spend a reviewer on the change.
Sending a red branch into the review loop wastes the loop's iterations on things a command would
have caught. Capture the actual output — the PR body and the reviewer prompt both cite it.

### 5. Independent review loop (the contract)

Commit the work to a feature branch, then loop:

1. **Spawn a fresh-context reviewer.** Reuse the review skill's roles — don't invent a rubric:

   ```
   spawn(
     prompt="Independent review of branch <branch> implementing <task / issue link>. Acceptance "
            "criteria: <...>. Local gates already pass: <test/lint output summary>. Read the "
            "diff against <base> cold — re-derive the reasoning, don't trust the author's. "
            "Verdict: APPROVE or CHANGES REQUESTED, with itemized file:line findings.",
     system_prompt="skills/code-review/assets/roles/reviewer.md",
   )
   ```

   For a change with a **public API surface**, also spawn
   `skills/code-review/assets/roles/api-bar-raiser.md` (`model="advanced"`); for **risky logic**
   (parsing, concurrency, error paths), also spawn
   `skills/code-review/assets/roles/adversarial-tester.md` (`model="advanced"`). For a full-blown
   change, activating the whole `code-review` skill pipeline is the deluxe path — the minimum bar
   is one independent correctness pass.

2. **Triage the findings.** Fix every real finding (and add the test that would have caught it);
   for a finding you *disagree* with, write down the concrete reason — it goes in the next
   reviewer's prompt and the PR body, it doesn't just get dropped silently.

3. **Re-verify** (step 4 gates again), commit, and **spawn ANOTHER fresh reviewer** — include the
   previous round's findings + what you changed, so the new reviewer can check the fixes *and*
   look for regressions. Do not reuse the previous reviewer's context; fresh eyes each round is
   the mechanism.

4. **Exit when converged**, whichever comes first:
   - **Satisfied:** the reviewer returns APPROVE / no actionable findings → done.
   - **Converged-stuck:** a round's findings substantially overlap the previous round's (you're
     re-litigating, not improving) → stop, carry the open points into the PR body as known
     tradeoffs.
   - **Cap:** hard cap **3 review rounds**. Cost matters; a change that can't converge in 3
     independent passes needs a human, not a fourth pass. File the residual findings on the PR.

**A reviewer spawn can fail transiently** — retry it once or twice (a retry is a fresh subagent).
If it still fails, say so in the PR body rather than silently skipping the loop.

### 6. Finish

- Push the branch, open the PR (`use_github`), linking the issue (`Fixes #N`).
- The PR body reports, with evidence: what changed and why, the test/lint output, and the **review
  loop ledger** — rounds run, findings per round, fixed vs disputed vs open. A PR that states what
  was independently verified is trusted; one that only asserts is not.
- **Confirm before pushing/PRing to a repo you don't own**, per the global contract.

## Principles (the whole skill in six lines)

1. **Ground before code.** AGENTS.md and the neighborhood first — convention violations are
   self-inflicted review rounds.
2. **Tests ship with the change.** A behavior without a failing-before/passing-after test isn't
   implemented, it's typed.
3. **Green before review.** Never spend an independent reviewer on what `pytest`/`ruff` would catch.
4. **Independent review is mandatory, iterated, and fresh each round** — fix, then a *new*
   reviewer verifies; never self-approve a fix.
5. **Converge or cap (3 rounds).** Overlapping findings = stop; residuals go on the PR, visibly.
6. **Smallest diff that solves the task.** Scope creep is convergence poison.
