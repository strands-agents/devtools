import { describe, it, expect } from 'vitest'
import { parseCommand } from '../src/runner'

describe('parseCommand', () => {
  it('strips the /strands-ts trigger', () => {
    expect(parseCommand('/strands-ts review please')).toBe('review')
  })
  it('accepts a bare command', () => {
    expect(parseCommand('review')).toBe('review')
  })
  it('returns empty for the lone trigger', () => {
    expect(parseCommand('/strands-ts')).toBe('')
  })
})
