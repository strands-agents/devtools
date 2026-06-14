import { realpathSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { resolveMode } from './modes/registry.js'

export function parseCommand(raw: string): string {
  // Accept "/strands-ts review ..." or "review ..."; take the first word after the trigger.
  const cleaned = raw.replace(/^\/strands-ts\s*/i, '').trim()
  return cleaned.split(/\s+/)[0] ?? ''
}

async function main(): Promise<void> {
  const raw = process.env.INPUT_TASK ?? process.argv.slice(2).join(' ')
  const command = parseCommand(raw)
  const handler = resolveMode(command)
  if (!handler) throw new Error(`Unknown /strands-ts command: "${command}"`)

  const prNumber = Number(process.env.PR_NUMBER)
  const repo = process.env.GITHUB_REPOSITORY
  const headSha = process.env.PR_HEAD_SHA
  if (!prNumber || !repo || !headSha) {
    throw new Error('PR_NUMBER, GITHUB_REPOSITORY, and PR_HEAD_SHA must be set')
  }
  await handler({ prNumber, repo, headSha })
}

// Run as a script but not when imported.
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
