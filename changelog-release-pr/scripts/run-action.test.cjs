const { test } = require('node:test')
const assert = require('node:assert/strict')
const { branchName } = require('./run-action.cjs')

test('single mode: branch is keyed by the (git-sanitized) tag', () => {
  assert.equal(
    branchName('strands-agents/harness-sdk', 'python/v1.43.0', []),
    'changelog/sync-harness-sdk-python-v1.43.0'
  )
})

test('branch slug comes from the repo name, not the owner', () => {
  // Archived sdk-typescript and harness-sdk must never collide on one branch.
  assert.match(branchName('strands-agents/sdk-typescript', 'v0.1.0', []), /^changelog\/sync-sdk-typescript-/)
})

test('backfill mode: branch is a content hash of the written set', () => {
  const b = branchName('strands-agents/harness-sdk', '', ['a.md', 'b.md'])
  assert.match(b, /^changelog\/sync-harness-sdk-backfill-[0-9a-f]{12}$/)
})

test('backfill: same files (any order) → same branch, so a re-run updates one PR', () => {
  const a = branchName('strands-agents/harness-sdk', '', ['a.md', 'b.md'])
  const b = branchName('strands-agents/harness-sdk', '', ['b.md', 'a.md'])
  assert.equal(a, b)
})

test('backfill: different files → different branch, so independent runs get separate PRs', () => {
  const week1 = branchName('strands-agents/harness-sdk', '', ['python-v1.43.0.md'])
  const week2 = branchName('strands-agents/harness-sdk', '', ['python-v1.44.0.md'])
  assert.notEqual(week1, week2)
})

test('backfill: undefined tag is treated the same as empty (hash path)', () => {
  assert.equal(
    branchName('strands-agents/harness-sdk', undefined, ['a.md']),
    branchName('strands-agents/harness-sdk', '', ['a.md'])
  )
})
