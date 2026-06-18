const { test } = require('node:test')
const assert = require('node:assert/strict')
const { run } = require('./run.cjs')

// Entries are now sourced from the compare API (commits between the prior tag
// and this one, each resolved to a PR), not parsed from the release body. The
// fake client below provides listReleases/getRelease plus the compare surface:
// listTags (prior-tag detection), compareCommits, and commitPulls.

const releases = [
  { tag_name: 'python/v1.42.0', published_at: '2026-06-01T00:00:00Z', html_url: 'h1', body: '' },
  { tag_name: 'python/v1.41.0', published_at: '2026-05-21T00:00:00Z', html_url: 'h0', body: '' },
  { tag_name: 'python-wasm/v0.0.1', published_at: '2026-06-02T00:00:00Z', html_url: 'h2', body: '' },
]

function fakeClient(overrides = {}) {
  return {
    listReleases: async () => releases,
    getRelease: async (_r, tag) => releases.find((x) => x.tag_name === tag) || null,
    listTags: async () => releases.map((r) => ({ name: r.tag_name })),
    // One PR (#1) merged between v1.41.0 and v1.42.0.
    compareCommits: async (_r, base, head) =>
      base === 'python/v1.41.0' && head === 'python/v1.42.0'
        ? { commits: [{ sha: 's1' }], truncated: false }
        : { commits: [], truncated: false },
    commitPulls: async (_r, sha) => (sha === 's1' ? [{ number: 1, title: 'feat: a', user: 'x' }] : []),
    getPr: async () => ({ labels: ['area-model'], merge_commit_sha: 'abc1234', user: 'x', files: ['strands-py/a.py'] }),
    ...overrides,
  }
}

test('backfill writes one file per in-scope release with compare-derived entries', async () => {
  const written = {}
  const res = await run({
    repo: 'strands-agents/harness-sdk', mode: 'backfill', client: fakeClient(),
    readExisting: async () => null,
    writeFile: async (p, c) => { written[p] = c },
  })
  // python-wasm is out of scope; v1.41.0 is the first in-scope release (no prior
  // tag → no entries) but still gets a file; v1.42.0 gets the derived entry.
  assert.ok(written['site/src/content/changelog/harness/python-v1.42.0.md'])
  assert.match(written['site/src/content/changelog/harness/python-v1.42.0.md'], /title: "a"/)
  // enrichment landed: area-model label → areas: [model]
  assert.match(written['site/src/content/changelog/harness/python-v1.42.0.md'], /areas: \[model\]/)
  assert.ok(!Object.keys(written).some((p) => p.includes('wasm')))
})

test('skipExisting skips releases with files and never calls enrichment for them', async () => {
  let prCalls = 0
  const client = fakeClient({ getPr: async () => { prCalls++; return { labels: [], merge_commit_sha: 'abc1234', user: 'x' } } })
  const res = await run({
    repo: 'strands-agents/harness-sdk', mode: 'backfill', skipExisting: true, client,
    readExisting: async () => '---\nsdk: harness\n---\n', // every file already exists
    writeFile: async () => {},
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
  const pre = [{ tag_name: 'v2.0.0', published_at: '2026-06-01T00:00:00Z', html_url: 'h', prerelease: true, body: '' }]
  const client = fakeClient({ listReleases: async () => pre, getRelease: async () => null })
  const res = await run({
    repo: 'strands-agents/evals', mode: 'backfill', client,
    readExisting: async () => null, writeFile: async () => { throw new Error('must not write') },
  })
  assert.deepEqual(res.written, [])
})

test('memoizes PR fetches across entries and newContributors', async () => {
  let prCalls = 0
  const rel = [
    { tag_name: 'python/v9.0.0', published_at: '2026-06-01T00:00:00Z', html_url: 'h',
      body: '## New Contributors\n* @newdev made their first contribution in https://github.com/strands-agents/harness-sdk/pull/7' },
    { tag_name: 'python/v8.9.0', published_at: '2026-05-01T00:00:00Z', html_url: 'h0', body: '' },
  ]
  const client = fakeClient({
    listReleases: async () => rel,
    getRelease: async () => null,
    listTags: async () => rel.map((r) => ({ name: r.tag_name })),
    compareCommits: async (_r, base, head) =>
      head === 'python/v9.0.0' ? { commits: [{ sha: 's7' }], truncated: false } : { commits: [], truncated: false },
    commitPulls: async (_r, sha) => (sha === 's7' ? [{ number: 7, title: 'feat: thing', user: 'newdev' }] : []),
    getPr: async () => { prCalls++; return { labels: [], merge_commit_sha: 'abc1234', user: 'newdev', files: ['strands-py/x.py'] } },
  })
  await run({ repo: 'strands-agents/harness-sdk', mode: 'backfill', client, readExisting: async () => null, writeFile: async () => {} })
  assert.equal(prCalls, 1) // entry (#7) + contributor (#7) gating share one fetch
})

test('skips draft releases (null published_at) without crashing', async () => {
  const withDraft = [
    { tag_name: 'v1.0.0', published_at: null, html_url: 'h', body: '' },
    { tag_name: 'v0.9.0', published_at: '2026-01-01T00:00:00Z', html_url: 'h', body: '' },
  ]
  const client = fakeClient({ listReleases: async () => withDraft, getRelease: async () => null, listTags: async () => withDraft.map((r) => ({ name: r.tag_name })) })
  const written = {}
  const res = await run({
    repo: 'strands-agents/evals', mode: 'backfill', client,
    readExisting: async () => null, writeFile: async (p, c) => { written[p] = c },
  })
  // only the published one is written
  assert.deepEqual(res.written, ['site/src/content/changelog/evals/v0.9.0.md'])
})

test('surfaces a truncated-compare warning', async () => {
  const rel = [
    { tag_name: 'python/v2.0.0', published_at: '2026-01-02T00:00:00Z', html_url: 'h', body: '' },
    { tag_name: 'python/v1.0.0', published_at: '2026-01-01T00:00:00Z', html_url: 'h0', body: '' },
  ]
  const client = fakeClient({
    listReleases: async () => rel, getRelease: async () => null,
    listTags: async () => rel.map((r) => ({ name: r.tag_name })),
    compareCommits: async () => ({ commits: [{ sha: 's1' }], truncated: true }),
    commitPulls: async () => [{ number: 1, title: 'feat: x', user: 'a' }],
  })
  const res = await run({
    repo: 'strands-agents/harness-sdk', mode: 'backfill', client,
    readExisting: async () => null, writeFile: async () => {},
  })
  assert.ok(res.warnings.some((w) => /250-commit cap/.test(w)))
})
