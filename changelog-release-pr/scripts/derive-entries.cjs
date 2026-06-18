// Derive a release's changelog entries deterministically from the GitHub
// compare API instead of parsing the release-notes body. The compare endpoint
// lists every merged commit between two tags regardless of how the notes are
// written, so the breakdown is immune to release-note format drift. Each
// commit is resolved to its PR via the commit->PR API for authoritative
// association (handles squash and merge-commit repos), then classified with the
// shared conventional-commit logic. Pure given an injected client.

const { classifyTitle } = require('./parse-release-body.cjs')
const { tagToMeta } = require('./tag-meta.cjs')
const { compareVersionDesc } = require('./semver-compare.cjs')

/**
 * The previous tag in the same stream (same sdk + language) as `tag`, or null
 * if `tag` is the first release in its stream. Streams: python/v*, typescript/v*
 * (harness), and bare v* (evals / pre-monorepo). Uses tagToMeta to classify and
 * compareVersionDesc to order.
 * @returns {Promise<string|null>}
 */
async function previousTagInStream(repo, tag, client) {
  const meta = tagToMeta(repo, tag)
  if (!meta) return null
  const tags = (await client.listTags(repo)) || []
  // Same-stream tags, excluding `tag` itself, sorted newest-first.
  const stream = tags
    .map((t) => t.name)
    .filter((name) => {
      if (name === tag) return false
      const m = tagToMeta(repo, name)
      return m && m.sdk === meta.sdk && m.language === meta.language
    })
    .sort((a, b) => compareVersionDesc(tagToMeta(repo, a).version, tagToMeta(repo, b).version))
  // The immediate predecessor is the newest tag older than `tag`.
  for (const name of stream) {
    if (compareVersionDesc(meta.version, tagToMeta(repo, name).version) < 0) {
      // `meta` is newer than `name` (compareVersionDesc<0 means first is newer)
      return name
    }
  }
  return null
}

/**
 * Derive parsed-line entries (the shape parseReleaseBody returns) for the range
 * base..head in `repo`. Resolves each commit to its PR(s); a commit with no
 * associated PR (direct push) is skipped. Memoizes commit->PR lookups.
 *
 * @param {{repo:string, base:string|null, head:string,
 *   client:{compareCommits:(repo,base,head)=>Promise<{commits:Array,truncated?:boolean}>,
 *           commitPulls:(repo,sha)=>Promise<Array>}}} opts
 * @returns {Promise<{entries:Array, truncated:boolean, warning?:string}>}
 */
async function deriveEntries({ repo, base, head, client }) {
  if (!base) {
    // First release in the stream — no prior tag to diff against. Don't guess
    // the whole history; emit nothing and let the caller note it.
    return { entries: [], truncated: false, warning: `${head}: no prior tag in stream — entries not derived from compare.` }
  }
  const cmp = (await client.compareCommits(repo, base, head)) || { commits: [] }
  const commits = cmp.commits || []
  const seen = new Set()
  const entries = []
  for (const c of commits) {
    const pulls = (await client.commitPulls(repo, c.sha)) || []
    for (const pr of pulls) {
      if (seen.has(pr.number)) continue // a PR can map to multiple commits
      seen.add(pr.number)
      entries.push({ ...classifyTitle(pr.title || ''), author: pr.user || null, pr: pr.number, prRepo: repo })
    }
  }
  const truncated = cmp.truncated === true
  return {
    entries,
    truncated,
    warning: truncated ? `${head}: compare range exceeded GitHub's 250-commit cap — entry list may be incomplete; review before merge.` : undefined,
  }
}

module.exports = { deriveEntries, previousTagInStream }
