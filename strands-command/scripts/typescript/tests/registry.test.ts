import { describe, it, expect } from 'vitest'
import { resolveMode } from '../src/modes/registry'

describe('resolveMode', () => {
  it('resolves the reviewer mode for "review"', () => {
    expect(resolveMode('review')).toBeDefined()
  })
  it('returns undefined for an unknown command', () => {
    expect(resolveMode('frobnicate')).toBeUndefined()
  })
  it('returns undefined for an empty command', () => {
    expect(resolveMode('')).toBeUndefined()
  })
})
