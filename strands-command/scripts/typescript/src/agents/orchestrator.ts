// src/agents/orchestrator.ts
import { Agent } from '@strands-agents/sdk'
import { buildSpecialistTools } from './specialists.js'
import { readTools } from '../tools/github.js'
import { loadSop, scorerRubric } from '../prompts/sopLoader.js'
import { ReviewOutputSchema } from '../findings.js'
import { makeModel, resolveModelChoice } from '../models.js'

export function buildOrchestrator(repo: string): Agent {
  // "orchestrator" is the user-config key for this agent in STRANDS_TS_AGENTS;
  // the orchestrator itself has no agent-choice (nothing upstream picks for it).
  const choice = resolveModelChoice('orchestrator', undefined, 'sonnet')
  const sop = loadSop('orchestrator', 'reviewer.sop.md').replace('{{RUBRIC}}', scorerRubric())
  return new Agent({
    model: makeModel(choice),
    systemPrompt: sop,
    tools: [...buildSpecialistTools(), ...readTools(repo)],
    structuredOutputSchema: ReviewOutputSchema,
  })
}
