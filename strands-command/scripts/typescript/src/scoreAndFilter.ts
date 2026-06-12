import type { Finding } from './findings.js'

export const THRESHOLD = 80
export const MAX_COMMENTS = 15

export function scoreAndFilter(findings: Finding[]): Finding[] {
  const passing = findings.filter((f) => f.score >= THRESHOLD)

  // Dedupe on file+line+description, keeping the highest score.
  const best = new Map<string, Finding>()
  for (const f of passing) {
    const key = `${f.file}:${f.line}:${f.description}`
    const existing = best.get(key)
    if (!existing || f.score > existing.score) best.set(key, f)
  }

  return [...best.values()]
    .sort((a, b) => b.score - a.score)
    .slice(0, MAX_COMMENTS)
}
