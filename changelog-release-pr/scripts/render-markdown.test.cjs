const { test } = require('node:test')
const assert = require('node:assert/strict')
const { renderMarkdown, mergePreserving } = require('./render-markdown.cjs')

const file = {
  sdk: 'harness', language: 'python', version: '1.42.0', tag: 'python/v1.42.0',
  date: '2026-06-01',
  releaseUrl: 'https://github.com/strands-agents/harness-sdk/releases/tag/python%2Fv1.42.0',
  packageUrl: 'https://pypi.org/project/strands-agents/1.42.0/',
  entries: [
    {
      type: 'feat', breaking: false, scope: 'model', areas: ['model'], title: 'plumb cache tokens',
      pr: 2287, prUrl: 'https://github.com/strands-agents/sdk-python/pull/2287',
      commit: '155239d', commitUrl: 'https://github.com/strands-agents/sdk-python/commit/155239d', author: 'yatszhash',
    },
  ],
}

test('renders valid frontmatter markdown', () => {
  const md = renderMarkdown(file)
  assert.match(md, /^---\n/)
  assert.match(md, /\nsdk: harness\n/)
  assert.match(md, /\nlanguage: python\n/)
  assert.match(md, /\nversion: "1\.42\.0"\n/)
  assert.match(md, /\ntag: python\/v1\.42\.0\n/)
  assert.match(md, /type: feat/)
  assert.match(md, /areas: \[model\]/)
  assert.match(md, /title: "plumb cache tokens"/)
  assert.doesNotMatch(md, /highlights:/) // none provided
})

test('evals omits language key', () => {
  const md = renderMarkdown({
    ...file, sdk: 'evals', language: undefined, tag: 'v0.2.1', version: '0.2.1',
    releaseUrl: 'https://github.com/strands-agents/evals/releases/tag/v0.2.1',
    packageUrl: 'https://pypi.org/project/strands-agents-evals/0.2.1/',
  })
  assert.doesNotMatch(md, /\nlanguage:/)
})

test('empty entries renders entries: []', () => {
  assert.match(renderMarkdown({ ...file, entries: [] }), /\nentries: \[\]\n/)
})

test('quotes all-digit commit so YAML keeps it a string', () => {
  const md = renderMarkdown({
    ...file,
    entries: [{ ...file.entries[0], commit: '1122334', commitUrl: 'https://github.com/o/r/commit/1122334' }],
  })
  assert.match(md, /commit: "1122334"/)
})

test('null pr/commit/author render as null', () => {
  const md = renderMarkdown({
    ...file,
    entries: [{ type: 'feat', breaking: false, scope: null, areas: [], title: 'x', pr: null, prUrl: null, commit: null, commitUrl: null, author: null }],
  })
  assert.match(md, /pr: null/)
  assert.match(md, /commit: null/)
  assert.match(md, /author: null/)
  assert.match(md, /scope: null/)
})

test('quotes areas that contain YAML-significant chars', () => {
  const md = renderMarkdown({
    ...file,
    entries: [{ ...file.entries[0], areas: ['model', 'a,b', 'weird]bracket', 'has space'] }],
  })
  // commas/brackets/spaces inside a label must be quoted so the flow seq stays valid
  assert.match(md, /areas: \[model, "a,b", "weird\]bracket", "has space"\]/)
})

test('quotes YAML reserved bool/null words in scope and areas', () => {
  const md = renderMarkdown({
    ...file,
    entries: [{ ...file.entries[0], scope: 'on', areas: ['yes', 'null', 'model'] }],
  })
  assert.match(md, /scope: "on"/)
  assert.match(md, /areas: \["yes", "null", model\]/)
})

test('escapes quotes and newlines in titles', () => {
  const md = renderMarkdown({
    ...file,
    entries: [{ ...file.entries[0], title: 'add "quoted" thing' }],
  })
  assert.match(md, /title: "add \\"quoted\\" thing"/)
})

test('renders newContributors when present, omits when empty', () => {
  const md = renderMarkdown({ ...file, newContributors: [{ login: 'newdev', pr: 2700 }, { login: 'other-dev', pr: 2701 }] })
  assert.match(md, /newContributors:\n  - \{ login: newdev, pr: 2700 \}\n  - \{ login: other-dev, pr: 2701 \}/)
  assert.doesNotMatch(renderMarkdown(file), /newContributors/)
})

test('mergePreserving keeps existing highlights + body, refreshes entries', () => {
  const existing = `---
sdk: harness
language: python
version: "1.42.0"
tag: python/v1.42.0
date: 2026-06-01
releaseUrl: https://example/r
packageUrl: https://example/p
highlights: |
  Hand written summary.
entries: []
---

Some curated prose body.`
  const merged = mergePreserving(file, existing)
  assert.match(merged, /highlights: \|/)
  assert.match(merged, /Hand written summary\./)
  assert.match(merged, /Some curated prose body\./)
  assert.match(merged, /plumb cache tokens/) // entries refreshed from fresh file
})

test('mergePreserving with no existing file just renders fresh', () => {
  const merged = mergePreserving(file, null)
  assert.equal(merged, renderMarkdown(file))
})
