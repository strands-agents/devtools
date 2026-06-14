# Changelog Release PR

Composite action that turns a GitHub Release into a structured changelog entry
for the docs site in `strands-agents/harness-sdk` and opens a PR there.

Pipeline (deterministic — no LLM):

1. Fetch the release (single tag) or all releases (backfill) from `source-repo`.
2. Parse the auto-generated "What's Changed" body into structured entries
   (conventional-commit type/scope, title, PR, author). "New Contributors"
   lines are extracted separately and never become entries.
3. Enrich each entry from its PR: `area-*` labels → areas, `breaking change`
   label, merge-commit SHA, author. For monorepo releases the PR's changed
   files gate entries by language (`strands-py` → python stream, `strands-ts` →
   typescript, both → both, neither → omitted; new contributors with neither
   are kept in both — people aren't noise). Enrichment degrades gracefully when
   a PR can't be fetched.
4. Render `site/src/content/changelog/<sdk>/<file>.md` matching the harness-sdk
   content-collection schema. Human-written `highlights:` blocks and markdown
   bodies survive re-syncs.
5. Open a PR against `target-repo` via peter-evans/create-pull-request.

## Inputs

| Input | Required | Default | Notes |
|---|---|---|---|
| `source-repo` | yes | — | owner/repo the release belongs to |
| `tag` | single mode | `''` | release tag to sync |
| `mode` | no | `single` | `single` \| `backfill` |
| `skip-existing` | no | `false` | backfill only: generate just the missing files (zero PR-API cost for existing ones, never regresses enrichment). Used by the daily cron backstop. |
| `github-token` | yes | — | reads releases/PRs and opens the PR. Needs `contents:write` + `pull-requests:write` on `target-repo`. NOTE: PRs created with the default `GITHUB_TOKEN` don't trigger `pull_request` workflows (required checks won't run) — use an App/PAT token where that matters. |
| `target-repo` | no | `strands-agents/harness-sdk` | repo that hosts the changelog |

## Consumers

- `strands-agents/harness-sdk` `.github/workflows/changelog-sync.yml` — on
  release + daily cron backstop (the cron also backstops evals).
- `strands-agents/evals` `.github/workflows/changelog-sync.yml` — cross-repo
  PR into harness-sdk on each evals release.

## Tests

```bash
cd changelog-release-pr/scripts && node --test
```

Dependency-free `.cjs` modules run via `actions/github-script`; logic modules
are pure with injected fetchers/fs, so the suite runs without network.
