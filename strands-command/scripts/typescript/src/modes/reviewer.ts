import { buildOrchestrator } from '../agents/orchestrator.js'
import { scoreAndFilter } from '../scoreAndFilter.js'
import { formatReview, inlineBody } from '../format.js'
import { addPrComment } from '../tools/github.js'
import { writeEnabled } from '../tools/deferredWrite.js'
import { ReviewOutputSchema } from '../findings.js'

export interface ModeContext {
  prNumber: number
  repo: string
  headSha: string
}

export async function runReviewer(ctx: ModeContext): Promise<void> {
  const orchestrator = buildOrchestrator(ctx.repo)
  const result = await orchestrator.invoke(
    `Review pull request #${ctx.prNumber} in ${ctx.repo}. The PR head commit is ${ctx.headSha}; ` +
    `use it as the ref when fetching file contents.`,
  )
  const parsed = ReviewOutputSchema.safeParse(result.structuredOutput)
  if (!parsed.success) {
    // Designed silence means VERIFIED nothing to report. Malformed output is a
    // failure, not a clean review — fail loudly so the workflow run goes red
    // instead of posting a misleading "No issues found".
    throw new Error(`Reviewer structured output failed validation: ${parsed.error.message}`)
  }
  const kept = scoreAndFilter(parsed.data.findings)
  const mode = writeEnabled()
  // Inline comments can fail at replay time if a finding targets a line outside
  // the diff (GitHub 422). The summary comment independently lists every
  // finding, so a failed inline degrades gracefully rather than losing the
  // finding. (writeExecutor already continues past per-op failures and reports
  // counts.)
  for (const finding of kept) {
    await addPrComment(mode, {
      prNumber: ctx.prNumber,
      body: inlineBody(finding),
      path: finding.file,
      line: finding.line,
      startLine: finding.startLine,
      commitId: ctx.headSha,
      repo: ctx.repo,
    })
  }
  const body = formatReview(kept, ctx.repo, ctx.headSha)
  await addPrComment(mode, { prNumber: ctx.prNumber, body, repo: ctx.repo })
}
