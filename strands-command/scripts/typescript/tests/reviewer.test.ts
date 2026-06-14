import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { existsSync, rmSync, readFileSync } from 'node:fs'
import { ARTIFACT_PATH } from '../src/tools/deferredWrite'

vi.mock('../src/agents/orchestrator', () => ({
  buildOrchestrator: vi.fn(),
}))

import { buildOrchestrator } from '../src/agents/orchestrator'
import { runReviewer } from '../src/modes/reviewer'

const ctx = { prNumber: 7, repo: 'o/r', headSha: 'abc123' }

function mockAgent(structuredOutput: unknown) {
  return { invoke: vi.fn().mockResolvedValue({ structuredOutput }) }
}

describe('runReviewer', () => {
  beforeEach(() => {
    process.env.GITHUB_WRITE = 'false'
    if (existsSync(ARTIFACT_PATH)) rmSync(ARTIFACT_PATH)
  })
  afterEach(() => {
    vi.restoreAllMocks()
    delete process.env.GITHUB_WRITE
    if (existsSync(ARTIFACT_PATH)) rmSync(ARTIFACT_PATH)
  })

  it('defers a formatted comment for valid findings above threshold', async () => {
    vi.mocked(buildOrchestrator).mockReturnValue(mockAgent({
      findings: [{ lens: 'bug', description: 'real bug', file: 'a.ts', line: 3, reason: 'r', score: 95 }],
    }) as any)
    await runReviewer(ctx)
    const lines = readFileSync(ARTIFACT_PATH, 'utf8').trim().split('\n').map((l) => JSON.parse(l))
    expect(lines).toHaveLength(2)
    // First line: the inline comment anchored to the finding's location.
    expect(lines[0].function).toBe('addPrComment')
    expect(lines[0].kwargs.path).toBe('a.ts')
    expect(lines[0].kwargs.line).toBe(3)
    expect(lines[0].kwargs.commitId).toBe('abc123')
    expect(lines[0].kwargs.body).toContain('real bug')
    // Last line: the summary comment (no path).
    const summary = lines[lines.length - 1]
    expect(summary.function).toBe('addPrComment')
    expect(summary.kwargs.path).toBeUndefined()
    expect(summary.kwargs.body).toContain('real bug')
    expect(summary.kwargs.body).toContain('abc123')
  })

  it('passes startLine through to the inline comment', async () => {
    vi.mocked(buildOrchestrator).mockReturnValue(mockAgent({
      findings: [{ lens: 'bug', description: 'range bug', file: 'b.ts', line: 9, startLine: 7, reason: 'r', score: 88 }],
    }) as any)
    await runReviewer(ctx)
    const lines = readFileSync(ARTIFACT_PATH, 'utf8').trim().split('\n').map((l) => JSON.parse(l))
    expect(lines[0].kwargs.startLine).toBe(7)
    expect(lines[0].kwargs.commitId).toBe('abc123')
  })

  it('defers the designed-silence template when all findings are filtered out', async () => {
    vi.mocked(buildOrchestrator).mockReturnValue(mockAgent({
      findings: [{ lens: 'bug', description: 'weak', file: 'a.ts', line: 3, reason: 'r', score: 40 }],
    }) as any)
    await runReviewer(ctx)
    const lines = readFileSync(ARTIFACT_PATH, 'utf8').trim().split('\n').map((l) => JSON.parse(l))
    expect(lines).toHaveLength(1)
    expect(lines[0].kwargs.body).toContain('No issues found')
  })

  it('throws on malformed structured output without deferring anything', async () => {
    vi.mocked(buildOrchestrator).mockReturnValue(mockAgent({ nonsense: true }) as any)
    await expect(runReviewer(ctx)).rejects.toThrow(/structured output/)
    expect(existsSync(ARTIFACT_PATH)).toBe(false)
  })
})
