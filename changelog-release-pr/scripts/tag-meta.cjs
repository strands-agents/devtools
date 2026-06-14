// Map a repo + release tag to changelog metadata, and build package-registry URLs.
// Pure, dependency-free. Mirrors site/src/config/changelog.ts (Plan 1 contract).

function cleanVersion(raw) {
  // Strip a leading 'v' and any stray dot after it (handles 'v.1.2.0').
  return raw.replace(/^v\.?/, '')
}

/**
 * @param {string} repo  e.g. 'strands-agents/harness-sdk' | 'strands-agents/evals'
 * @param {string} tag   the release tag
 * @returns {{sdk:'harness'|'evals', language:'python'|'typescript'|undefined, version:string}|null}
 */
function tagToMeta(repo, tag) {
  const isEvals = repo.endsWith('/evals')
  if (isEvals) {
    // Evals is python-only; accept bare vX or python/vX.
    const m = tag.match(/(?:^|\/)v\.?(\d.*)$/)
    if (!m) return null
    return { sdk: 'evals', language: undefined, version: cleanVersion('v' + m[1]) }
  }
  // The archived pre-monorepo TypeScript repo: all its releases are
  // harness/typescript history (used only by the one-time backfill).
  if (repo.endsWith('/sdk-typescript')) {
    if (!/^v\.?\d/.test(tag)) return null
    return { sdk: 'harness', language: 'typescript', version: cleanVersion(tag) }
  }
  // harness-sdk
  if (tag.startsWith('python-wasm/')) return null
  if (tag.startsWith('python/')) {
    return { sdk: 'harness', language: 'python', version: cleanVersion(tag.slice('python/'.length)) }
  }
  if (tag.startsWith('typescript/')) {
    return { sdk: 'harness', language: 'typescript', version: cleanVersion(tag.slice('typescript/'.length)) }
  }
  if (/^v\.?\d/.test(tag)) {
    return { sdk: 'harness', language: 'python', version: cleanVersion(tag) }
  }
  return null
}

const pypi = (name, v) => `https://pypi.org/project/${name}/${v}/`
const npm = (name, v) => `https://www.npmjs.com/package/${name}/v/${v}`

/**
 * @param {'harness'|'evals'} sdk
 * @param {'python'|'typescript'|undefined} language
 * @param {string} version
 */
function getPackageUrl(sdk, language, version) {
  if (sdk === 'evals') return pypi('strands-agents-evals', version)
  if (language === 'typescript') return npm('@strands-agents/sdk', version)
  return pypi('strands-agents', version)
}

module.exports = { tagToMeta, getPackageUrl }
