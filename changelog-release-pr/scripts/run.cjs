// Entry-point logic: pick releases (single tag or full backfill), build each
// into a file, write it, and collect any format-drift warnings. Pure given an
// injected client + fs ops, so it's unit-testable. The github-script wrapper
// (run-action.cjs) supplies a real client built from Octokit + node:fs.

const { buildReleaseFile } = require('./build-release-file.cjs')
const { enrichFromPr } = require('./enrich.cjs')

/**
 * @param {{
 *   repo:string,
 *   mode:'single'|'backfill',
 *   tag?:string,
 *   skipExisting?:boolean,
 *   client:{ listReleases:(repo:string)=>Promise<any[]>, getRelease:(repo:string,tag:string)=>Promise<any|null>, getPr:(repo:string,num:number)=>Promise<any|null> },
 *   readExisting:(path:string)=>Promise<string|null>,
 *   writeFile:(path:string,contents:string)=>Promise<void>,
 * }} opts
 * @returns {Promise<{written:string[], warnings:string[]}>}
 */
async function run(opts) {
  const warnings = []
  let releases
  if (opts.mode === 'backfill') {
    releases = await opts.client.listReleases(opts.repo)
  } else {
    const r = await opts.client.getRelease(opts.repo, opts.tag)
    releases = r ? [r] : []
    if (!r) warnings.push(`${opts.repo}: no release found for tag "${opts.tag || ''}" — nothing to sync.`)
  }

  // Skip drafts (no published_at) and prereleases — the changelog covers
  // published, stable releases only.
  releases = releases.filter((r) => r && r.published_at && !r.prerelease)

  // Memoize PR fetches: a first-time contributor's PR usually also appears in
  // "What's Changed", and on the monorepo each fetch includes a paginated file
  // list — caching roughly halves API spend on a backfill.
  const prCache = new Map()
  const getPr = (repo, num) => {
    const key = `${repo}#${num}`
    if (!prCache.has(key)) prCache.set(key, opts.client.getPr(repo, num))
    return prCache.get(key)
  }

  const deps = {
    enrich: (prRepo, pr) => enrichFromPr(prRepo, pr, getPr),
    readExisting: opts.readExisting,
    skipExisting: opts.skipExisting === true,
  }

  const written = []
  for (const release of releases) {
    const built = await buildReleaseFile(opts.repo, release, deps)
    if (!built) continue
    await opts.writeFile(built.path, built.contents)
    written.push(built.path)
    if (built.warning) warnings.push(built.warning)
  }
  return { written, warnings }
}

module.exports = { run }
