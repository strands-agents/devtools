const { test } = require('node:test')
const assert = require('node:assert/strict')
const { deriveEntries, previousTagInStream } = require('./derive-entries.cjs')

// --- deriveEntries -----------------------------------------------------------

test('derives parsed-line entries from compare commits via commit->PR', async () => {
  const client = {
    compareCommits: async () => ({
      commits: [{ sha: 'a1' }, { sha: 'b2' }],
      truncated: false,
    }),
    commitPulls: async (_repo, sha) =>
      sha === 'a1'
        ? [{ number: 1799, title: 'feat(model): add service_tier', user: 'pgrayy' }]
        : [{ number: 2087, title: 'fix: enforce user-first message', user: 'lizradway' }],
  }
  const { entries, truncated } = await deriveEntries({ repo: 'strands-agents/harness-sdk', base: 'python/v1.34.1', head: 'python/v1.35.0', client })
  assert.equal(truncated, false)
  assert.deepEqual(entries, [
    { type: 'feat', scope: 'model', breaking: false, title: 'add service_tier', author: 'pgrayy', pr: 1799, prRepo: 'strands-agents/harness-sdk' },
    { type: 'fix', scope: null, breaking: false, title: 'enforce user-first message', author: 'lizradway', pr: 2087, prRepo: 'strands-agents/harness-sdk' },
  ])
})

test('dedups a PR that spans multiple commits', async () => {
  const client = {
    compareCommits: async () => ({ commits: [{ sha: 'a1' }, { sha: 'a2' }], truncated: false }),
    commitPulls: async () => [{ number: 500, title: 'feat: x', user: 'a' }], // same PR on both commits
  }
  const { entries } = await deriveEntries({ repo: 'r', base: 'v1', head: 'v2', client })
  assert.equal(entries.length, 1)
  assert.equal(entries[0].pr, 500)
})

test('skips commits with no associated PR (direct push)', async () => {
  const client = {
    compareCommits: async () => ({ commits: [{ sha: 'a1' }, { sha: 'a2' }], truncated: false }),
    commitPulls: async (_r, sha) => (sha === 'a1' ? [{ number: 7, title: 'fix: y', user: 'b' }] : []),
  }
  const { entries } = await deriveEntries({ repo: 'r', base: 'v1', head: 'v2', client })
  assert.deepEqual(entries.map((e) => e.pr), [7])
})

test('no prior tag → no entries, with a warning', async () => {
  const { entries, warning } = await deriveEntries({ repo: 'r', base: null, head: 'v0.1.0', client: {} })
  assert.deepEqual(entries, [])
  assert.match(warning, /no prior tag/)
})

test('truncated compare range yields a warning', async () => {
  const client = {
    compareCommits: async () => ({ commits: [{ sha: 'a1' }], truncated: true }),
    commitPulls: async () => [{ number: 1, title: 'feat: z', user: 'c' }],
  }
  const { truncated, warning } = await deriveEntries({ repo: 'r', base: 'v1', head: 'v2', client })
  assert.equal(truncated, true)
  assert.match(warning, /250-commit cap/)
})

// --- previousTagInStream -----------------------------------------------------

const harnessTags = (names) => ({ listTags: async () => names.map((name) => ({ name })) })

test('finds the immediate predecessor in the python stream', async () => {
  const client = harnessTags(['python/v1.36.0', 'python/v1.35.0', 'python/v1.34.1', 'typescript/v1.5.0', 'python/v1.34.0'])
  const prior = await previousTagInStream('strands-agents/harness-sdk', 'python/v1.35.0', client)
  assert.equal(prior, 'python/v1.34.1')
})

test('ignores tags from other streams', async () => {
  // typescript tags must not be chosen as the prior for a python release
  const client = harnessTags(['typescript/v1.9.0', 'python/v1.20.0', 'typescript/v1.8.0'])
  const prior = await previousTagInStream('strands-agents/harness-sdk', 'python/v1.21.0', client)
  assert.equal(prior, 'python/v1.20.0')
})

test('orders numerically, not lexically (v1.9.0 precedes v1.10.0)', async () => {
  const client = harnessTags(['python/v1.10.0', 'python/v1.9.0', 'python/v1.8.0'])
  const prior = await previousTagInStream('strands-agents/harness-sdk', 'python/v1.10.0', client)
  assert.equal(prior, 'python/v1.9.0')
})

test('returns null for the first release in a stream', async () => {
  const client = harnessTags(['python/v1.0.0', 'typescript/v0.5.0'])
  const prior = await previousTagInStream('strands-agents/harness-sdk', 'python/v1.0.0', client)
  assert.equal(prior, null)
})

test('evals bare-v stream resolves its own predecessor', async () => {
  const client = { listTags: async () => ['v0.2.0', 'v0.1.17', 'v0.1.16'].map((name) => ({ name })) }
  const prior = await previousTagInStream('strands-agents/evals', 'v0.2.0', client)
  assert.equal(prior, 'v0.1.17')
})
