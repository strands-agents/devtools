import { describe, it, expect, beforeEach } from 'vitest'
import { makeModel, resolveModelChoice } from '../src/models'

describe('makeModel', () => {
  it('throws on an unknown tier that is not a model id', () => {
    expect(() => makeModel('gpt')).toThrow()
  })
  it('accepts a raw Bedrock model id passthrough', () => {
    expect(() => makeModel('global.anthropic.claude-opus-4-8')).not.toThrow()
  })
  it('rejects prototype-chain keys as tiers', () => {
    expect(() => makeModel('constructor')).toThrow()
  })
})

describe('resolveModelChoice (precedence: user config > agent choice > default)', () => {
  beforeEach(() => { delete process.env.STRANDS_TS_AGENTS })

  it('falls back to the default tier', () => {
    expect(resolveModelChoice('bug', undefined, 'sonnet')).toBe('sonnet')
  })

  it('agent choice overrides the default', () => {
    expect(resolveModelChoice('bug', 'haiku', 'sonnet')).toBe('haiku')
  })

  it('user config (STRANDS_TS_AGENTS JSON) overrides agent choice', () => {
    process.env.STRANDS_TS_AGENTS = '{"bug":{"model":"global.anthropic.claude-opus-4-8"}}'
    expect(resolveModelChoice('bug', 'haiku', 'sonnet')).toBe('global.anthropic.claude-opus-4-8')
  })

  it('user config for other keys does not affect this one', () => {
    process.env.STRANDS_TS_AGENTS = '{"adherence":{"model":"haiku"}}'
    expect(resolveModelChoice('bug', undefined, 'sonnet')).toBe('sonnet')
  })

  it('malformed config JSON is ignored, not fatal', () => {
    process.env.STRANDS_TS_AGENTS = 'not json'
    expect(resolveModelChoice('bug', 'haiku', 'sonnet')).toBe('haiku')
  })
})
