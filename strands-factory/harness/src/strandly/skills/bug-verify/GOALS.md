# Goals: bug-verify

- The issue was snapshotted once and passed verbatim; no re-fetch of the live body.
- The reproducer inspected first (SUSPECTED AREA `file:line`, MECHANISM, LIKELIHOOD, VERSION-SKEW)
  and any "Not reproduced" shows both Path A and a concrete Path B script.
- Verdict uses the taxonomy; deciding script + output are in the report; P0–P3 only for Reproduced.
- Before any comment posted, the orchestrator re-ran the deciding script itself, it matched the
  oracle, and it exercises the suspected area; an *Insufficient information* comment asks only for
  things genuinely absent.
- At most one comment, only in the protocol's two cases, fence language matching the SDK. Labels
  recommended, never applied; no source changes, branch, PR, closure, or assignment.
- AWS resources, if any, followed the e2e-test boundary and were cleaned up (or ids listed).
