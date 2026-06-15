// tests/writeExecutor.test.ts
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { writeFileSync, rmSync, existsSync, mkdirSync } from 'node:fs'
import { replayOperations } from '../src/writeExecutor'
import { GitHubApiError } from '../src/tools/github'

const TMP = '.artifact/test_ops.jsonl'

describe('replayOperations', () => {
  beforeEach(() => { process.env.GITHUB_REPOSITORY = 'o/r'; mkdirSync('.artifact', { recursive: true }) })
  afterEach(() => { vi.restoreAllMocks(); if (existsSync(TMP)) rmSync(TMP) })

  it('replays each line via the matching write fn in write mode', async () => {
    const fake = vi.fn().mockResolvedValue('ok')
    writeFileSync(TMP, [
      JSON.stringify({ timestamp: 't', function: 'addPrComment', kwargs: { prNumber: 1, body: 'a', repo: 'o/r' } }),
      JSON.stringify({ timestamp: 't', function: 'addPrComment', kwargs: { prNumber: 1, body: 'b', repo: 'o/r' } }),
    ].join('\n') + '\n')

    const { total, ok, failed } = await replayOperations(TMP, { addPrComment: fake })
    expect(total).toBe(2)
    expect(ok).toBe(2)
    expect(failed).toBe(0)
    expect(fake).toHaveBeenCalledTimes(2)
    expect(fake.mock.calls[0][0]).toEqual({ write: true }) // forced write mode
    expect(fake.mock.calls[0][1]).toEqual({ prNumber: 1, body: 'a', repo: 'o/r' })
  })

  it('pins undefined kwargs.repo to the expected repo', async () => {
    const fake = vi.fn().mockResolvedValue('ok')
    writeFileSync(TMP, JSON.stringify(
      { timestamp: 't', function: 'addPrComment', kwargs: { prNumber: 1, body: 'a' } },
    ) + '\n')
    const { ok } = await replayOperations(TMP, { addPrComment: fake })
    expect(ok).toBe(1)
    expect(fake.mock.calls[0][1].repo).toBe('o/r')
  })

  it('rejects an operation targeting a different repo', async () => {
    const fake = vi.fn().mockResolvedValue('ok')
    writeFileSync(TMP, JSON.stringify(
      { timestamp: 't', function: 'addPrComment', kwargs: { prNumber: 1, body: 'a', repo: 'evil/elsewhere' } },
    ) + '\n')
    const { total, ok, failed } = await replayOperations(TMP, { addPrComment: fake })
    expect(total).toBe(1)
    expect(ok).toBe(0)
    expect(failed).toBe(1)
    expect(fake).not.toHaveBeenCalled()
  })

  it('skips unknown function names without throwing', async () => {
    writeFileSync(TMP, JSON.stringify({ timestamp: 't', function: 'nope', kwargs: {} }) + '\n')
    const { total, ok, failed } = await replayOperations(TMP, {})
    expect(total).toBe(1)
    expect(ok).toBe(0)
    expect(failed).toBe(1)
  })

  it('returns zero counts when the file is missing', async () => {
    const { total } = await replayOperations('.artifact/does_not_exist.jsonl', {})
    expect(total).toBe(0)
  })

  it('counts a 422 GitHubApiError as skipped, not failed, without throwing', async () => {
    const fake = vi.fn().mockRejectedValue(new GitHubApiError(422, 'POST', 'pulls/3/comments'))
    writeFileSync(TMP, JSON.stringify(
      { timestamp: 't', function: 'addPrComment', kwargs: { prNumber: 3, body: 'a', repo: 'o/r' } },
    ) + '\n')
    const { ok, failed, skipped } = await replayOperations(TMP, { addPrComment: fake })
    expect(ok).toBe(0)
    expect(failed).toBe(0)
    expect(skipped).toBe(1)
    expect(fake).toHaveBeenCalledOnce()
  })

  it('counts a non-422 GitHubApiError as failed', async () => {
    const fake = vi.fn().mockRejectedValue(new GitHubApiError(500, 'POST', 'pulls/3/comments'))
    writeFileSync(TMP, JSON.stringify(
      { timestamp: 't', function: 'addPrComment', kwargs: { prNumber: 3, body: 'a', repo: 'o/r' } },
    ) + '\n')
    const { ok, failed, skipped } = await replayOperations(TMP, { addPrComment: fake })
    expect(ok).toBe(0)
    expect(failed).toBe(1)
    expect(skipped).toBe(0)
  })
})
