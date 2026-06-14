const { test } = require('node:test')
const assert = require('node:assert/strict')
const { tagToMeta, getPackageUrl } = require('./tag-meta.cjs')

test('harness python prefixed tag', () => {
  assert.deepEqual(tagToMeta('strands-agents/harness-sdk', 'python/v1.42.0'), {
    sdk: 'harness', language: 'python', version: '1.42.0',
  })
})

test('harness typescript prefixed tag', () => {
  assert.deepEqual(tagToMeta('strands-agents/harness-sdk', 'typescript/v1.4.0'), {
    sdk: 'harness', language: 'typescript', version: '1.4.0',
  })
})

test('harness bare v tag is pre-monorepo python', () => {
  assert.deepEqual(tagToMeta('strands-agents/harness-sdk', 'v1.9.1'), {
    sdk: 'harness', language: 'python', version: '1.9.1',
  })
})

test('evals bare v tag is python-only (no language)', () => {
  assert.deepEqual(tagToMeta('strands-agents/evals', 'v0.2.1'), {
    sdk: 'evals', language: undefined, version: '0.2.1',
  })
})

test('evals python-prefixed tag also maps to evals python', () => {
  assert.deepEqual(tagToMeta('strands-agents/evals', 'python/v0.1.3'), {
    sdk: 'evals', language: undefined, version: '0.1.3',
  })
})

test('malformed typescript tag still parses', () => {
  assert.deepEqual(tagToMeta('strands-agents/harness-sdk', 'typescript/v.1.2.0'), {
    sdk: 'harness', language: 'typescript', version: '1.2.0',
  })
})

test('python-wasm is skipped (null)', () => {
  assert.equal(tagToMeta('strands-agents/harness-sdk', 'python-wasm/v0.0.1'), null)
})

test('archived sdk-typescript repo bare v tags map to harness/typescript', () => {
  assert.deepEqual(tagToMeta('strands-agents/sdk-typescript', 'v1.3.0'), {
    sdk: 'harness', language: 'typescript', version: '1.3.0',
  })
  // rc tags parse too
  assert.deepEqual(tagToMeta('strands-agents/sdk-typescript', 'v1.0.0-rc.5'), {
    sdk: 'harness', language: 'typescript', version: '1.0.0-rc.5',
  })
})

test('package urls', () => {
  assert.equal(getPackageUrl('harness', 'python', '1.42.0'), 'https://pypi.org/project/strands-agents/1.42.0/')
  assert.equal(getPackageUrl('harness', 'typescript', '1.4.0'), 'https://www.npmjs.com/package/@strands-agents/sdk/v/1.4.0')
  assert.equal(getPackageUrl('evals', undefined, '0.2.1'), 'https://pypi.org/project/strands-agents-evals/0.2.1/')
})
