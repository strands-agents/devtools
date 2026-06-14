// tests/agents.test.ts
import { describe, it, expect } from 'vitest'
import { buildSpecialistTools } from '../src/agents/specialists'
import { buildOrchestrator } from '../src/agents/orchestrator'
import { LENSES } from '../src/findings'

describe('specialist tools', () => {
  it('builds one tool per lens plus the custom_reviewer meta-agent', () => {
    const tools = buildSpecialistTools()
    expect(tools).toHaveLength(LENSES.length + 1)
    const names = tools.map((t) => t.name)
    for (const lens of LENSES) expect(names).toContain(`${lens}_reviewer`)
    expect(names).toContain('custom_reviewer')
  })
})

describe('orchestrator', () => {
  it('builds an Agent wired with specialists + read tools', () => {
    const agent = buildOrchestrator('o/r')
    const toolNames = agent.tools.map((t) => t.name)
    expect(toolNames).toContain('bug_reviewer')
    expect(toolNames).toContain('get_pr_diff')
  })
})
