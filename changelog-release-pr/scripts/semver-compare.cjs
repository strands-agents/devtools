// Compare two version strings newest-first, with prerelease (rc) handling.
// Ported from the docs site's util/changelog.ts compareVersionDesc — kept as a
// small self-contained copy so this action stays dependency-free (the two
// packages don't share a module). Used to find the previous tag in a stream.
//
// Returns <0 if `a` is newer than `b` (so ascending .sort() puts newest first):
//   1.0.0 > 1.0.0-rc.1 > 1.0.0-rc.0, and 1.10.0 > 1.9.0 (numeric, not lexical).

function compareVersionDesc(a, b) {
  const parse = (v) => {
    const [core = '', pre] = String(v).replace(/^v/, '').split('-')
    return { core: core.split('.').map((n) => parseInt(n, 10) || 0), pre: pre ? pre.split('.') : null }
  }
  const pa = parse(a)
  const pb = parse(b)
  for (let i = 0; i < Math.max(pa.core.length, pb.core.length); i++) {
    const d = (pb.core[i] ?? 0) - (pa.core[i] ?? 0)
    if (d !== 0) return d
  }
  // Same core: a release (no prerelease) is newer than any prerelease of it.
  if (!pa.pre && pb.pre) return -1
  if (pa.pre && !pb.pre) return 1
  if (pa.pre && pb.pre) {
    for (let i = 0; i < Math.max(pa.pre.length, pb.pre.length); i++) {
      const x = pa.pre[i] ?? ''
      const y = pb.pre[i] ?? ''
      const nx = Number(x)
      const ny = Number(y)
      const d = Number.isNaN(nx) || Number.isNaN(ny) ? y.localeCompare(x) : ny - nx
      if (d !== 0) return d
    }
  }
  return 0
}

module.exports = { compareVersionDesc }
