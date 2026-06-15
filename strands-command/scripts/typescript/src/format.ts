import type { Finding } from './findings.js'

export const NO_ISSUES_TEMPLATE =
  '### Code review\n\nNo issues found. Checked for bugs and guideline compliance.'

function permalink(repo: string, sha: string, file: string, line: number, startLine?: number): string {
  const lo = startLine ?? Math.max(1, line - 1)
  const hi = Math.max(line + 1, lo)
  return `https://github.com/${repo}/blob/${sha}/${file}#L${lo}-L${hi}`
}

// Clamp an inline-comment anchor: a finding's `line` is the changed line; only
// keep a multi-line range when it's a small, forward span (start < line, within
// 10 lines), otherwise anchor to the single line so GitHub doesn't 422 on a
// range that reaches over unchanged context.
export function inlineAnchor(line: number, startLine?: number): { line: number; startLine?: number } {
  if (startLine === undefined || startLine >= line || line - startLine > 10) {
    return { line }
  }
  return { line, startLine }
}

export function inlineBody(f: Finding): string {
  return `**${f.lens}**: ${f.description}\n\n${f.reason} (confidence: ${f.score})`
}

export function formatReview(findings: Finding[], repo: string, sha: string): string {
  if (findings.length === 0) return NO_ISSUES_TEMPLATE
  const lines = findings.map((f, i) =>
    `${i + 1}. ${f.description} (${f.reason})\n\n${permalink(repo, sha, f.file, f.line, f.startLine)}`,
  )
  return `### Code review\n\nFound ${findings.length} issue(s):\n\n${lines.join('\n\n')}`
}
