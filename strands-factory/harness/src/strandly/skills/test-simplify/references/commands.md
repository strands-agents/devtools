# Commands (from each package's TESTING.md / AGENTS.md / pyproject, read 2026-11-02)

```bash
# --- existing work ---
gh pr list --repo $R --author @me --state all -L 300 --json number,title,state,headRefName \
  --jq '.[] | select(.headRefName | startswith("test-simplify/") or startswith("test-gap/"))'

# --- churn ---
git log --since=90.days --oneline -- $TEST_FILE | wc -l
```

## Scoped coverage per package

Run the same command on `main` and on the branch; compare the module's covered lines and branches.

| Package | Repo | Scoped coverage |
|---|---|---|
| `strands-py/` | `strands-agents/harness-sdk` | `hatch test -c $TEST_PATH` (pytest-cov; XML/HTML under `build/coverage/`) |
| `strands-ts/` | `strands-agents/harness-sdk` | `npx vitest run --coverage --project unit-node $TEST_PATH` |
| `harness-py/` | `strands-agents/harness-sdk` | `pip install pytest-cov && pytest --cov=strands_harness --cov-branch --cov-report=term-missing $TEST_PATH` |
| `harness-ts/` | `strands-agents/harness-sdk` | `npx vitest run --coverage $TEST_PATH` (`npm run check` before the PR) |
| evals | `strands-agents/evals` | `hatch test -c $TEST_PATH`; lint with `hatch fmt --linter` |

Sibling maps worth reading together: `strands-py/tests/strands/models/` against
`strands-ts/src/models/__tests__/`, and each provider's file against the other providers' in the
same directory.
