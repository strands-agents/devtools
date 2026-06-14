// Parse GitHub's auto-generated "What's Changed" release body into structured
// lines. Pure, dependency-free.
//
// Line shape: "* <type>(<scope>)!: <title> by @<author> in <pr-url>"
// (scope, '!' marker, and "by @author" are all optional).

const LINE = /^\s*[-*]\s+(.*)$/
// "<msg> by @<author> in <pr-url>"  OR  "<msg> in <pr-url>"
// Author logins may carry a bracket suffix for apps/bots, e.g. dependabot[bot].
const TAIL = /^(.*?)(?:\s+by\s+@([\w-]+(?:\[[\w-]+\])?))?\s+in\s+https?:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)\s*$/i
const CONVENTIONAL = /^(feat|fix|docs|style|refactor|perf|test|chore|build|ci|revert)(?:\(([^)]+)\))?(!)?:\s*(.+)$/i
const KNOWN_TYPES = new Set(['feat', 'fix', 'docs', 'perf', 'refactor', 'test', 'chore'])
// GitHub's "## New Contributors" lines: "@login made their first contribution in <pr-url>".
// These are celebrated separately (parseNewContributors) and must NOT become entries.
// Login pattern must mirror TAIL's (incl. the bracket suffix for bots like dependabot[bot]).
const FIRST_CONTRIBUTION = /^@([\w-]+(?:\[[\w-]+\])?) made their first contribution\s+in\s+https?:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)\s*$/i

/**
 * @param {string|null|undefined} body
 * @returns {Array<{type:string,scope:string|null,breaking:boolean,title:string,author:string|null,pr:number|null,prRepo:string|null}>}
 */
function parseReleaseBody(body) {
  if (!body) return []
  const out = []
  // GitHub release bodies use CRLF; normalize so trailing \r doesn't break matches.
  for (const raw of body.replace(/\r\n?/g, '\n').split('\n')) {
    const li = raw.match(LINE)
    if (!li) continue
    if (FIRST_CONTRIBUTION.test(li[1].trim())) continue // celebrated separately
    const tail = li[1].trim().match(TAIL)
    if (!tail) continue // not an itemized PR line (skips "omitted" notes, footers)
    const message = tail[1].trim()
    const author = tail[2] || null
    const prRepo = tail[3] || null
    const pr = tail[4] ? Number(tail[4]) : null

    const cc = message.match(CONVENTIONAL)
    if (cc) {
      const type = cc[1].toLowerCase()
      out.push({
        type: KNOWN_TYPES.has(type) ? type : 'other',
        scope: cc[2] || null,
        breaking: cc[3] === '!',
        title: cc[4].trim(),
        author, pr, prRepo,
      })
    } else {
      out.push({ type: 'other', scope: null, breaking: false, title: message, author, pr, prRepo })
    }
  }
  return out
}

// Loosely count bullets that look like changelog entries, using a format-
// INDEPENDENT heuristic (a list bullet mentioning an author '@' or a PR
// '#'/'/pull/'). Deliberately looser than parseReleaseBody's strict TAIL: if
// this is high but parseReleaseBody returns far fewer, the release-note format
// has likely drifted (or notes are hand-written) and callers should flag it.
const LOOSE_ENTRY = /(@[\w-]+|#\d+|\/pull\/\d+)/
function countChangelogBullets(body) {
  if (!body) return 0
  let n = 0
  for (const raw of body.replace(/\r\n?/g, '\n').split('\n')) {
    const li = raw.match(LINE)
    if (li && LOOSE_ENTRY.test(li[1]) && !FIRST_CONTRIBUTION.test(li[1].trim())) n++
  }
  return n
}

/**
 * Extract GitHub's "## New Contributors" section into structured data.
 * prRepo is the repo from the PR url (may differ from the release's repo for
 * pre-monorepo releases) — gate/enrich against THAT repo, mirroring entries.
 * @param {string|null|undefined} body
 * @returns {Array<{login:string, pr:number, prRepo:string}>}
 */
function parseNewContributors(body) {
  if (!body) return []
  const out = []
  for (const raw of body.replace(/\r\n?/g, '\n').split('\n')) {
    const li = raw.match(LINE)
    if (!li) continue
    const m = li[1].trim().match(FIRST_CONTRIBUTION)
    if (m) out.push({ login: m[1], pr: Number(m[3]), prRepo: m[2] })
  }
  return out
}

module.exports = { parseReleaseBody, countChangelogBullets, parseNewContributors }
