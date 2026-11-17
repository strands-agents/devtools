---
name: first-response
description: >
  First response to a freshly-filed issue/ticket: reproduce it, find duplicates, decide where it
  belongs, and prepare the ticket + a draft reply. For bug reports it follows a strict verification
  protocol: inspect-then-reproduce (the reporter's repro, else a DERIVED repro before any "cannot
  reproduce"), a verdict taxonomy, and a P0–P3 urgency score for confirmed bugs. TRIGGER when
  handling an incoming bug report or issue ("triage this issue", "is this a dup", "can you repro
  this", "verify this bug", "first response"). SKIP for changes already understood/scoped.
  Disposition: needs-info / confirmed-bug / duplicate / wrong-repo / works-as-intended / escalate.
allowed-tools: bash file_editor use_github think spawn
---

# First Response

Use this to get an incoming issue maintainer-ready: reproduce → find duplicates → assess
disposition → prepare the ticket → draft a reply. More action-oriented than `triage` (which only
judges go/no-go): this one actually reproduces and prepares the ticket.

## How to run it

Spawn a subagent with the first-responder role prompt:

```
spawn(
  prompt="First-response triage for <issue link / the report text>. <repo context, repro steps "
         "given>.",
  system_prompt="skills/first-response/assets/roles/first-responder.md",
)
```

The subagent inherits the harness toolset (there is no `tools=` argument on `spawn`): `bash` to
reproduce in the sandbox, `use_github` to search duplicates/related and read issue context.
Reproduction is mandatory for bug reports — it runs the report, it doesn't speculate. Keep the
default model tier — disposition calls (duplicate vs. new, bug vs. works-as-intended) gate what a
maintainer sees next.

## Bug verification — the rigor layer

For **bug reports**, the role follows the protocol in
`skills/first-response/references/bug-verification.md` (read it yourself if you're verifying
without a spawn). Its contract, in brief:

- **Inspect before running:** locate the implicated code, record a likelihood call
  (likely-bug / likely-not-bug / uncertain) and a version-skew check.
- **Path A → Path B:** run the reporter's repro first; if it's missing, fails, or doesn't match
  their Expected/Actual, you must attempt to **derive your own repro** before concluding "cannot
  reproduce". A derived repro that works is maintainer gold — it goes in the draft reply.
- **One verdict:** Reproduced (via reporter's / via derived repro) / Partially reproduced /
  Not reproduced / Insufficient information — each with captured output as evidence.
- **Urgency P0–P3 for confirmed bugs only** (impact × reach × workaround × regression); never a
  priority on an unconfirmed report. Labels are *recommended* in the ticket, not applied.
- **Live-AWS repros** ride the `e2e-test` skill's enforced credential boundary
  (`ManagedBy=strandly` tags, `strandly-managed-*` buckets, cleanup) when the sandbox carries the
  CI Bedrock role — see that skill before creating anything.

## What you get back

A prepared ticket (summary, likelihood call, repro verdict with output, suspected area, priority
recommendation for confirmed bugs, duplicates/related links, recommended labels), a recommended
**disposition**, and a draft first-response comment ready for a maintainer to post (it drafts; it
does not post unless you tell it to).
