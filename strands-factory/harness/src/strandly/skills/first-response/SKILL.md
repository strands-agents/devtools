---
name: first-response
description: >
  First response to a freshly-filed issue: reproduce it, find duplicates, decide where it belongs,
  and prepare the ticket + a draft reply (draft-only unless told otherwise). Bug reports follow the
  inspect-then-reproduce protocol with P0–P3 urgency for confirmed bugs. TRIGGER when handling an
  incoming bug report or issue ("triage this issue", "is this a dup", "first response"). SKIP for
  changes already scoped, and for standalone verification that posts a comment (→ bug-verify).
allowed-tools: shell read write edit use_github subagent
---

# First Response

Get an incoming issue maintainer-ready: reproduce → duplicates → disposition → ticket → draft reply.
One pass: run the `first-response/first-responder` role (`agent_type`) as a `subagent` (default model, full
toolset — it reproduces in the sandbox and searches GitHub) with the issue link and repo context,
or run the role (`skills/first-response/assets/roles/first-responder.md`) yourself for a small issue. Bug reports follow
`skills/bug-verify/references/protocol.md`; when the ask is to *post* a verification comment,
use `bug-verify` instead — this skill drafts and doesn't post.

You get back: a ticket (summary, likelihood, repro verdict + output, suspected area, priority for
confirmed bugs, duplicates, recommended labels), a disposition, and a draft comment.

End with one `work-product` block (`skills/_shared/work-product-emit.md`, `kind: "first-response"`)
mapped from the role's output; omit `priority` for any unconfirmed verdict:

```work-product
{"kind": "first-response", "verdict": "confirmed-bug", "title": "Streaming drops the final tool-use chunk",
 "tldr": "Last chunk dropped when stop_reason arrives mid-block.", "target_url": "https://github.com/o/r/issues/91",
 "repo": "o/r", "number": 91, "priority": "P1", "repro_status": "reproduced",
 "suspected_area": "src/strands/event_loop/streaming.py:212", "recommended_labels": ["bug-validated", "P1"],
 "duplicates": [], "draft_comment": "Thanks — reproduced on main with your snippet; tracking as P1."}
```
