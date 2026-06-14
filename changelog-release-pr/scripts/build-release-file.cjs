// Orchestrate one GitHub release into a rendered changelog file. Pure given
// injected deps (enrich + readExisting), so it's unit-testable without network.

const { tagToMeta, getPackageUrl } = require('./tag-meta.cjs')
const { parseReleaseBody, countChangelogBullets, parseNewContributors } = require('./parse-release-body.cjs')
const { renderMarkdown, mergePreserving } = require('./render-markdown.cjs')

function fileNameFor(sdk, language, version) {
  if (sdk === 'evals') return `evals/v${version}.md`
  return `harness/${language}-v${version}.md`
}

/**
 * @param {string} repo  the SOURCE repo the release belongs to
 * @param {{tag_name:string, published_at:string, html_url:string, body:string|null}} release
 * @param {{enrich:(prRepo:string,pr:number)=>Promise<{areas:string[],breaking:boolean,commit:string|null,author:string|null}>, readExisting:(path:string)=>Promise<string|null>, skipExisting?:boolean}} deps
 * @returns {Promise<{path:string, contents:string, warning?:string}|null>}
 */
async function buildReleaseFile(repo, release, deps) {
  const meta = tagToMeta(repo, release.tag_name)
  if (!meta) return null

  const path = `site/src/content/changelog/${fileNameFor(meta.sdk, meta.language, meta.version)}`
  const existing = await deps.readExisting(path)

  // skipExisting (used by the daily cron backstop): only generate files for
  // releases that don't have one yet. Checked BEFORE enrichment so a skipped
  // release costs zero PR API calls, and existing files (possibly carrying
  // richer enrichment from when labels were fresher) are never regressed by a
  // rate-limited re-run. A full refresh is an explicit backfill dispatch.
  if (deps.skipExisting && existing) return null

  const parsed = parseReleaseBody(release.body)

  // Format-drift guard: if the body clearly has changelog bullets but few/none
  // parsed, the format likely changed (or notes are hand-written). Don't fail —
  // emit the file (still links the release) and attach a warning for the PR.
  const bullets = countChangelogBullets(release.body)
  const warning =
    bullets >= 3 && parsed.length < bullets * 0.8
      ? `${release.tag_name}: parsed ${parsed.length} of ${bullets} changelog bullets — release-note format may have changed; review before merge.`
      : undefined

  // Monorepo releases (prefixed tags) list every merged PR regardless of
  // language, so gate entries by which SDK dirs the PR actually touched:
  // python stream keeps python-touching PRs, ts stream keeps ts-touching,
  // both → both, site/ci/docs-only (empty languages) → omitted everywhere,
  // unknown (file info unavailable) → kept (degrade open). Pre-monorepo
  // bare-`v` tags and evals are single-language releases — no filtering.
  const isMonorepoStream =
    meta.sdk === 'harness' &&
    (release.tag_name.startsWith('python/') || release.tag_name.startsWith('typescript/'))

  const entries = []
  for (const p of parsed) {
    const prRepo = p.prRepo || repo
    const enr = p.pr
      ? await deps.enrich(prRepo, p.pr)
      : { areas: [], breaking: false, commit: null, author: null, languages: null }
    if (isMonorepoStream && Array.isArray(enr.languages) && !enr.languages.includes(meta.language)) {
      continue
    }
    const breaking = p.breaking || enr.breaking
    entries.push({
      type: breaking && p.type === 'other' ? 'breaking' : p.type,
      breaking,
      scope: p.scope,
      areas: enr.areas,
      title: p.title,
      pr: p.pr,
      prUrl: p.pr ? `https://github.com/${prRepo}/pull/${p.pr}` : null,
      commit: enr.commit,
      commitUrl: enr.commit ? `https://github.com/${prRepo}/commit/${enr.commit}` : null,
      author: enr.author || p.author,
    })
  }

  // New contributors: language-gate like entries, with one deliberate
  // difference — a first PR that touches NO sdk dir (docs/ci/site) is still
  // celebrated in BOTH streams (entries with no language are dropped as noise;
  // people aren't noise). Unknown file info → also kept in both.
  let newContributors = parseNewContributors(release.body)
  if (isMonorepoStream) {
    const gated = []
    for (const c of newContributors) {
      // Use the PR's own repo (mirrors the entries path) — first-contribution
      // links can point at the pre-monorepo repos.
      const enr = await deps.enrich(c.prRepo || repo, c.pr)
      const langs = enr.languages
      if (!Array.isArray(langs) || langs.length === 0 || langs.includes(meta.language)) {
        gated.push(c)
      }
    }
    newContributors = gated
  }

  const file = {
    sdk: meta.sdk,
    language: meta.language,
    version: meta.version,
    tag: release.tag_name,
    date: release.published_at.slice(0, 10),
    releaseUrl: release.html_url,
    packageUrl: getPackageUrl(meta.sdk, meta.language, meta.version),
    entries,
    newContributors,
  }

  const contents = existing ? mergePreserving(file, existing) : renderMarkdown(file)
  return warning ? { path, contents, warning } : { path, contents }
}

module.exports = { buildReleaseFile }
