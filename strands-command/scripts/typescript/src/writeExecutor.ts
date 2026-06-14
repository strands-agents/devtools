// src/writeExecutor.ts
import { existsSync, readFileSync, realpathSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { addPrComment, GitHubApiError } from './tools/github.js'
import { ARTIFACT_PATH, type WriteOperation } from './tools/deferredWrite.js'

// function name -> write fn. Each fn is called as fn({write:true}, kwargs).
// This allowlist bounds what a (potentially agent-influenced) artifact can do.
type WriteFn = (mode: { write: true }, kwargs: Record<string, unknown>) => Promise<string>

const DEFAULT_WRITE_FNS: Record<string, WriteFn> = {
  addPrComment: (mode, kwargs) => addPrComment(mode, kwargs as any),
}

export interface ReplayResult { total: number; ok: number; failed: number; skipped: number }

export async function replayOperations(
  path: string = ARTIFACT_PATH,
  writeFns: Record<string, WriteFn> = DEFAULT_WRITE_FNS,
): Promise<ReplayResult> {
  if (!existsSync(path)) return { total: 0, ok: 0, failed: 0, skipped: 0 }
  const expectedRepo = process.env.GITHUB_REPOSITORY
  const lines = readFileSync(path, 'utf8').split('\n').map((l) => l.trim()).filter(Boolean)
  let ok = 0
  let failed = 0
  let skipped = 0
  for (const line of lines) {
    let op: WriteOperation
    try {
      op = JSON.parse(line) as WriteOperation
    } catch (e) {
      console.error(`Replay error: ${String(e)}`)
      failed++
      continue
    }
    const fn = writeFns[op.function]
    if (!fn) { console.error(`Unknown function: ${op.function}`); failed++; continue }
    // Repo guard: the artifact is produced while an agent runs; never let a
    // recorded op write outside the repo this workflow serves. Undefined
    // repo is pinned to the expected repo rather than trusted to fallbacks.
    const target = op.kwargs?.repo
    if (target !== undefined && target !== expectedRepo) {
      console.error(`Rejected op targeting foreign repo: ${String(target)}`)
      failed++
      continue
    }
    const kwargs = { ...op.kwargs, repo: expectedRepo }
    try {
      await fn({ write: true }, kwargs)
      ok++
    } catch (e) {
      // A 422 from GitHub on an inline comment means the anchored line isn't in
      // the diff hunk — an expected degradation, since the finding is still
      // preserved in the summary comment. Skip it rather than fail the run.
      if (e instanceof GitHubApiError && e.status === 422) {
        console.warn(`Skipped (422, line likely not in diff): ${op.function}`)
        skipped++
      } else {
        console.error(`Replay error: ${String(e)}`)
        failed++
      }
    }
  }
  return { total: lines.length, ok, failed, skipped }
}

async function main(): Promise<void> {
  const path = process.argv[2] ?? ARTIFACT_PATH
  const { total, ok, failed, skipped } = await replayOperations(path)
  console.log(`Replay complete: total=${total} ok=${ok} failed=${failed} skipped=${skipped}`)
  if (failed > 0) process.exitCode = 1
}

// Run as a script (finalize step) but not when imported by tests.
function isMain(): boolean {
  if (!process.argv[1]) return false
  try {
    return realpathSync(fileURLToPath(import.meta.url)) === realpathSync(process.argv[1])
  } catch {
    return false
  }
}

if (isMain()) {
  main().catch((e) => {
    console.error(String(e))
    process.exit(1)
  })
}
