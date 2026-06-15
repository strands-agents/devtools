import { runReviewer, type ModeContext } from './reviewer.js'

export type ModeHandler = (ctx: ModeContext) => Promise<void>

const REGISTRY: Record<string, ModeHandler> = {
  reviewer: runReviewer,
}

// Map a /strands-ts <command> word to a mode handler.
const COMMAND_TO_MODE: Record<string, string> = {
  review: 'reviewer',
}

export function resolveMode(command: string): ModeHandler | undefined {
  const mode = COMMAND_TO_MODE[command.trim().toLowerCase()]
  return mode ? REGISTRY[mode] : undefined
}
