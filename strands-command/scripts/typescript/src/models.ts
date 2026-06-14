// src/models.ts
import { BedrockModel } from '@strands-agents/sdk'

// Tier aliases — friendly names for the common cases.
export const MODEL_IDS = {
  haiku: 'global.anthropic.claude-haiku-4-5-20251001',
  sonnet: 'global.anthropic.claude-sonnet-4-6',
  opus: 'global.anthropic.claude-opus-4-8',
  fable: 'global.anthropic.claude-fable-5',
} as const

export type ModelTier = keyof typeof MODEL_IDS

// haiku gets a smaller budget; every other tier (and raw model ids) use the default.
const MAX_TOKENS = 16000
const HAIKU_MAX_TOKENS = 8000

// A model choice is either a tier alias ("haiku" | "sonnet" | "opus" | "fable")
// or a raw Bedrock model id (anything containing a dot).
export type ModelChoice = ModelTier | (string & {})

// Default tier for any agent when neither user config nor an agent-chosen tier
// applies. Opus by default: review quality is worth the cost, and per-dispatch
// downgrades (e.g. "haiku" for trivial changes) remain available.
export const DEFAULT_TIER: ModelTier = 'opus'

// Per-agent user config: STRANDS_TS_AGENTS env var — JSON map of
// agentKey -> { model?: ModelChoice, sop?: string (path relative to the SOP dir) }.
// Set by the workflow input; explicit human config always wins.
export interface AgentOverride {
  model?: string
  sop?: string
}

export function agentOverrides(): Record<string, AgentOverride> {
  const raw = process.env.STRANDS_TS_AGENTS
  if (!raw) return {}
  try {
    return JSON.parse(raw) as Record<string, AgentOverride>
  } catch {
    // Malformed config must not kill a review run.
    console.error('STRANDS_TS_AGENTS is not valid JSON; ignoring')
    return {}
  }
}

/**
 * Resolve which model a given agent (orchestrator or a specialist key like
 * "bug"/"adherence") should use. Precedence:
 *   1. User config (STRANDS_TS_AGENTS[key].model)
 *   2. Agent choice (the orchestrator may pick a tier per task complexity)
 *   3. Default tier for that agent.
 */
export function resolveModelChoice(
  agentKey: string,
  agentChoice: ModelChoice | undefined,
  defaultTier: ModelChoice,
): ModelChoice {
  const fromConfig = agentOverrides()[agentKey]?.model
  if (typeof fromConfig === 'string' && fromConfig.length > 0) return fromConfig
  return agentChoice ?? defaultTier
}

export function makeModel(choice: ModelChoice): BedrockModel {
  const isTier = Object.prototype.hasOwnProperty.call(MODEL_IDS, choice)
  if (!isTier && !choice.includes('.')) {
    throw new Error(`Unknown model tier or id: ${choice}`)
  }
  const modelId = isTier ? MODEL_IDS[choice as ModelTier] : choice
  const maxTokens = choice === 'haiku' ? HAIKU_MAX_TOKENS : MAX_TOKENS
  return new BedrockModel({
    modelId,
    maxTokens,
    region: process.env.AWS_REGION ?? 'us-west-2',
  })
}
