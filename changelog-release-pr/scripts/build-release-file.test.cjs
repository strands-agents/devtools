const { test } = require('node:test')
const assert = require('node:assert/strict')
const { buildReleaseFile } = require('./build-release-file.cjs')

const release = {
  tag_name: 'python/v1.42.0',
  published_at: '2026-06-01T18:18:57Z',
  html_url: 'https://github.com/strands-agents/harness-sdk/releases/tag/python%2Fv1.42.0',
  body: "## What's Changed\n* feat(model): plumb cache tokens by @yatszhash in https://github.com/strands-agents/sdk-python/pull/2287\n",
}

test('produces correct path + parsed/enriched contents', async () => {
  const result = await buildReleaseFile('strands-agents/harness-sdk', release, {
    enrich: async () => ({ areas: ['model'], breaking: false, commit: '155239d', author: 'yatszhash' }),
    readExisting: async () => null,
  })
  assert.equal(result.path, 'site/src/content/changelog/harness/python-v1.42.0.md')
  assert.match(result.contents, /sdk: harness/)
  assert.match(result.contents, /title: "plumb cache tokens"/)
  assert.match(result.contents, /areas: \[model\]/)
  // prUrl/commitUrl use the PR's own repo (sdk-python), not harness-sdk
  assert.match(result.contents, /sdk-python\/pull\/2287/)
  assert.match(result.contents, /sdk-python\/commit\/155239d/)
  assert.equal(result.warning, undefined)
})

test('evals file path + no language', async () => {
  const evalsRelease = {
    tag_name: 'v0.2.1', published_at: '2026-05-29T00:00:00Z',
    html_url: 'https://github.com/strands-agents/evals/releases/tag/v0.2.1',
    body: '* feat: add chaos testing by @x in https://github.com/strands-agents/evals/pull/224\n',
  }
  const r = await buildReleaseFile('strands-agents/evals', evalsRelease, {
    enrich: async () => ({ areas: [], breaking: false, commit: 'aaa1111', author: 'x' }),
    readExisting: async () => null,
  })
  assert.equal(r.path, 'site/src/content/changelog/evals/v0.2.1.md')
  assert.doesNotMatch(r.contents, /\nlanguage:/)
})

test('skips out-of-scope tags', async () => {
  const r = await buildReleaseFile('strands-agents/harness-sdk',
    { ...release, tag_name: 'python-wasm/v0.0.1' },
    { enrich: async () => ({ areas: [], breaking: false, commit: null, author: null }), readExisting: async () => null })
  assert.equal(r, null)
})

test('flags format-drift warning when bullets parse poorly', async () => {
  const drifted = { ...release, body: "## What's Changed\n* updated thing #11\n* fixed thing #12\n* added thing #13\n" }
  const r = await buildReleaseFile('strands-agents/harness-sdk', drifted, {
    enrich: async () => ({ areas: [], breaking: false, commit: null, author: null }),
    readExisting: async () => null,
  })
  assert.ok(r)
  assert.match(r.warning, /parsed 0 of 3/)
})

test('monorepo release filters entries by stream language from PR files', async () => {
  const body = [
    '* feat: py thing by @a in https://github.com/strands-agents/harness-sdk/pull/1',
    '* feat: ts thing by @b in https://github.com/strands-agents/harness-sdk/pull/2',
    '* feat: both thing by @c in https://github.com/strands-agents/harness-sdk/pull/3',
    '* chore: site thing by @d in https://github.com/strands-agents/harness-sdk/pull/4',
    '* fix: unknown thing by @e in https://github.com/strands-agents/harness-sdk/pull/5',
  ].join('\n')
  const langByPr = { 1: ['python'], 2: ['typescript'], 3: ['python', 'typescript'], 4: [], 5: null }
  const deps = {
    enrich: async (_repo, pr) => ({ areas: [], breaking: false, commit: null, author: null, languages: langByPr[pr] }),
    readExisting: async () => null,
  }
  const py = await buildReleaseFile('strands-agents/harness-sdk',
    { tag_name: 'python/v1.43.0', published_at: '2026-06-12T00:00:00Z', html_url: 'h', body }, deps)
  // python stream: keeps py(1), both(3), unknown(5); drops ts(2) and site-only(4)
  assert.match(py.contents, /py thing/)
  assert.match(py.contents, /both thing/)
  assert.match(py.contents, /unknown thing/)
  assert.doesNotMatch(py.contents, /ts thing/)
  assert.doesNotMatch(py.contents, /site thing/)

  const ts = await buildReleaseFile('strands-agents/harness-sdk',
    { tag_name: 'typescript/v1.5.0', published_at: '2026-06-12T00:00:00Z', html_url: 'h', body }, deps)
  // typescript stream: keeps ts(2), both(3), unknown(5)
  assert.match(ts.contents, /ts thing/)
  assert.match(ts.contents, /both thing/)
  assert.match(ts.contents, /unknown thing/)
  assert.doesNotMatch(ts.contents, /py thing/)
  assert.doesNotMatch(ts.contents, /site thing/)
})

test('pre-monorepo and evals releases are not language-filtered', async () => {
  const deps = {
    // even if files say typescript, a single-language-repo release keeps everything
    enrich: async () => ({ areas: [], breaking: false, commit: null, author: null, languages: ['typescript'] }),
    readExisting: async () => null,
  }
  const old = await buildReleaseFile('strands-agents/harness-sdk',
    { tag_name: 'v1.9.1', published_at: '2026-01-01T00:00:00Z', html_url: 'h',
      body: '* feat: old thing by @a in https://github.com/strands-agents/sdk-python/pull/9' }, deps)
  assert.match(old.contents, /old thing/)
  const ev = await buildReleaseFile('strands-agents/evals',
    { tag_name: 'v0.2.1', published_at: '2026-01-01T00:00:00Z', html_url: 'h',
      body: '* feat: eval thing by @a in https://github.com/strands-agents/evals/pull/9' }, deps)
  assert.match(ev.contents, /eval thing/)
})

test('new contributors are language-gated, but docs/ci-only ones appear in both streams', async () => {
  const body = [
    '* feat: x by @a in https://github.com/strands-agents/harness-sdk/pull/1',
    '',
    '## New Contributors',
    '* @pydev made their first contribution in https://github.com/strands-agents/harness-sdk/pull/10',
    '* @tsdev made their first contribution in https://github.com/strands-agents/harness-sdk/pull/11',
    '* @docsdev made their first contribution in https://github.com/strands-agents/harness-sdk/pull/12',
    '* @mystery made their first contribution in https://github.com/strands-agents/harness-sdk/pull/13',
  ].join('\n')
  const langByPr = { 1: ['python'], 10: ['python'], 11: ['typescript'], 12: [], 13: null }
  const deps = {
    enrich: async (_r, pr) => ({ areas: [], breaking: false, commit: null, author: null, languages: langByPr[pr] }),
    readExisting: async () => null,
  }
  const py = await buildReleaseFile('strands-agents/harness-sdk',
    { tag_name: 'python/v1.43.0', published_at: '2026-06-12T00:00:00Z', html_url: 'h', body }, deps)
  // python stream: pydev (python), docsdev (neither → both), mystery (unknown → both); NOT tsdev
  assert.match(py.contents, /login: pydev/)
  assert.match(py.contents, /login: docsdev/)
  assert.match(py.contents, /login: mystery/)
  assert.doesNotMatch(py.contents, /login: tsdev/)

  const ts = await buildReleaseFile('strands-agents/harness-sdk',
    { tag_name: 'typescript/v1.5.0', published_at: '2026-06-12T00:00:00Z', html_url: 'h', body }, deps)
  assert.match(ts.contents, /login: tsdev/)
  assert.match(ts.contents, /login: docsdev/)
  assert.match(ts.contents, /login: mystery/)
  assert.doesNotMatch(ts.contents, /login: pydev/)
})

test('new contributors flow into frontmatter, not entries', async () => {
  const body = [
    '* feat: real thing by @a in https://github.com/strands-agents/harness-sdk/pull/1',
    '',
    '## New Contributors',
    '* @newdev made their first contribution in https://github.com/strands-agents/harness-sdk/pull/2700',
  ].join('\n')
  const r = await buildReleaseFile('strands-agents/harness-sdk',
    { tag_name: 'python/v1.43.0', published_at: '2026-06-12T00:00:00Z', html_url: 'h', body },
    { enrich: async () => ({ areas: [], breaking: false, commit: null, author: null, languages: ['python'] }), readExisting: async () => null })
  assert.match(r.contents, /newContributors:\n  - \{ login: newdev, pr: 2700 \}/)
  assert.doesNotMatch(r.contents, /first contribution/) // not an entry
})

test('breaking marker promotes type when no conventional type', async () => {
  // a non-conventional line that the PR labels mark breaking → type becomes 'breaking'
  const r = await buildReleaseFile('strands-agents/harness-sdk',
    { ...release, body: '* drop the old api by @x in https://github.com/strands-agents/harness-sdk/pull/1\n' },
    { enrich: async () => ({ areas: [], breaking: true, commit: 'bbb2222', author: 'x' }), readExisting: async () => null })
  assert.match(r.contents, /type: breaking/)
  assert.match(r.contents, /breaking: true/)
})
