// Parse GitHub's auto-generated "What's Changed" release body into structured
// lines. Pure, dependency-free.
//
// Line shape: "* <type>(<scope>)!: <title> by @<author> in <pr-url>"
// (scope, '!' marker, and "by @author" are all optional).

const LINE = /^\s*[-*]\s+(.*)$/
// "<msg> by @<author> in <ref>", where <ref> is either a full PR URL (older
// auto-generated notes) or a short "#<n>" (newer curated notes). "by @author"
// is optional. For the short form there's no repo in the ref, so prRepo is
// null and the caller defaults it to the release's own repo.
// Author logins may carry a bracket suffix for apps/bots, e.g. dependabot[bot].
const TAIL =
  /^(.*?)(?:\s+by\s+@([\w-]+(?:\[[\w-]+\])?))?\s+in\s+(?:https?:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)|#(\d+))\s*$/i
// Cross-SDK marker in newer harness notes, e.g. "title _(shared with TS)_".
// Language gating (from PR changed files) already decides stream membership,
// so strip this annotation from the rendered title.
const SHARED_ANNOTATION = /\s*_\(shared with [^)]+\)_\s*$/i
const CONVENTIONAL = /^(feat|fix|docs|style|refactor|perf|test|chore|build|ci|revert)(?:\(([^)]+)\))?(!)?:\s*(.+)$/i
const KNOWN_TYPES = new Set(['feat', 'fix', 'docs', 'perf', 'refactor', 'test', 'chore'])
// GitHub's "## New Contributors" lines: "@login made their first contribution in <pr-url>".
// These are celebrated separately (parseNewContributors) and must NOT become entries.
// Login pattern must mirror TAIL's (incl. the bracket suffix for bots like dependabot[bot]).
const FIRST_CONTRIBUTION = /^@([\w-]+(?:\[[\w-]+\])?) made their first contribution\s+in\s+https?:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)\s*$/i

/**
 * Classify a PR title / commit message into changelog fields. Strips the
 * cross-SDK "_(shared with …)_" annotation, then applies the conventional-commit
 * grammar (type/scope/!), falling back to type "other" for non-conventional
 * titles. Shared by parseReleaseBody and the compare-driven derive-entries path.
 * @param {string} message
 * @returns {{type:string, scope:string|null, breaking:boolean, title:string}}
 */
function classifyTitle(message) {
  const msg = String(message).trim().replace(SHARED_ANNOTATION, '')
  const cc = msg.match(CONVENTIONAL)
  if (cc) {
    const type = cc[1].toLowerCase()
    return { type: KNOWN_TYPES.has(type) ? type : 'other', scope: cc[2] || null, breaking: cc[3] === '!', title: cc[4].trim() }
  }
  return { type: 'other', scope: null, breaking: false, title: msg }
}

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
    const author = tail[2] || null
    // Full-URL form fills groups 3 (repo) + 4 (pr); short "#n" form fills group 5
    // with no repo, so prRepo stays null and the caller uses the release repo.
    const prRepo = tail[3] || null
    const pr = tail[4] ? Number(tail[4]) : tail[5] ? Number(tail[5]) : null
    out.push({ ...classifyTitle(tail[1]), author, pr, prRepo })
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

module.exports = { parseReleaseBody, classifyTitle, countChangelogBullets, parseNewContributors }
