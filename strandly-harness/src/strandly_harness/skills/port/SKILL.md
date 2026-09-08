---
name: port
description: >
  Port a completed feature from one language to another in the Strands SDK monorepo and open a
  review-ready PR with evidence artifacts. TRIGGER when the user asks to "translate" or "port" a
  feature across languages, or provides a translation request issue/PR URL. SKIP
  for changes that aren't cross-language feature ports.
allowed-tools: bash file_editor use_github think spawn
---

# Port

Translate a source feature (code, tests, docs) into a target language and produce a PR with review
artifacts that let a human approve the translation with confidence and speed.

## Determine the mode

### Mode A: New translation

The input is a GitHub issue URL (typically `[PORT]` prefix) or natural language describing what to
translate.

1. Read the issue body with `use_github` — extract source files, target language, instructions
2. **Read ALL comments on the issue** — they contain additional context, clarifications, and
   constraints added after filing
3. If no issue URL, use `bash`/`file_editor` to locate source files from the description

Extract: **(a)** source files, **(b)** target language, **(c)** instructions/constraints. If
ambiguous, ask before proceeding.

Source files to port are the feature's implementation and its tests — unit and integration. Read
`docs/TESTING.md` in the source and target packages for test layout and execution instructions.

**Then:** Run the full workflow below.

### Mode B: Iterate on an existing translation PR

The input is a GitHub PR URL with review feedback to address.

1. Read the PR description — understand what was translated and its current state
2. **Read ALL review comments and inline comments** — these are the spec for this iteration
3. Read the current diff to understand what exists

Extract: **(a)** what needs to change, **(b)** what's fine as-is, **(c)** any new constraints from
reviewers.

**Then:** Address the feedback directly. Fix the issues, re-validate what changed, push to the PR.
No need to re-run the full 5-phase workflow.

## Translation guidance

Before starting, read the guidance file:

```
bash("find / -name 'guidance.md' -path '*port/references*' 2>/dev/null")
```

It contains language-pair construct mappings, known gotchas, and convention discovery instructions.
Read it once; apply the relevant language-pair section throughout. If your pair isn't covered,
proceed with universal rules and flag the gap.

## Workflow (Mode A only)

Five phases. For each, you decide: do it yourself or spawn a subagent with the corresponding role
prompt at `skills/port/assets/roles/<role>.md`.

```
Plan → Implement → Validate ─┐
         ▲                    │
         └── (findings) ──────┘
                   │
                   ▼ (pass)
              Document → Report → PR
```

### Spawn vs. inline

- **>5 files or >500 lines:** prefer spawning Plan and Implement
- **<5 files:** do it yourself — faster, less overhead
- **Validation must be independent:** the code and its validation must come from different agents

When spawning, pass file paths (not contents), the relevant guidance section, and task-specific
context. Keep every port phase on the **default model tier** — translation fidelity is correctness
work; don't downgrade a phase to `model="fast"` to save cost. Subagents have `file_editor` and `bash` — they read files themselves.

### Phase 1: Plan

Read source files, inspect target conventions, map constructs, identify language gaps, produce a
structured plan (files to create, construct mapping, decisions, implementation order).

**Role prompt:** `skills/port/assets/roles/planner.md`

Example spawn prompt (Plan phase, TS → Python):

```
spawn(
  prompt="""Plan the translation of the Anthropic model provider from TypeScript to Python.

## Translation guidance (TypeScript → Python)
| TypeScript | Python | Notes |
| interface (constructor param) | TypedDict | NOT dataclass |
| interface (with methods) | Protocol | structural subtyping |
| enum | StrEnum | Python 3.11+ |

Gotchas: TypedDict vs dataclass (TS interface as object literal → TypedDict);
No uuid v7 in Python < 3.14 — flag as language gap.

## Source files
- strands-ts/src/models/anthropic/index.ts
- strands-ts/src/models/anthropic/types.ts
- strands-ts/tests/models/anthropic.test.ts

## Target language
Python

## Instructions
Map to the existing BaseModelProvider ABC.
""",
  system_prompt="skills/port/assets/roles/planner.md",
)
```

### Phase 2: Implement

Execute the plan. Write idiomatic target code and tests. Run lint/format. Report what was created
and any decisions made.

**Role prompt:** `skills/port/assets/roles/implementer.md`

### Phase 3: Validate

Two complementary checks — keep them distinct:

1. **Translation equivalence (port-specific).** Map source tests → target tests (behavior
   traceability), run tests/lint/type-check, scan for sensitive surfaces. Produce PASS (with
   evidence) or FINDINGS. This is the check that's unique to a port — the source tests are the spec,
   and every source behavior must have a target counterpart.
   **Role prompt:** `skills/port/assets/roles/validator.md`

2. **General code review (delegate to the review skill).** Translated code is still new code: it can
   have correctness bugs, weak tests, or API-shape drift that a source→target mapping won't catch.
   Don't re-implement that judgment here — reuse it. Spawn the review passes directly:
   `spawn(system_prompt="skills/code-review/assets/roles/reviewer.md", ...)` for correctness, and —
   when the port introduces or reshapes a **public API** in the target language —
   `spawn(system_prompt="skills/code-review/assets/roles/api-bar-raiser.md", model="advanced", ...)`.
   The api-bar-raiser's cross-language pattern-matching is especially valuable for a port: it checks
   the target API against both its target-language siblings and the source-language original.

**If FINDINGS from either:** retry from Plan with the findings. Cap at 2 retries — then proceed with
open questions. Independence still holds — the validation/review must come from a different agent
than the implementer.

### Phase 4: Document

Update docstrings and user-facing docs to match target conventions.

**Role prompt:** `skills/port/assets/roles/documenter.md`

### Phase 5: Report

Assemble the review artifacts (see below) into a PR body with collapsed detail blocks.

**Role prompt:** `skills/port/assets/roles/reporter.md`

### Open the PR

1. Create branch (e.g. `port/feature-name-to-target`)
2. Commit translated files
3. Open PR with the report as body
4. Link back to source issue if one exists

### Post-translation: update guidance

If you learned something new — a construct mapping, a gotcha, a non-obvious convention — append it
to `references/guidance.md`. Only concrete, specific lessons.

## Principles

- **Translate, don't improve.** Reproduce behavior as-is. Don't fix unrelated code or redesign.
- **Idiomatic over literal, when confident.** When uncertain, literal is fine — flag it.
- **Translate only completed features.** Source must be merged, tested, documented. If not, stop.
- **Fix the system, not the instance.** Conflicting precedents in the target → flag as system issue.

## Review artifacts

The PR report must include (mechanically derived — evidence the agent can't fake):

- **Behavior traceability matrix** — source test → target test → pass/fail. Missing rows visible.
- **Captured test output** — verbatim runner output.
- **Structural map** — source decomposition → where each piece landed.
- **Decision log** — deviations from source, what was chosen, why, alternatives.
- **New dependencies / capability delta** — with justification.
- **Sensitive-surface diff** — credentials, network, subprocess, deserialization.
- **Lint/format/type-check results** — actual output.
- **Open questions** — anything unverified or uncertain.
