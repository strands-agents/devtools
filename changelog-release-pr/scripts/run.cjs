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
 *   client:{ listReleases:(repo:string)=>Promise<any[]>, getRelease:(repo:string,tag:string)=>Promise<any|null>, getPr:(repo:string,num:number)=>Promise<any|null> },
 *   readExisting:(path:string)=>Promise<string|null>,
 *   writeFile:(path:string,contents:string)=>Promise<void>,
 * }} opts
 * @returns {Promise<{written:string[], warnings:string[]}>}
 */
async function run(opts) {
  let releases
  if (opts.mode === 'backfill') {
    releases = await opts.client.listReleases(opts.repo)
  } else {
    const r = await opts.client.getRelease(opts.repo, opts.tag)
    releases = r ? [r] : []
  }

  // listReleases includes drafts (no published_at) — skip them; a changelog
  // only covers published releases.
  releases = releases.filter((r) => r && r.published_at)

  const deps = {
    enrich: (prRepo, pr) => enrichFromPr(prRepo, pr, opts.client.getPr),
    readExisting: opts.readExisting,
  }

  const written = []
  const warnings = []
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
