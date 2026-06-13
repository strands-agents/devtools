import { describe, it, expect } from 'vitest'
import { formatReview, inlineBody, NO_ISSUES_TEMPLATE } from '../src/format'
import type { Finding } from '../src/findings'

describe('formatReview', () => {
  it('renders the no-issues template when empty (designed silence)', () => {
    expect(formatReview([], 'o/r', 'abc123')).toContain(NO_ISSUES_TEMPLATE)
  })
  it('renders findings with full-SHA permalinks', () => {
    const findings: Finding[] = [
      { lens: 'bug', description: 'off-by-one', file: 'a.ts', line: 10, reason: 'loop', score: 90 },
    ]
    const out = formatReview(findings, 'o/r', 'abc123def')
    expect(out).toContain('off-by-one')
    expect(out).toContain('https://github.com/o/r/blob/abc123def/a.ts#L')
  })
  it('inlineBody renders lens, description, reason, confidence', () => {
    const out = inlineBody({ lens: 'bug', description: 'off-by-one', file: 'a.ts', line: 10, reason: 'loop', score: 90 })
    expect(out).toContain('bug')
    expect(out).toContain('off-by-one')
    expect(out).toContain('90')
  })
})
