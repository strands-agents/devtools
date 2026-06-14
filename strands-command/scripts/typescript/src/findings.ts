import { z } from 'zod'

export const LENSES = ['adherence', 'api', 'bug', 'history', 'test'] as const

export const FindingSchema = z.object({
  // Deliberately a free string, not z.enum(LENSES): the custom_reviewer
  // meta-agent emits findings under ad-hoc lens names.
  lens: z.string(),
  description: z.string(),
  file: z.string(),
  line: z.number().int(),
  startLine: z.number().int().optional(),
  reason: z.string(),
  score: z.number().int().min(0).max(100),
}).refine((f) => f.startLine === undefined || f.startLine <= f.line, {
  message: 'startLine must be <= line',
})

export type Finding = z.infer<typeof FindingSchema>

// The orchestrator emits this as structuredOutput.
export const ReviewOutputSchema = z.object({
  findings: z.array(FindingSchema),
})
