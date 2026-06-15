import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { readFileSync, rmSync, existsSync } from 'node:fs'
import { recordOrCall, ARTIFACT_PATH } from '../src/tools/deferredWrite'

describe('recordOrCall', () => {
  beforeEach(() => { if (existsSync(ARTIFACT_PATH)) rmSync(ARTIFACT_PATH) })
  afterEach(() => { if (existsSync(ARTIFACT_PATH)) rmSync(ARTIFACT_PATH) })

  it('defers (records, does not call) when write disabled', async () => {
    let called = false
    const result = await recordOrCall(
      { write: false }, 'addPrComment', { prNumber: 1, body: 'hi' },
      async () => { called = true; return 'posted' },
    )
    expect(called).toBe(false)
    expect(result).toMatch(/deferred/i)
    const line = JSON.parse(readFileSync(ARTIFACT_PATH, 'utf8').trim())
    expect(line.function).toBe('addPrComment')
    expect(line.kwargs).toEqual({ prNumber: 1, body: 'hi' })
    expect(typeof line.timestamp).toBe('string')
  })

  it('calls through when write enabled', async () => {
    let called = false
    const result = await recordOrCall(
      { write: true }, 'addPrComment', { prNumber: 1, body: 'hi' },
      async () => { called = true; return 'posted' },
    )
    expect(called).toBe(true)
    expect(result).toBe('posted')
    expect(existsSync(ARTIFACT_PATH)).toBe(false)
  })

  it('propagates call errors in write mode', async () => {
    await expect(recordOrCall(
      { write: true }, 'addPrComment', {}, async () => { throw new Error('boom') },
    )).rejects.toThrow('boom')
  })

  it('appends one JSONL line per deferred call', async () => {
    await recordOrCall({ write: false }, 'a', { n: 1 }, async () => 'x')
    await recordOrCall({ write: false }, 'b', { n: 2 }, async () => 'y')
    const lines = readFileSync(ARTIFACT_PATH, 'utf8').trim().split('\n')
    expect(lines).toHaveLength(2)
    expect(lines.map((l) => JSON.parse(l).function)).toEqual(['a', 'b'])
  })
})
