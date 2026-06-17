const { test } = require('node:test')
const assert = require('node:assert/strict')
const { enrichFromPr } = require('./enrich.cjs')

test('extracts areas, commit, author', async () => {
  const fetcher = async (repo, num) => {
    assert.equal(repo, 'strands-agents/harness-sdk')
    assert.equal(num, 2287)
    return {
      labels: ['enhancement', 'python', 'area-model', 'area-otel', 'size/xs'],
      merge_commit_sha: '155239dca769c8eea7652b2496822ea47283a1a9',
      user: 'yatszhash',
    }
  }
  const e = await enrichFromPr('strands-agents/harness-sdk', 2287, fetcher)
  assert.deepEqual(e.areas, ['model', 'otel'])
  assert.equal(e.breaking, false)
  assert.equal(e.commit, '155239d')
  assert.equal(e.author, 'yatszhash')
})

test('detects breaking label', async () => {
  const f = async () => ({ labels: ['breaking change'], merge_commit_sha: 'abcdef0123', user: 'x' })
  const e = await enrichFromPr('r', 1, f)
  assert.equal(e.breaking, true)
})

test('missing pr degrades gracefully (fetcher returns null)', async () => {
  const f = async () => null
  const e = await enrichFromPr('r', 1, f)
  assert.deepEqual(e, { areas: [], breaking: false, commit: null, author: null, languages: null, docsOnly: false })
})

test('docsOnly true when every file is under site/ or docs/', async () => {
  const f = async () => ({ labels: [], merge_commit_sha: 'abc1234', user: 'x', files: ['site/src/content/blog/post.md', 'docs/guide.md'] })
  const e = await enrichFromPr('r', 1, f)
  assert.equal(e.docsOnly, true)
})

test('docsOnly false when a PR also touches code', async () => {
  const f = async () => ({ labels: [], merge_commit_sha: 'abc1234', user: 'x', files: ['site/blog/post.md', 'strands-py/src/agent.py'] })
  const e = await enrichFromPr('r', 1, f)
  assert.equal(e.docsOnly, false)
})

test('docsOnly false on unknown or empty file info (do not drop)', async () => {
  const unknown = await enrichFromPr('r', 1, async () => ({ labels: [], merge_commit_sha: 'a', user: 'x' }))
  assert.equal(unknown.docsOnly, false)
  const empty = await enrichFromPr('r', 1, async () => ({ labels: [], merge_commit_sha: 'a', user: 'x', files: [] }))
  assert.equal(empty.docsOnly, false)
})

test('docsOnly true for top-level doc files (README, AGENTS.md, CONTRIBUTING)', async () => {
  const f = async () => ({ labels: [], merge_commit_sha: 'a', user: 'x', files: ['README.md', 'AGENTS.md', 'CONTRIBUTING'] })
  const e = await enrichFromPr('r', 1, f)
  assert.equal(e.docsOnly, true)
})

test('docsOnly false when a root doc PR also touches code', async () => {
  const f = async () => ({ labels: [], merge_commit_sha: 'a', user: 'x', files: ['README.md', 'strands-py/src/agent.py'] })
  const e = await enrichFromPr('r', 1, f)
  assert.equal(e.docsOnly, false)
})

test('docsOnly false for a top-level non-doc file (e.g. pyproject.toml)', async () => {
  // A root config/build file is not docs — don't drop a release-affecting change.
  const f = async () => ({ labels: [], merge_commit_sha: 'a', user: 'x', files: ['pyproject.toml'] })
  const e = await enrichFromPr('r', 1, f)
  assert.equal(e.docsOnly, false)
})

test('no merge sha yields null commit', async () => {
  const f = async () => ({ labels: [], merge_commit_sha: null, user: null })
  const e = await enrichFromPr('r', 1, f)
  assert.equal(e.commit, null)
  assert.equal(e.author, null)
})

test('derives languages from monorepo top-level dirs', async () => {
  const f = async () => ({ labels: [], merge_commit_sha: 'abc1234', user: 'x', files: ['strands-py/src/agent.py', 'strands-py/tests/t.py'] })
  const e = await enrichFromPr('r', 1, f)
  assert.deepEqual(e.languages, ['python'])
})

test('PR touching both sdk dirs yields both languages', async () => {
  const f = async () => ({ labels: [], merge_commit_sha: 'abc1234', user: 'x', files: ['strands-py/a.py', 'strands-ts/b.ts'] })
  const e = await enrichFromPr('r', 1, f)
  assert.deepEqual(e.languages.sort(), ['python', 'typescript'])
})

test('site/ci/docs-only PR yields empty languages', async () => {
  const f = async () => ({ labels: [], merge_commit_sha: 'abc1234', user: 'x', files: ['site/src/page.astro', '.github/workflows/x.yml', 'designs/d.md'] })
  const e = await enrichFromPr('r', 1, f)
  assert.deepEqual(e.languages, [])
})

test('missing files info yields null languages (unknown — keep everywhere)', async () => {
  const f = async () => ({ labels: [], merge_commit_sha: 'abc1234', user: 'x' })
  const e = await enrichFromPr('r', 1, f)
  assert.equal(e.languages, null)
})
