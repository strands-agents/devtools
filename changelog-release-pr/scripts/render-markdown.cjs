// Render a release into the changelog markdown file format consumed by the
// harness-sdk content collection (Plan 1 Zod schema). Hand-rolled minimal YAML
// emitter — entries are flat objects emitted as inline flow maps, mirroring the
// committed fixtures. mergePreserving keeps any human-written highlights/body
// on re-sync while refreshing the parsed entries/urls.

function q(s) {
  return `"${String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`
}

// Bareword if safe for YAML, else quoted. Anything starting with a digit or
// containing YAML-significant chars gets quoted.
function scalar(v) {
  if (v === null || v === undefined) return 'null'
  if (typeof v === 'number') return String(v)
  if (v === '') return '""'
  if (/^[\w.@/-]+$/.test(v) && !/^\d/.test(v)) return v
  return q(v)
}

function flowEntry(e) {
  const parts = [
    `type: ${e.type}`,
    `breaking: ${e.breaking === true}`,
    `scope: ${e.scope ? scalar(e.scope) : 'null'}`,
    `areas: [${e.areas.join(', ')}]`,
    `title: ${q(e.title)}`,
    `pr: ${e.pr == null ? 'null' : e.pr}`,
    `prUrl: ${e.prUrl ? q(e.prUrl) : 'null'}`,
    `commit: ${e.commit ? q(e.commit) : 'null'}`,
    `commitUrl: ${e.commitUrl ? q(e.commitUrl) : 'null'}`,
    `author: ${e.author ? scalar(e.author) : 'null'}`,
  ]
  return `  - { ${parts.join(', ')} }`
}

/**
 * @param {object} f  release file shape (see build-release-file.cjs)
 * @param {string} [body]  optional curated markdown body to append
 */
function renderMarkdown(f, body = '') {
  const lines = ['---']
  lines.push(`sdk: ${f.sdk}`)
  if (f.language) lines.push(`language: ${f.language}`)
  lines.push(`version: ${q(f.version)}`)
  lines.push(`tag: ${f.tag}`)
  lines.push(`date: ${f.date}`)
  lines.push(`releaseUrl: ${f.releaseUrl}`)
  lines.push(`packageUrl: ${f.packageUrl}`)
  if (f.compareUrl) lines.push(`compareUrl: ${f.compareUrl}`)
  if (f.highlights && f.highlights.trim()) {
    lines.push('highlights: |')
    for (const l of f.highlights.replace(/\s+$/, '').split('\n')) lines.push(`  ${l}`)
  }
  if (f.entries && f.entries.length) {
    lines.push('entries:')
    for (const e of f.entries) lines.push(flowEntry(e))
  } else {
    lines.push('entries: []')
  }
  lines.push('---')
  return lines.join('\n') + '\n' + (body ? '\n' + body.replace(/\s+$/, '') + '\n' : '')
}

// Pull the human-authored highlights block + markdown body out of an existing
// file so a re-sync doesn't clobber curation. Entries/urls always regenerate.
function mergePreserving(fresh, existing) {
  if (!existing) return renderMarkdown(fresh)
  const fm = existing.match(/^---\n([\s\S]*?)\n---\n?([\s\S]*)$/)
  const body = fm ? fm[2].trim() : ''
  let highlights = fresh.highlights
  if (fm) {
    // Capture a block scalar `highlights: |` up to the next top-level key or EOF.
    const hl = fm[1].match(/highlights:\s*\|\s*\n([\s\S]*?)(?=\n[A-Za-z][\w-]*:|$)/)
    if (hl) highlights = hl[1].replace(/^ {1,2}/gm, '').replace(/\s+$/, '')
  }
  return renderMarkdown({ ...fresh, highlights }, body)
}

module.exports = { renderMarkdown, mergePreserving }
