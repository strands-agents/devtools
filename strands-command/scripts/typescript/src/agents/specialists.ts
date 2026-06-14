// src/agents/specialists.ts
import { Agent, tool } from '@strands-agents/sdk'
import { z } from 'zod'
import { LENSES } from '../findings.js'
import { loadSop } from '../prompts/sopLoader.js'
import { makeModel, resolveModelChoice, DEFAULT_TIER } from '../models.js'

const TIER_ENUM = z.enum(['haiku', 'sonnet', 'opus', 'fable'])

async function runSpecialist(systemPrompt: string, model: ReturnType<typeof makeModel>, prompt: string): Promise<string> {
  // Model + Agent constructed per call: the orchestrator may emit parallel tool
  // calls, and sharing instances across concurrent invocations is not
  // guaranteed safe by the SDK.
  const agent = new Agent({ model, printer: false, systemPrompt })
  const result = await agent.invoke(prompt)
  return result.lastMessage.content.map((b) => (b.type === 'textBlock' ? b.text : '')).join('')
}

export function buildSpecialistTools() {
  const lensTools = LENSES.map((lens) =>
    tool({
      name: `${lens}_reviewer`,
      description:
        `Review the PR through the ${lens} lens using its tuned SOP; returns a JSON array of ` +
        `findings. Optionally pass modelTier ("haiku" simple, "sonnet" mid, "opus" default / "fable" ` +
        `large or subtle) to match model strength to task complexity.`,
      inputSchema: z.object({
        prNumber: z.number().int(),
        context: z.string().describe('Diff and any extra context for this lens'),
        modelTier: TIER_ENUM.optional().describe('Model strength for this dispatch; omit for default'),
      }),
      callback: async (input) => {
        // Precedence: user config (STRANDS_TS_AGENTS) > orchestrator's modelTier > sonnet.
        const choice = resolveModelChoice(lens, input.modelTier, DEFAULT_TIER)
        const sop = loadSop(lens, `lenses/${lens}.sop.md`)
        return runSpecialist(sop, makeModel(choice), `PR #${input.prNumber}\n\n${input.context}`)
      },
    }),
  )

  // Meta-agent escape hatch: the orchestrator authors a focused prompt itself
  // when no tuned SOP covers the concern. SOPs remain the preferred path (the
  // orchestrator SOP says so); this tool is for genuinely uncovered cases.
  const customReviewer = tool({
    name: 'custom_reviewer',
    description:
      'Dispatch a custom one-off reviewer when NO tuned lens covers a concern. You write its ' +
      'system prompt and pick its model. Prefer the tuned *_reviewer tools whenever they apply.',
    inputSchema: z.object({
      systemPrompt: z.string().min(50)
        .describe('Focused reviewer system prompt; must demand the same JSON findings output contract'),
      prNumber: z.number().int(),
      context: z.string().describe('Diff and any extra context'),
      modelTier: TIER_ENUM.optional().describe('Model strength; omit for default'),
    }),
    callback: async (input) => {
      // "custom" is the user-config key, so humans can also pin its model.
      const choice = resolveModelChoice('custom', input.modelTier, DEFAULT_TIER)
      return runSpecialist(input.systemPrompt, makeModel(choice), `PR #${input.prNumber}\n\n${input.context}`)
    },
  })

  return [...lensTools, customReviewer]
}
