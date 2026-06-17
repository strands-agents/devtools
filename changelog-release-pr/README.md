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

## Authoring curated (narrative) release notes

The generated file gives every release a structured summary (entries grouped
into Features / Fixes / Other, area tags, first-time-contributor chips). For
high-visibility releases you can add a hand-written narrative on top — prose,
code samples, migration notes — that renders on the release's detail page and,
namespaced, on the combined changelog. These are the best customer-facing
changelogs; write them when there's manpower to.

Two curated fields, both survive re-syncs (the parser never overwrites them):

- **`highlights:`** — a short YAML string (1–3 sentences, inline markdown OK)
  shown in an accent callout at the top of the release. Use for a quick "why
  this release matters."
- **markdown body** — everything after the closing `---`. Long-form narrative.

### Conventions (so curated notes stay consistent and valid)

1. **Don't restate the structured data.** The frontmatter already renders the
   per-PR entry list and the first-time-contributor chips. A curated body must
   NOT include GitHub's auto-generated `## What's Changed` /
   `## New Contributors` / `**Full Changelog**` scaffolding — that duplicates
   the chips/entries and (because every release reuses those headings) produced
   colliding heading ids on the combined page. Keep only the human narrative.
2. **Start body headings at `###`.** The version number is the page heading
   (`h1` on the detail page); section headings inside the body should be `###`
   or deeper so the outline stays well-formed.
3. **Lead with the "why."** Describe what changed and why a user cares, then
   show a minimal code sample. Link PRs inline as `[PR#1234](url)`.
4. **Set `areas`/types via PR labels**, not prose — the structured summary is
   generated from `area-*` labels and conventional-commit titles.

### Template

Copy this skeleton into a release file's body (after the frontmatter). The
frontmatter itself is generated; you add `highlights:` and the body.

```markdown
---
# ...generated frontmatter (sdk, language, version, tag, date, urls, entries,
# newContributors)...
highlights: Adds X and Y; migrates Z. One or two sentences on why it matters.
---

### Headline feature — [PR#1234](https://github.com/strands-agents/harness-sdk/pull/1234)

One or two paragraphs on what it does and why, in plain language.

```python
# a minimal, runnable example of the new capability
```

### Another notable change — [PR#1240](https://github.com/strands-agents/harness-sdk/pull/1240)

Short narrative. Note any migration steps or breaking behavior here.

### Notes

- Smaller call-outs, deprecations, or upgrade guidance as a short list.
```

The six files under `site/src/content/changelog/` that carry hand-written
bodies today (`harness/python-v1.25.0`, `harness/python-v1.35.0`,
`harness/typescript-v0.2.1`, `harness/typescript-v1.0.0-rc.3`, `evals/v0.1.5`,
`evals/v0.1.14`) follow this convention and serve as worked examples.
