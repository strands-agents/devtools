const { test } = require('node:test')
const assert = require('node:assert/strict')
const { run } = require('./run.cjs')

const releases = [
  { tag_name: 'python/v1.42.0', published_at: '2026-06-01T00:00:00Z', html_url: 'h1', body: '* feat: a by @x in https://github.com/strands-agents/sdk-python/pull/1\n' },
  { tag_name: 'python-wasm/v0.0.1', published_at: '2026-06-02T00:00:00Z', html_url: 'h2', body: '' },
]

function fakeClient() {
  return {
    listReleases: async () => releases,
    getRelease: async (_r, tag) => releases.find((x) => x.tag_name === tag) || null,
    getPr: async () => ({ labels: ['area-model'], merge_commit_sha: 'abc1234', user: 'x' }),
  }
}

test('backfill writes one file per in-scope release', async () => {
  const written = {}
  const res = await run({
    repo: 'strands-agents/harness-sdk', mode: 'backfill', client: fakeClient(),
    readExisting: async () => null,
    writeFile: async (p, c) => { written[p] = c },
  })
  assert.deepEqual(Object.keys(written), ['site/src/content/changelog/harness/python-v1.42.0.md'])
  // enrichment landed: area-model label → areas: [model]
  assert.match(written['site/src/content/changelog/harness/python-v1.42.0.md'], /areas: \[model\]/)
  assert.deepEqual(res.warnings, [])
})

test('skipExisting skips releases with files and never calls enrichment for them', async () => {
  let prCalls = 0
  const client = {
    ...fakeClient(),
    getPr: async () => { prCalls++; return { labels: [], merge_commit_sha: 'abc1234', user: 'x' } },
  }
  const written = {}
  const res = await run({
    repo: 'strands-agents/harness-sdk', mode: 'backfill', skipExisting: true, client,
    readExisting: async () => '---\nsdk: harness\n---\n', // every file already exists
    writeFile: async (p, c) => { written[p] = c },
  })
  assert.deepEqual(res.written, [])
  assert.equal(prCalls, 0) // existence checked BEFORE enrichment
})

test('single mode writes only the given tag', async () => {
  const written = {}
  await run({
    repo: 'strands-agents/harness-sdk', mode: 'single', tag: 'python/v1.42.0',
    client: fakeClient(), readExisting: async () => null,
    writeFile: async (p, c) => { written[p] = c },
  })
  assert.deepEqual(Object.keys(written), ['site/src/content/changelog/harness/python-v1.42.0.md'])
})

test('single mode with unknown tag writes nothing and warns', async () => {
  const written = {}
  const res = await run({
    repo: 'strands-agents/harness-sdk', mode: 'single', tag: 'python/v9.9.9',
    client: fakeClient(), readExisting: async () => null,
    writeFile: async (p, c) => { written[p] = c },
  })
  assert.deepEqual(Object.keys(written), [])
  assert.deepEqual(res.written, [])
  assert.match(res.warnings[0], /no release found for tag "python\/v9\.9\.9"/)
})

test('prereleases are skipped', async () => {
  const pre = [{ tag_name: 'v2.0.0', published_at: '2026-06-01T00:00:00Z', html_url: 'h', prerelease: true, body: '* feat: x by @a in https://github.com/o/r/pull/1\n' }]
  const client = { listReleases: async () => pre, getRelease: async () => null, getPr: async () => null }
  const res = await run({
    repo: 'strands-agents/evals', mode: 'backfill', client,
    readExisting: async () => null, writeFile: async () => { throw new Error('must not write') },
  })
  assert.deepEqual(res.written, [])
})

test('memoizes PR fetches across entries and newContributors', async () => {
  let prCalls = 0
  const rel = [{
    tag_name: 'python/v9.0.0', published_at: '2026-06-01T00:00:00Z', html_url: 'h',
    body: [
      '* feat: thing by @newdev in https://github.com/strands-agents/harness-sdk/pull/7',
      '',
      '## New Contributors',
      '* @newdev made their first contribution in https://github.com/strands-agents/harness-sdk/pull/7',
    ].join('\n'),
  }]
  const client = {
    listReleases: async () => rel, getRelease: async () => null,
    getPr: async () => { prCalls++; return { labels: [], merge_commit_sha: 'abc1234', user: 'newdev', files: ['strands-py/x.py'] } },
  }
  await run({ repo: 'strands-agents/harness-sdk', mode: 'backfill', client, readExisting: async () => null, writeFile: async () => {} })
  assert.equal(prCalls, 1) // entry + contributor gating share one fetch
})

test('skips draft releases (null published_at) without crashing', async () => {
  const withDraft = [
    { tag_name: 'v1.0.0', published_at: null, html_url: 'h', body: '* feat: x by @a in https://github.com/o/r/pull/1\n' },
    { tag_name: 'v0.9.0', published_at: '2026-01-01T00:00:00Z', html_url: 'h', body: '* fix: y by @b in https://github.com/o/r/pull/2\n' },
  ]
  const client = { listReleases: async () => withDraft, getRelease: async () => null, getPr: async () => null }
  const written = {}
  const res = await run({
    repo: 'strands-agents/evals', mode: 'backfill', client,
    readExisting: async () => null, writeFile: async (p, c) => { written[p] = c },
  })
  // only the published one is written
  assert.deepEqual(res.written, ['site/src/content/changelog/evals/v0.9.0.md'])
})

test('collects drift warnings', async () => {
  const drifty = [{ tag_name: 'v1.0.0', published_at: '2026-01-01T00:00:00Z', html_url: 'h', body: '* a #1\n* b #2\n* c #3\n' }]
  const client = { listReleases: async () => drifty, getRelease: async () => null, getPr: async () => null }
  const res = await run({
    repo: 'strands-agents/harness-sdk', mode: 'backfill', client,
    readExisting: async () => null, writeFile: async () => {},
  })
  assert.equal(res.warnings.length, 1)
  assert.match(res.warnings[0], /parsed 0 of 3/)
})
