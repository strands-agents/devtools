---
name: code-review
description: >
  Independent review of a code change / PR: route → parallel specialist passes → dedupe → post.
  Self-scaling from a one-line diff to a large PR. TRIGGER on "review" / "check" / "look over" a
  change, PR, or diff, on a narrow ask like "just adversarially test this", and after you make a
  non-trivial change and want a second pass. SKIP for pure questions and trivial edits. Produces a
  POSTED GitHub review: TL;DR verdict + severity-ranked inline findings.
allowed-tools: shell read write edit use_github subagent
---

# Code Review

You orchestrate; findings come from fresh-context specialists you run as `subagent`s. Your own context is
biased once you've read the diff, so a pass run warm is worth less than a fresh one.

**The rule that matters most: false positives kill the reviewer.** Every posted comment must point
at a concrete, evidence-grounded problem the author doesn't already know. Precision over recall.

## Pipeline

1. **Route (you, inline).** `git fetch origin pull/<N>/head:pr-<N>`, read the diff and the PR/issue
   text. Decide go/no-go and which passes apply:

   | Pass | Role (`agent_type`) | Model | Run when |
   |---|---|---|---|
   | Correctness / scope / tests / docs | `code-review/reviewer` | default | any logic change |
   | API / DevX / model-facing surface | `code-review/api-bar-raiser` | `advanced` | public class/fn/param/export/type/error changed, or tool descriptions / prompts / tool results changed |
   | Adversarial / repro | `code-review/adversarial-tester` | `advanced` | risky logic: parsing, concurrency, error paths, security |

   Docs-only or trivial PR → reviewer alone (or none — say why). Narrow ask ("just adversarially
   test") → that one pass. Don't run a pass the change doesn't warrant.

2. **Run the routed passes as parallel `subagent`s.** Resolve and record the exact head and comparison
   base commits. Each task gets these commits, PR URL, source paths, what the change claims (issue
   link + acceptance criteria), and the changed-file list. Paths, not contents: they read the
   sandbox. Use the same pinned revisions throughout; if the head changes, revalidate the affected
   evidence. Pass the role as `agent_type`; it sets the tool scope, so don't pass `tools`.
   If `subagent` is broken, run the pass yourself after reading its role file
   (`skills/code-review/assets/roles/<role>.md`) and say so.

3. **Dedupe and tier (you, inline).** Merge findings that share a root cause. Drop anything without
   `file:line` + quoted evidence, anything linter-owned, restatements, and speculative hardening.
   Re-check the line numbers and spot-check the riskiest claims against the code — a finding that
   doesn't hold is dropped, not softened. Then, per finding, ask **would a user actually hit
   this?** Trace it from the public surface (`Agent`, documented config, an exported API) to the
   failing line with input a real caller would produce. If the only route is a direct call into an
   internal layer, input the public surface can't produce, or a request the upstream API rejects on
   its own, it is unreachable: ⚪ at most, usually just dropped. Unreachable does not demote a 🔴
   to a 🟡 — it takes the finding out of the review. A write-up that has to say *unreachable*,
   *pre-existing*, *for the record*, or *nothing to do* is telling you it isn't a finding. Then
   tier on **reachability**:
   - 🔴 block — reachable via a public/documented path with plausible input; consequence beats a
     clear error (data loss, silent wrong answer, security, broken contract); and the failure is
     shown (repro ran, or deterministic from the code). Can't name the caller and input, or can't
     show it → not a 🔴.
   - 🟡 should-fix — reachable by the same test, bounded consequence.
   - ⚪ nit — contrived, hypothetical, style. Appendix only.
   Pre-existing bugs (true with the PR unmerged) are never inline or blocking. Before filing an issue,
   gate inline (no subagent): (1) would a maintainer act on it now — not a ⚪/defensive/hypothetical nit;
   (2) it belongs to *this* repo, not a dependency or the caller; (3) no open or recently-closed
   duplicate; (4) proportionate scope, not a rewrite. All four clear → file (`file:line`, repro, why)
   + one appendix line *"pre-existing, filed #N"*. Belongs elsewhere → name the repo in the appendix,
   don't auto-file cross-repo. Otherwise drop it — at most one appendix line saying why not filed.
   Design/scope/API questions with no `file:line` defect still ship — as **Questions**.

4. **Post one review** via `use_github` (confirm first on a repo you don't own):
   - TL;DR comment: verdict (approve / changes-requested), one headline, ≤15 visible lines. Below
     the fold in `<details>`: ✅ what you verified (SHA, test cmd + count, repros), Questions
     (blocking / non-blocking), Reading order for human reviewers, Appendix — non-blocking (N).
   - Inline comments for 🔴 and the top few 🟡 only (a 30-comment review is an ignored review),
     each written per **Write an inline finding** below.
   - Design points as questions ("would it be simpler to…?"); directive only for grounded 🔴.
   - On a PR update, review only the new diff; never re-nag resolved threads — but a thread the
     author resolved themselves isn't decided; re-check it silently.
   - Public API changed and no `needs-api-review` / `completed-api-review` label → say so in the
     TL;DR; a new primitive/abstraction → "needs API meeting", don't approve solo.

5. **Emit the `work-product` block last** — `skills/_shared/work-product-emit.md`, `kind: "review"`,
   every finding including suppressed ones.

## Complete the testing campaign

For a behavioral review, including test-only changes that protect runtime behavior, plan the
relevant inputs, boundaries, failure paths, operation sequences, and supported configurations.
Assign each suite or experiment to one owner; use isolated scratch outputs for parallel work.
A narrow user request limits this campaign. Keep prose-only and cosmetic reviews proportional.

Run the affected-package suites required by the repository and the relevant behavior cases before
concluding. Initial focused probes do not replace that coverage. Add bounded repetitions or
concurrency checks when ordering or stability matters. Preserve the first failure and its inputs.
Verify collected and executed test counts, skips, exit codes, and that the intended operation ran.
Recheck legitimate behavior after a fix and rerun checks whose evidence the fix invalidates.

Keep regression evidence in the existing review details: both commits, commands, inputs, each
result, and limits. Distinguish a demonstrated defect, a test-coverage gap, and an unverified
hypothesis. Identify blocked suites and untested integrations or platforms; passing checks do not
establish behavior outside their tested conditions.

## Follow-up reviews

If the delta since your last review only fixes findings you filed, review it yourself — no fan-out.
Verify each fix against the diff, re-run any repro, and attack the fix itself (a fix that trades one
bug for another is the common failure). Lead the post with the status of previous findings (fixed /
fixed-but-introduced-X / still open / withdrawn). For each thread *you* opened whose fix you
verified, reply one line and `resolveReviewThread` it — leave still-open, withdrawn, and
human-opened threads alone. New behaviour, a touched public surface, or ~300+ lines → full pipeline.

## What we care about

We are design-led: scope, API shape, "do we even need this", and whether the *model* gets enough
context (tool descriptions, tool results, errors surfaced to the model) outrank correctness nits,
which outrank style (linter's job). Stances: new tools → community package, not core; minimise
public surface, keyword-only args; no breaking changes without justification; opt-in over forced
defaults; tests prove behaviour, not echo mocks; docs example-first. Block rarely and deliberately.

## Write an inline finding

Each comment stands alone: a developer who knows the language but hasn't traced this code follows it
without the TL;DR, another comment, or your scratch files.

- **Bold one-line title** naming what breaks. Then one short paragraph: caller and triggering input,
  expected vs observed behavior, consequence. Quote the smallest useful code or the violated
  contract; explain unfamiliar components.
- `Suggestion:` fixes the cause, in a `suggestion` block when the fix is concrete. State confirmed
  defects directly and respectfully; phrase optional improvements as suggestions. Tone never
  changes the tier.
- Evidence goes in a collapsed block in the same comment (blank lines around Markdown inside it):
  - Executed → `<details><summary>Reproduction details</summary>`: reviewed and tested revisions,
    relevant platform/config, setup, exact commands or inputs, expected and observed results,
    controls or base comparison when available, enough fixture or temporary-test code to repeat
    it, exit codes and side effects reported separately.
  - Source-only → `<details><summary>Evidence details</summary>`: revision, path, code or contract,
    the failing case, and that you did not run it. Label proposed checks unexecuted; name missing
    controls and evidence limits.
- Never invent a run, result, control, or measurement.
- Title, explanation, correction and summary fit 15 visible lines. A limit that changes impact or
  confidence stays visible. No line-by-line narration; keep the causal steps.
