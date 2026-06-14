// Enrich a parsed line from its linked PR: area-* labels, breaking flag,
// short merge-commit SHA, author, and (for monorepo PRs) which SDK languages
// the PR actually touches — derived from changed-file paths because language
// labels are too sparse to rely on. Pure given an injected fetcher (no network
// here), so it's unit-testable. Degrades to empty enrichment when the PR can't
// be fetched (deleted PR, missing permissions, old repo) — callers still emit
// the entry, just without areas/commit, and with languages unknown.

/**
 * @typedef {{labels:string[], merge_commit_sha:string|null, user:string|null, files?:string[]}} PrData
 * @typedef {(repo:string, num:number)=>Promise<PrData|null>} PrFetcher
 */

// Monorepo top-level dirs that mark a PR as touching an SDK language.
const LANGUAGE_DIRS = {
  'strands-py': 'python',
  'strands-ts': 'typescript',
}

/**
 * Derive SDK languages from changed-file paths. Returns:
 * - string[] of languages (possibly empty = site/ci/docs-only PR)
 * - null when file info is unavailable (unknown — callers should not filter)
 */
function languagesFromFiles(files) {
  if (!Array.isArray(files)) return null
  const langs = new Set()
  for (const f of files) {
    const top = String(f).split('/')[0]
    if (LANGUAGE_DIRS[top]) langs.add(LANGUAGE_DIRS[top])
  }
  return [...langs]
}

/**
 * @param {string} repo
 * @param {number} num
 * @param {PrFetcher} fetcher
 * @returns {Promise<{areas:string[], breaking:boolean, commit:string|null, author:string|null, languages:string[]|null}>}
 */
async function enrichFromPr(repo, num, fetcher) {
  const pr = await fetcher(repo, num)
  if (!pr) return { areas: [], breaking: false, commit: null, author: null, languages: null }
  const areas = (pr.labels || [])
    .filter((l) => l.startsWith('area-'))
    .map((l) => l.slice('area-'.length))
  const breaking = (pr.labels || []).some((l) => l.toLowerCase() === 'breaking change')
  const commit = pr.merge_commit_sha ? pr.merge_commit_sha.slice(0, 7) : null
  return { areas, breaking, commit, author: pr.user || null, languages: languagesFromFiles(pr.files) }
}

module.exports = { enrichFromPr }
