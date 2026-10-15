# Goals: first-response

Critic acceptance criteria when the `first-response` skill is active — getting an incoming issue
maintainer-ready.

- **A disposition is present:** one of `needs-info` / `confirmed-bug` / `duplicate` / `wrong-repo` /
  `works-as-intended` / `escalate`.
- **Reproduction was attempted for any bug report**, in the sandbox, with the actual output shown —
  not speculated. For a `confirmed-bug` disposition, the repro evidence must exist; re-run or inspect
  it. (`needs-info` is acceptable when the report genuinely lacks repro steps.)
- **A "cannot reproduce" conclusion required BOTH paths:** if the reporter's own steps were missing,
  failed, or didn't match their Expected/Actual, a *derived* reproduction attempt (Path B of
  `references/bug-verification.md`) must be visible in the work before any not-reproduced /
  cannot-reproduce verdict. Path A alone is not enough.
- **The repro verdict uses the taxonomy:** Reproduced (via reporter's repro / via derived repro) /
  Partially reproduced / Not reproduced / Insufficient information — with captured output.
- **Priority discipline:** a P0–P3 urgency score (with impact/reach/workaround/regression one-liners)
  is present for a confirmed bug, and **no priority** is assigned for any unconfirmed verdict
  (partially / not reproduced / insufficient information → `N/A`).
- **A duplicate / related search was done** (e.g. via `use_github`) and links are included, OR the
  absence of duplicates is stated.
- **A prepared ticket is present**: summary, repro status with output, suspected area,
  duplicate/related links, and recommended labels.
- **A draft first-response comment was produced**, ready for a maintainer, respecting the
  one-comment discipline (share a derived repro, or request the exact missing info).
- **If live AWS resources were created for the repro**, they follow the e2e-test boundary
  (`ManagedBy=strandly` tag, `strandly-managed-*` names) and were cleaned up — or the leftover ids
  are called out explicitly.
- **Nothing was posted to GitHub unless the user explicitly asked.** The skill drafts; it does not
  post on its own. A self-initiated public comment is a gap.
