const { test } = require('node:test')
const assert = require('node:assert/strict')
const { compareVersionDesc } = require('./semver-compare.cjs')

test('orders versions newest-first', () => {
  const sorted = ['1.0.0-rc.0', '1.0.0', '1.0.0-rc.1', '1.2.0'].sort(compareVersionDesc)
  assert.deepEqual(sorted, ['1.2.0', '1.0.0', '1.0.0-rc.1', '1.0.0-rc.0'])
})

test('numeric core comparison, not lexical (1.10.0 > 1.9.0)', () => {
  assert.ok(compareVersionDesc('1.10.0', '1.9.0') < 0)
  assert.deepEqual(['1.9.0', '1.10.0', '1.2.0'].sort(compareVersionDesc), ['1.10.0', '1.9.0', '1.2.0'])
})

test('a final release ranks above its prereleases', () => {
  assert.ok(compareVersionDesc('1.0.0', '1.0.0-rc.5') < 0)
})

test('prerelease numbers compare numerically (rc.10 > rc.2)', () => {
  assert.ok(compareVersionDesc('1.0.0-rc.10', '1.0.0-rc.2') < 0)
})

test('tolerates a leading v and equal versions', () => {
  assert.equal(compareVersionDesc('v1.2.3', '1.2.3'), 0)
})
