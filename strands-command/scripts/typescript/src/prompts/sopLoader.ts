// src/prompts/sopLoader.ts
import { readFileSync } from 'node:fs'
import { join, normalize, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { agentOverrides } from '../models.js'

const SOP_DIR = fileURLToPath(new URL('../../sops/', import.meta.url))

/** Load an agent's SOP: user-override path (relative to sops/, traversal-safe) or the default. */
export function loadSop(agentKey: string, defaultRelPath: string): string {
  const override = agentOverrides()[agentKey]?.sop
  const rel = override ?? defaultRelPath
  const base = normalize(SOP_DIR).replace(/\/+$/, '') + sep
  const full = normalize(join(SOP_DIR, rel))
  if (!full.startsWith(base)) {
    throw new Error(`SOP path escapes sops/ dir: ${rel}`)
  }
  return readFileSync(full, 'utf8')
}

export function scorerRubric(): string {
  // Findings scoring >= 80 are posted; < 80 are filtered. The bands are tuned so
  // a VERIFIED, doc-cited convention violation clears the bar (it is exactly the
  // kind of issue this reviewer exists to surface), while unverified or stylistic
  // findings not backed by a doc stay below it.
  return (
    '0: false positive, or a pre-existing issue not introduced by this change. ' +
    '25: possibly real but unverified, or a stylistic preference NOT backed by a cited doc. ' +
    '50: verified but a minor nitpick / rare in practice / low impact. ' +
    '80: verified and impactful, OR a violation of an explicitly cited convention/governance doc, ' +
    'OR a verified breaking change. These MUST be surfaced. ' +
    '100: certain, frequent, and directly evidence-confirmed.'
  )
}
