# Goals: test-simplify

- Before any PR, your existing `test-simplify/` and `test-gap/` PRs were listed; open ones were
  counted against the budget and a target with a declined PR was skipped.
- Each candidate came from reading the feature's source, its tests, and its siblings (other
  providers, the other language, shared fixtures), not from one test file in isolation.
- Candidates were ranked by lines removed, helpers collapsed, and churn; PRs were opened from the
  top of the list and stopped at the budget.
- Each PR changes exactly one test or test case and no source code.
- Each PR shows baseline and after coverage for the module under test from the same command, the
  covered set did not shrink, and every removed test ID maps to a test that still covers it.
- The break-the-behavior check ran and the new test failed on the broken source.
- Every dropped case cites the unreachable branch or the duplicate test that justifies it.
- A gap PR mirrors a named sibling test and covers named source lines; a gap test that failed was
  filed as a bug, not opened as a PR; a gap in the source itself was left to the port flow.
- An independent reviewer subagent ran and its real findings fixed before the PR.
- Branches were pushed to the fork only; nothing approved or merged.
- The report lists every acted-on and backlogged candidate; a quiet survey is one line.
