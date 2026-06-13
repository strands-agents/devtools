import type { Finding } from './findings.js'

export const NO_ISSUES_TEMPLATE =
  '### Code review\n\nNo issues found. Checked for bugs and guideline compliance.'

function permalink(repo: string, sha: string, file: string, line: number, startLine?: number): string {
  const lo = startLine ?? Math.max(1, line - 1)
  const hi = Math.max(line + 1, lo)
  return `https://github.com/${repo}/blob/${sha}/${file}#L${lo}-L${hi}`
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
