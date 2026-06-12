import { describe, it, expect } from 'vitest'
import { FindingSchema, ReviewOutputSchema } from '../src/findings'

describe('FindingSchema', () => {
  it('accepts a complete finding', () => {
    const f = FindingSchema.parse({
      lens: 'bug', description: 'off-by-one', file: 'a.py',
      line: 10, startLine: 8, reason: 'loop bound', score: 90,
    })
    expect(f.startLine).toBe(8)
  })

  it('defaults optional startLine to undefined', () => {
    const f = FindingSchema.parse({
      lens: 'api', description: 'd', file: 'x.ts', line: 1, reason: 'r', score: 50,
    })
    expect(f.startLine).toBeUndefined()
  })

  it('rejects an out-of-range score', () => {
    expect(() => FindingSchema.parse({
      lens: 'bug', description: 'd', file: 'x', line: 1, reason: 'r', score: 150,
    })).toThrow()
  })

  it('parses a review output wrapping a findings array', () => {
    const out = ReviewOutputSchema.parse({ findings: [] })
    expect(out.findings).toEqual([])
  })
})
