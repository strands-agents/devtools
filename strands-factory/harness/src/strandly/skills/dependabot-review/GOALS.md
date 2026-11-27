# Goals: dependabot-review

- Every version in the bump range was read (release notes or package diff), not only the PR body;
  sources are linked in the comment.
- The repo's call sites of the bumped package were found and each breaking change checked against
  them with `file:line`; "None found" means the grep ran, not that it was skipped.
- Tests were run in the sandbox with the exact command and result in the comment; anything not
  run (integ, platform-specific) is listed as skipped with the reason.
- Verdict is one of safe / needs-check / breaking / hold and follows from the evidence; a
  breaking verdict names the fix and whether to add an ignore rule.
- One comment review with the marker line for the head SHA; no approve, merge, auto-merge, or
  push; no second review on a PR you already reviewed unless its target version changed (a rebase
  alone is not a reason).
- One valid `work-product` block last, `kind: "review"`, Dependabot verdict in `tldr`.
