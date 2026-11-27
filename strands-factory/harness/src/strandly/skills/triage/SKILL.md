---
name: triage
description: >
  Meta-reasoning go/no-go on whether a task, issue, or PR should be done at all and whether the
  approach is right — BEFORE investing in implementation. TRIGGER on "should we do this", "is this
  our concern", "is this the right approach", "triage this", or a high-level go/no-go. SKIP when
  the decision to proceed is already made. Verdict: accept / defer / redirect / reject / escalate.
allowed-tools: shell read write edit use_github subagent
---

# Triage

Question the premise before committing effort. Run the rubric in
`skills/triage/assets/roles/triage.md` yourself; run it as a `subagent` (`agent_type="triage/triage"`) only when your context is already
invested in the answer (you've started implementing, or you wrote the proposal). It is a judgment,
not a fan-out — one pass, default model.

The verdict gates what follows: `defer` / `redirect` / `reject` means stop and say so, not build
anyway. Always give the alternative when the verdict isn't `accept`.

End with one `work-product` block (`skills/_shared/work-product-emit.md`, `kind: "triage"`):

```work-product
{"kind": "triage", "verdict": "redirect", "title": "Feature request: retry backoff knob",
 "tldr": "Real need, wrong layer — belongs in the model provider adapter.",
 "target_url": "https://github.com/o/r/issues/77", "repo": "o/r", "number": 77}
```
