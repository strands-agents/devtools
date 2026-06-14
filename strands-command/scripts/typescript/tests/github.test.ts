// tests/github.test.ts
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { existsSync, rmSync, readFileSync } from 'node:fs'
import { getPrComments, addPrComment, getFileContentsRaw, getFileHistoryRaw, _http } from '../src/tools/github'
import { ARTIFACT_PATH } from '../src/tools/deferredWrite'

// _http is the indirection seam: { request } object whose property tests replace.

describe('github tools', () => {
  beforeEach(() => { if (existsSync(ARTIFACT_PATH)) rmSync(ARTIFACT_PATH) })
  afterEach(() => { vi.restoreAllMocks(); if (existsSync(ARTIFACT_PATH)) rmSync(ARTIFACT_PATH) })

  it('getPrComments calls the issue comments endpoint', async () => {
    const spy = vi.spyOn(_http, 'request').mockResolvedValue([{ id: 1, body: 'x' }])
    const out = await getPrComments(7, 'o/r')
    expect(spy).toHaveBeenCalledWith('GET', 'issues/7/comments?per_page=100&page=1', 'o/r', undefined)
    expect(out).toContain('x')
  })

  it('paginates past the first full page', async () => {
    const page1 = Array.from({ length: 100 }, (_, i) => ({ id: i }))
    const page2 = [{ id: 100 }]
    const spy = vi.spyOn(_http, 'request')
      .mockResolvedValueOnce(page1)
      .mockResolvedValueOnce(page2)
    const out = JSON.parse(await getPrComments(7, 'o/r'))
    expect(spy).toHaveBeenCalledTimes(2)
    expect(out).toHaveLength(101)
  })

  it('getFileContentsRaw decodes base64 content', async () => {
    vi.spyOn(_http, 'request').mockResolvedValue({
      content: Buffer.from('hello world', 'utf8').toString('base64'),
      encoding: 'base64',
    })
    const out = await getFileContentsRaw('a.ts', 'abc123', 'o/r')
    expect(out).toContain('hello world')
  })

  it('addPrComment defers when write disabled', async () => {
    const spy = vi.spyOn(_http, 'request').mockResolvedValue({ id: 1 })
    const res = await addPrComment({ write: false }, { prNumber: 7, body: 'hi', repo: 'o/r' })
    expect(spy).not.toHaveBeenCalled()
    expect(res).toMatch(/deferred/i)
    const line = JSON.parse(readFileSync(ARTIFACT_PATH, 'utf8').trim())
    expect(line.function).toBe('addPrComment')
    expect(line.kwargs.prNumber).toBe(7)
  })

  it('addPrComment posts when write enabled', async () => {
    const spy = vi.spyOn(_http, 'request').mockResolvedValue({ id: 99 })
    const res = await addPrComment({ write: true }, { prNumber: 7, body: 'hi', repo: 'o/r' })
    expect(spy).toHaveBeenCalledOnce()
    expect(res).toContain('99')
  })

  it('addPrComment inline range posts to pulls endpoint with start_line', async () => {
    const spy = vi.spyOn(_http, 'request').mockResolvedValue({ id: 5 })
    await addPrComment({ write: true }, {
      prNumber: 7, body: 'hi', path: 'a.ts', line: 10, startLine: 8, commitId: 'deadbeef', repo: 'o/r',
    })
    expect(spy).toHaveBeenCalledWith('POST', 'pulls/7/comments', 'o/r', {
      body: 'hi', commit_id: 'deadbeef', path: 'a.ts', line: 10, side: 'RIGHT', start_line: 8, start_side: 'RIGHT',
    })
  })

  it('addPrComment inline without commitId throws', async () => {
    const spy = vi.spyOn(_http, 'request').mockResolvedValue({ id: 1 })
    await expect(addPrComment({ write: true }, {
      prNumber: 7, body: 'hi', path: 'a.ts', line: 10, repo: 'o/r',
    })).rejects.toThrow(/commitId/)
    expect(spy).not.toHaveBeenCalled()
  })

  it('getFileContentsRaw rejects dot-segment traversal paths', async () => {
    const spy = vi.spyOn(_http, 'request').mockResolvedValue({})
    await expect(getFileContentsRaw('../../../../user', 'abc', 'o/r')).rejects.toThrow(/Invalid file path/)
    expect(spy).not.toHaveBeenCalled()
  })

  it('getFileContentsRaw accepts normal nested paths', async () => {
    vi.spyOn(_http, 'request').mockResolvedValue({
      content: Buffer.from('x', 'utf8').toString('base64'), encoding: 'base64',
    })
    await expect(getFileContentsRaw('src/tools/github.ts', 'abc', 'o/r')).resolves.toContain('x')
  })

  it('getFileHistoryRaw rejects dot-segment paths', async () => {
    const spy = vi.spyOn(_http, 'request').mockResolvedValue([])
    await expect(getFileHistoryRaw('..', 'o/r')).rejects.toThrow(/Invalid file path/)
    expect(spy).not.toHaveBeenCalled()
  })
})
