---
name: triage
description: >
  Meta-reasoning go/no-go on whether a task, issue, or PR should be done at all and whether the
  approach is right — BEFORE investing in implementation. TRIGGER on "should we do this", "is this
  our concern", "is this the right approach", "triage this", or a high-level go/no-go. SKIP when
  the decision to proceed is already made. Verdict: accept / defer / redirect / reject / escalate.
allowed-tools: bash file_editor use_github think spawn
---

# Triage (meta-reasoning gate)

Use this to question the premise before committing effort: is the problem real, in scope, the right
layer, not already solved, and is the proposed approach sound?

## How to run it

Spawn a subagent with the triage role prompt for an independent, research-only judgment:

```
spawn(
  prompt="Triage <the issue/task>: <the request, relevant links/paths, any constraints>.",
  system_prompt="skills/triage/assets/roles/triage.md",
)
```

Triage is research-only *by role* — the role prompt constrains it to gather evidence and judge, not
implement (there is no `tools=` argument on `spawn`; scope is enforced by the role prompt). Give it
the request plus where to look so it can reason from evidence, not speculation.

**Model tier:** a genuine go/no-go judgment stays on the default model — don't downgrade the
decision that gates everything downstream. When triage is used as a *cheap router* (e.g. the
code-review pipeline's pass-routing step), `model="fast"` is enough.

## What you get back

A verdict (`accept` | `defer` | `redirect` | `reject` | `escalate`) with reasoning grounded in what
it found, and an alternative when it doesn't accept. Use it as the gate *before* implementing — if
it says defer/redirect/reject, stop and act on that rather than building anyway.
