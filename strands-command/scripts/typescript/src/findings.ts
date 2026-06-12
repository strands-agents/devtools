import { z } from 'zod'

export const LENSES = ['adherence', 'api', 'bug', 'history', 'test'] as const

export const FindingSchema = z.object({
  lens: z.string(),
  description: z.string(),
  file: z.string(),
  line: z.number().int(),
  startLine: z.number().int().optional(),
  reason: z.string(),
  score: z.number().int().min(0).max(100),
})

export type Finding = z.infer<typeof FindingSchema>

// The orchestrator emits this as structuredOutput.
export const ReviewOutputSchema = z.object({
  findings: z.array(FindingSchema),
})

export type ReviewOutput = z.infer<typeof ReviewOutputSchema>
