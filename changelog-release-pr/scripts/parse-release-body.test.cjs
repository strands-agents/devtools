const { test } = require('node:test')
const assert = require('node:assert/strict')
const { parseReleaseBody, countChangelogBullets, parseNewContributors } = require('./parse-release-body.cjs')

const body = `## What's Changed

* fix(tests): fix flaky tests by @lizradway in https://github.com/strands-agents/sdk-python/pull/2319
* feat(gemini): plumb through cache tokens by @yatszhash in https://github.com/strands-agents/sdk-python/pull/2287
* feat!: drop legacy run() by @pgrayy in https://github.com/strands-agents/sdk-python/pull/2299
* docs: tidy readme by @someone in https://github.com/strands-agents/sdk-python/pull/2300
* chore: bump deps in https://github.com/strands-agents/sdk-python/pull/2301

**Full Changelog**: https://github.com/strands-agents/sdk-python/compare/python/v1.41.0...python/v1.42.0`

test('parses conventional-commit lines', () => {
  // Whole-shape equality: the parser is deterministic, so assert everything.
  const repo = 'strands-agents/sdk-python'
  assert.deepEqual(parseReleaseBody(body), [
    { type: 'fix', scope: 'tests', breaking: false, title: 'fix flaky tests', author: 'lizradway', pr: 2319, prRepo: repo },
    { type: 'feat', scope: 'gemini', breaking: false, title: 'plumb through cache tokens', author: 'yatszhash', pr: 2287, prRepo: repo },
    { type: 'feat', scope: null, breaking: true, title: 'drop legacy run()', author: 'pgrayy', pr: 2299, prRepo: repo },
    { type: 'docs', scope: null, breaking: false, title: 'tidy readme', author: 'someone', pr: 2300, prRepo: repo },
    { type: 'chore', scope: null, breaking: false, title: 'bump deps', author: null, pr: 2301, prRepo: repo },
  ])
})

test('omitted-list body yields no entries', () => {
  assert.deepEqual(
    parseReleaseBody("## What's Changed\n\n*auto-generated itemized list omitted due to mono-repository merge*"),
    []
  )
})

test('empty/undefined body yields no entries', () => {
  assert.deepEqual(parseReleaseBody(''), [])
  assert.deepEqual(parseReleaseBody(null), [])
})

test('bot authors with bracket suffix parse cleanly (no title pollution)', () => {
  const lines = parseReleaseBody(
    '* chore(deps): bump uuid from 10.0.0 to 13.0.0 by @dependabot[bot] in https://github.com/o/r/pull/625'
  )
  assert.equal(lines.length, 1)
  assert.equal(lines[0].title, 'bump uuid from 10.0.0 to 13.0.0')
  assert.equal(lines[0].author, 'dependabot[bot]')
  assert.equal(lines[0].pr, 625)
})

test('non-conventional line falls back to type other', () => {
  const lines = parseReleaseBody('* just did a thing by @x in https://github.com/o/r/pull/9')
  assert.equal(lines[0].type, 'other')
  assert.equal(lines[0].scope, null)
  assert.equal(lines[0].title, 'just did a thing')
})

test('countChangelogBullets counts entry-like bullets, ignores notes', () => {
  assert.equal(countChangelogBullets(body), 5)
  assert.equal(countChangelogBullets('* auto-generated itemized list omitted'), 0)
  assert.equal(countChangelogBullets(''), 0)
})

test('handles CRLF line endings (real GitHub bodies)', () => {
  const crlf = "## What's Changed\r\n\r\n* feat(model): x by @a in https://github.com/o/r/pull/1\r\n* fix: y by @b in https://github.com/o/r/pull/2\r\n"
  const lines = parseReleaseBody(crlf)
  assert.equal(lines.length, 2)
  assert.equal(lines[0].scope, 'model')
  assert.equal(lines[0].title, 'x') // no trailing \r
  assert.equal(countChangelogBullets(crlf), 2)
})

const nc = `## What's Changed
* feat: real change by @dev in https://github.com/o/r/pull/1

## New Contributors
* @senthilkumarmohan made their first contribution in https://github.com/strands-agents/harness-sdk/pull/2623
* @ianholtz made their first contribution in https://github.com/strands-agents/harness-sdk/pull/2651

**Full Changelog**: https://github.com/o/r/compare/a...b`

test('parseNewContributors extracts structured logins + prs + prRepo', () => {
  assert.deepEqual(parseNewContributors(nc), [
    { login: 'senthilkumarmohan', pr: 2623, prRepo: 'strands-agents/harness-sdk' },
    { login: 'ianholtz', pr: 2651, prRepo: 'strands-agents/harness-sdk' },
  ])
  assert.deepEqual(parseNewContributors(''), [])
  assert.deepEqual(parseNewContributors(null), [])
})

test('bracket-suffixed bot first-contribution lines are captured, not leaked into entries', () => {
  const body = '* @dependabot[bot] made their first contribution in https://github.com/o/r/pull/625'
  assert.deepEqual(parseNewContributors(body), [{ login: 'dependabot[bot]', pr: 625, prRepo: 'o/r' }])
  assert.deepEqual(parseReleaseBody(body), []) // must NOT become an entry
})

test('first-contribution lines are excluded from entries', () => {
  const lines = parseReleaseBody(nc)
  assert.equal(lines.length, 1)
  assert.equal(lines[0].title, 'real change')
})

test('countChangelogBullets ignores first-contribution lines (no false drift)', () => {
  assert.equal(countChangelogBullets(nc), 1)
})

test('countChangelogBullets stays loose vs strict parser (drift signal)', () => {
  // PR refs as #123 instead of full URL: loose counter sees them, strict parser does not.
  const drifted = '* updated thing #11\n* fixed thing #12\n* added thing #13'
  assert.equal(countChangelogBullets(drifted), 3)
  assert.equal(parseReleaseBody(drifted).length, 0)
})
