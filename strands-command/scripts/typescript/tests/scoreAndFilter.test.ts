import { describe, it, expect } from 'vitest'
import { scoreAndFilter, THRESHOLD, MAX_COMMENTS } from '../src/scoreAndFilter'
import type { Finding } from '../src/findings'

function f(score: number, line = 1, file = 'a.py', description = 'd'): Finding {
  return { lens: 'bug', description, file, line, reason: 'r', score }
}

describe('scoreAndFilter', () => {
  it('drops findings below the threshold', () => {
    const kept = scoreAndFilter([f(79, 1), f(80, 2), f(100, 3)])
    expect(kept.map((x) => x.score)).toEqual([100, 80]) // sorted desc, 79 dropped
  })

  it('dedupes same file+line+description keeping the highest score', () => {
    const kept = scoreAndFilter([f(90), f(95)])
    expect(kept).toHaveLength(1)
    expect(kept[0].score).toBe(95)
  })

  it('caps the number of findings', () => {
    const many = Array.from({ length: 40 }, (_, i) => f(90, i))
    expect(scoreAndFilter(many)).toHaveLength(MAX_COMMENTS)
  })

  it('exposes a threshold of 80', () => {
    expect(THRESHOLD).toBe(80)
  })
})
