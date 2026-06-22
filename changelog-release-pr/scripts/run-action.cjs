// github-script entry: assumes `github` (Octokit), `context`, and `core` are
// provided by actions/github-script (same convention as process-input.cjs).
// Builds a real client + fs ops and delegates to run.cjs. Inputs come from env
// (set by the composite action): SOURCE_REPO, MODE, TAG, TARGET_DIR.

const fs = require('fs')
const path = require('path')
const crypto = require('crypto')
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
    // Merged commits between two tags. The compare endpoint returns its
    // `commits` array one 100-item page at a time, so we MUST paginate — a
    // single call would silently drop every commit past the first 100 and
    // produce an incomplete changelog. We walk all pages via the iterator
    // (accumulating commits) and read `total_commits` from the first page to
    // detect GitHub's hard 250-commit cap, beyond which the range genuinely
    // can't be fully expanded and callers should warn.
    compareCommits: async (repoFull, base, head) => {
      const { owner, repo } = splitRepo(repoFull)
      try {
        const commits = []
        let total = null
        for await (const page of github.paginate.iterator(github.rest.repos.compareCommitsWithBasehead, {
          owner,
          repo,
          basehead: `${base}...${head}`,
          per_page: 100,
        })) {
          if (total === null && typeof page.data.total_commits === 'number') total = page.data.total_commits
          for (const c of page.data.commits || []) commits.push({ sha: c.sha, message: c.commit && c.commit.message })
        }
        // total_commits is capped at 250 by GitHub; if it reports more than we
        // could collect, the tail is genuinely unavailable from this endpoint.
        return { commits, truncated: typeof total === 'number' && total > commits.length }
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
  // Only `branch` and `warnings` are consumed by the composite action (PR
  // branch + body); the written count is already logged via core.info above.
  core.setOutput('warnings', result.warnings.join('\n'))
  core.setOutput('branch', branchName(sourceRepo, tag, result.written))
  return result
}

/**
 * Git-safe PR branch name for a sync run. Slug from the repo NAME so different
 * source repos (harness-sdk vs the archived sdk-typescript) can never collide.
 *
 * In single mode the tag is unique, so it identifies the branch. In backfill
 * mode there's no tag — a constant ('backfill') would make every run reuse one
 * branch, so an open-but-unmerged PR from one run would be clobbered by the next
 * (especially via push-to-fork, where the branch lives on the fork). Hash the
 * SET of written files instead. `written` is the set of releases NOT yet on the
 * checked-out base (the PR target), so it equals the PR's content: a re-run that
 * regenerates the same pending release(s) hashes identically and UPDATES the
 * open PR; a run whose pending set differs gets a distinct branch (a separate,
 * independent PR). Caveat: if more releases become pending while an earlier PR
 * is still open, the grown set hashes differently and opens a second PR — which
 * is correct for backfill-opens-one-PR semantics and self-heals once merged.
 * Empty `written` (nothing new) hashes to a constant, but with no file changes
 * create-pull-request no-ops, so that branch never materializes.
 *
 * @param {string} sourceRepo owner/repo
 * @param {string} tag release tag, or '' / undefined for backfill
 * @param {string[]} written paths written this run (order-independent)
 */
function branchName(sourceRepo, tag, written) {
  const repoSlug = sourceRepo.split('/')[1] || sourceRepo
  let suffix
  if (tag) {
    suffix = tag.replace(/[^A-Za-z0-9._-]+/g, '-')
  } else {
    const fingerprint = [...written].sort().join('\n')
    suffix = `backfill-${crypto.createHash('sha256').update(fingerprint).digest('hex').slice(0, 12)}`
  }
  return `changelog/sync-${repoSlug}-${suffix}`
}

module.exports = runAction
module.exports.branchName = branchName
