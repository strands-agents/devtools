// github-script entry: assumes `github` (Octokit), `context`, and `core` are
// provided by actions/github-script (same convention as process-input.cjs).
// Builds a real client + fs ops and delegates to run.cjs. Inputs come from env
// (set by the composite action): SOURCE_REPO, MODE, TAG, TARGET_DIR.

const fs = require('fs')
const path = require('path')
const { run } = require('./run.cjs')

function splitRepo(full) {
  const [owner, repo] = full.split('/')
  return { owner, repo }
}

async function runAction(github, context, core) {
  const sourceRepo = process.env.SOURCE_REPO
  const mode = process.env.MODE === 'backfill' ? 'backfill' : 'single'
  const tag = process.env.TAG || undefined
  const targetDir = process.env.TARGET_DIR
  const skipExisting = process.env.SKIP_EXISTING === 'true'

  if (mode === 'single' && !tag) {
    core.setFailed('changelog: single mode requires a tag (got none). Pass the release tag or use mode: backfill.')
    return { written: [], warnings: [] }
  }

  const client = {
    listReleases: async (repoFull) => {
      const { owner, repo } = splitRepo(repoFull)
      return github.paginate(github.rest.repos.listReleases, { owner, repo, per_page: 100 })
    },
    getRelease: async (repoFull, t) => {
      const { owner, repo } = splitRepo(repoFull)
      try {
        const res = await github.rest.repos.getReleaseByTag({ owner, repo, tag: t })
        return res.data
      } catch (e) {
        if (e.status === 404) return null
        throw e
      }
    },
    getPr: async (repoFull, num) => {
      const { owner, repo } = splitRepo(repoFull)
      try {
        const res = await github.rest.pulls.get({ owner, repo, pull_number: num })
        const pr = res.data
        const out = {
          labels: (pr.labels || []).map((l) => l.name),
          merge_commit_sha: pr.merge_commit_sha,
          user: pr.user ? pr.user.login : null,
        }
        // Changed files drive two gates: language gating (monorepo) and the
        // docs-only drop (all streams, incl. pre-monorepo/evals). So fetch
        // files for every repo now — a docs/blog-only PR is filtered out
        // everywhere, which needs file info on single-language repos too.
        try {
          const files = await github.paginate(github.rest.pulls.listFiles, { owner, repo, pull_number: num, per_page: 100 })
          out.files = files.map((f) => f.filename)
        } catch (e) {
          // Leave files undefined → languages null, docsOnly false → entry kept.
          core.warning(`PR ${repoFull}#${num} files: ${e.status || e.message} — gating skipped`)
        }
        return out
      } catch (e) {
        // 404 (deleted PR) and transient errors (403 rate-limit, 5xx) should
        // degrade this one entry, not abort a whole backfill. Enrichment is
        // best-effort; the next sync re-enriches.
        if (e.status !== 404) core.warning(`PR ${repoFull}#${num}: ${e.status || e.message} — skipping enrichment`)
        return null
      }
    },
    // Tags for prior-tag-in-stream detection.
    listTags: async (repoFull) => {
      const { owner, repo } = splitRepo(repoFull)
      const tags = await github.paginate(github.rest.repos.listTags, { owner, repo, per_page: 100 })
      return tags.map((t) => ({ name: t.name, commitSha: t.commit && t.commit.sha }))
    },
    // Merged commits between two tags. GitHub caps a compare at 250 commits;
    // total_commits tells us when the list is truncated so callers can warn.
    compareCommits: async (repoFull, base, head) => {
      const { owner, repo } = splitRepo(repoFull)
      try {
        const res = await github.rest.repos.compareCommitsWithBasehead({ owner, repo, basehead: `${base}...${head}`, per_page: 100 })
        const data = res.data
        const commits = (data.commits || []).map((c) => ({ sha: c.sha, message: c.commit && c.commit.message }))
        return { commits, truncated: typeof data.total_commits === 'number' && data.total_commits > commits.length }
      } catch (e) {
        core.warning(`compare ${repoFull} ${base}...${head}: ${e.status || e.message} — no entries derived`)
        return { commits: [], truncated: false }
      }
    },
    // PRs associated with a commit (authoritative; handles squash + merge).
    commitPulls: async (repoFull, sha) => {
      const { owner, repo } = splitRepo(repoFull)
      try {
        const res = await github.rest.repos.listPullRequestsAssociatedWithCommit({ owner, repo, commit_sha: sha, per_page: 100 })
        return (res.data || [])
          .filter((pr) => pr.merged_at) // only merged PRs
          .map((pr) => ({ number: pr.number, title: pr.title, user: pr.user ? pr.user.login : null }))
      } catch (e) {
        if (e.status !== 404) core.warning(`commit ${repoFull}@${sha} pulls: ${e.status || e.message} — skipped`)
        return []
      }
    },
  }

  const result = await run({
    repo: sourceRepo,
    mode,
    tag,
    skipExisting,
    client,
    readExisting: async (p) => {
      try {
        return fs.readFileSync(path.join(targetDir, p), 'utf8')
      } catch {
        return null
      }
    },
    writeFile: async (p, contents) => {
      const full = path.join(targetDir, p)
      fs.mkdirSync(path.dirname(full), { recursive: true })
      fs.writeFileSync(full, contents)
    },
  })

  core.info(`changelog: wrote ${result.written.length} file(s)`)
  for (const w of result.warnings) core.warning(w)
  // Expose for the PR body + a git-safe branch name (tags contain '/' and '.').
  // Slug from the repo NAME so different source repos (harness-sdk vs the
  // archived sdk-typescript) can never collide on the same backfill branch.
  const repoSlug = sourceRepo.split('/')[1] || sourceRepo
  const tagSlug = (tag || 'backfill').replace(/[^A-Za-z0-9._-]+/g, '-')
  // Only `branch` and `warnings` are consumed by the composite action (PR
  // branch + body); the written count is already logged via core.info above.
  core.setOutput('warnings', result.warnings.join('\n'))
  core.setOutput('branch', `changelog/sync-${repoSlug}-${tagSlug}`)
  return result
}

module.exports = runAction
