---
name: bug-verify
description: >
  Verify a reported bug with evidence — snapshot the issue, inspect the code, reproduce it (the
  reporter's steps, then a DERIVED repro before any "cannot reproduce"), then post at most ONE
  comment. TRIGGER on "verify this bug", "can you repro this", "bug-verify", or when a bug report
  needs validating before anyone fixes it. SKIP for feature requests and questions (first-response /
  triage), for fixing the bug (implement), and for reviewing a code change (code-review).
allowed-tools: shell read write edit use_github subagent
---

# Bug Verify

Protocol (oracle, Path A/B, verdicts, urgency, comment templates): `skills/bug-verify/references/protocol.md`.

1. **Snapshot & route.** Fetch the issue once via `use_github`. That text is the snapshot: wrap it in
   `--- ISSUE SNAPSHOT --- … --- END ISSUE SNAPSHOT ---` and pass it verbatim to the reproducer;
   nobody re-fetches the body (it can be edited mid-run; metadata reads are fine). Not a bug report
   (feature request, question) → say so and stop. No behavioural content at all (no steps, error,
   or expected/actual) → report **Insufficient information** with the exact missing items; skip
   the subagent.
2. **Reproduce.** Run the `bug-verify/reproducer` role (`agent_type`) as a `subagent` (`model="advanced"`) with
   the snapshot, repo + SDK language, and build hints. It inspects first (suspected area,
   likelihood, version skew), then runs Path A and Path B, and returns the deciding script and
   output verbatim. One subagent; no parallel inspector, no skeptic.
3. **Verdict & urgency (you).** Take the reproducer's verdict; where inspection and execution
   disagree, execution wins the verdict and the tension goes in the report. Score P0–P3 (protocol
   rubric) for Reproduced verdicts only, `N/A` otherwise.
4. **Report, then at most one comment.** The report (always): verdict, suspected area + mechanism,
   repro script + output in `<details>`, urgency lines, version-skew note, label recommendations,
   missing-info list, next steps. Before posting either protocol comment, **re-run the deciding
   script yourself** in a clean shell and check it against the reporter's oracle *and* the suspected
   area — a clean run of the wrong code path proves nothing; never patch the script to make the
   claim come true. Output differs → downgrade, don't post. Before a public *Insufficient
   information*, confirm each missing item is really absent and no runnable path was skipped. Post
   via `use_github` only in the protocol's two cases; labels recommended, never applied.
   Lost reproducer → inspection-only report, no comment.
