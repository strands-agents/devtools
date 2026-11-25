# Goals: port

- Only a merged, tested, documented source feature was ported; behaviour preserved, not improved.
- Validation came from a different agent than the implementer (validator subagent); api-bar-raiser ran
  when the port introduced/reshaped a public API.
- The traceability matrix covers every source test (or shows a `missing-behavior` row) with real
  `file:line`s — spot-check one; runner/lint output in the PR is verbatim.
- PR carries matrix, output, decision log, dependency/sensitive-surface delta, open questions.
- Pushing to a repo the agent doesn't own was confirmed first.
