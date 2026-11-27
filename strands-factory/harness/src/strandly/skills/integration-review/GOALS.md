# Goals: integration-review

- The package was judged, not the YAML: the review names the SDK protocol the user-facing class
  implements with `file:line`, and asks whether a more native primitive applies (redirect names the
  template; never "relabel the YAML").
- Scorecard cells have a source command; spot-check two. Every provenance / data-flow 🔴 was re-run
  by the orchestrator before posting.
- Data flow classified from the artifact with the request-building `file:line`; artifact ≡ source
  checked or recorded UNAVAILABLE. Installability tested in a fresh env against the current SDK.
- Reputation reported as plain values (no adjectives, never a 🔴); tiers and editorial calls posed
  as maintainer questions, nothing granted or refused.
- Verdict is one of the five with the concrete path in; one review, ≤15 visible lines, detail
  collapsed, YAML fixes as `suggestion` threads; posting confirmed first; no re-nag.
- One valid `work-product` block last, `kind: "review"`, integration verdict in `tldr`.
