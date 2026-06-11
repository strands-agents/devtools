const { test } = require('node:test')
const assert = require('node:assert/strict')
const { parseReleaseBody, countChangelogBullets } = require('./parse-release-body.cjs')

const body = `## What's Changed

* fix(tests): fix flaky tests by @lizradway in https://github.com/strands-agents/sdk-python/pull/2319
* feat(gemini): plumb through cache tokens by @yatszhash in https://github.com/strands-agents/sdk-python/pull/2287
* feat!: drop legacy run() by @pgrayy in https://github.com/strands-agents/sdk-python/pull/2299
* docs: tidy readme by @someone in https://github.com/strands-agents/sdk-python/pull/2300
* chore: bump deps in https://github.com/strands-agents/sdk-python/pull/2301

**Full Changelog**: https://github.com/strands-agents/sdk-python/compare/python/v1.41.0...python/v1.42.0`

test('parses conventional-commit lines', () => {
  const lines = parseReleaseBody(body)
  assert.equal(lines.length, 5)
  assert.deepEqual(lines[0], {
    type: 'fix', scope: 'tests', breaking: false,
    title: 'fix flaky tests', author: 'lizradway',
    pr: 2319, prRepo: 'strands-agents/sdk-python',
  })
  assert.equal(lines[1].type, 'feat')
  assert.equal(lines[1].scope, 'gemini')
  assert.equal(lines[2].breaking, true) // '!' marker
  assert.equal(lines[2].type, 'feat')
  assert.equal(lines[4].author, null) // line without "by @author"
  assert.equal(lines[4].pr, 2301)
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

test('countChangelogBullets stays loose vs strict parser (drift signal)', () => {
  // PR refs as #123 instead of full URL: loose counter sees them, strict parser does not.
  const drifted = '* updated thing #11\n* fixed thing #12\n* added thing #13'
  assert.equal(countChangelogBullets(drifted), 3)
  assert.equal(parseReleaseBody(drifted).length, 0)
})
