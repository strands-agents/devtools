---
name: brief
description: >
  Produce the team's daily morning brief — a short, scannable Markdown file plus a TL;DR: (1)
  Strands, what the team shipped, summarized at the FEATURE level, not a list of PRs/issues; (2)
  Ecosystem, external AI/industry news. TRIGGER on "morning brief", "daily brief", "daily digest",
  "team update", "what's new", "what happened (today|yesterday|this week)". SKIP for a one-off
  question about a single PR/issue/topic — just answer it.
allowed-tools: shell read write edit use_github subagent
---

# Morning brief

One scannable brief; every claim linked to a real tool result, nothing invented. The invoking prompt gives the lookback window and output path.

## Sources

Edit these bullets to change coverage, nothing else.

**Team repos** (Strands section; scope all GitHub queries to these):

- `strands-agents/harness-sdk`

**Preferred ecosystem domains** (Ecosystem section; prefer results and primary-source URLs from these):

- Hacker News (`news.ycombinator.com`)
- AWS news (`aboutamazon.com`)
- Anthropic News (`anthropic.com`)

## Procedure

1. Run one gatherer `subagent` per source in parallel (`instructions` = the content of `skills/brief/writer.md`, `model="fast"`); each returns link-bearing bullets or "nothing new". Scoped to one source? Run only that one.
2. Merge into the format below; an empty section gets one honest line. Write the TL;DR last (1-2 lines, nothing not already below).
3. `write` to the output path (a directory → `morning-brief-<today>.md`); print the TL;DR to the terminal.

### Gatherer tasks

```
# Strands
subagent(
  task="Gather the Strands section. repos=<the team repos from Sources>, window=<lookback>. Via use_github: PRs MERGED in the window + any RELEASE published in the window (check releases/tags). Summarize at the FEATURE level, grouping related work into a few bullets (e.g. 'Added Bedrock KB native citations'), NOT one bullet per PR. <=6 bullets, most-relevant link each (the release, or a representative PR). Lead with the release if one shipped; else 'nothing notable shipped in the window'.",
  instructions=<content of skills/brief/writer.md>,
  model="fast",
)

# Ecosystem
subagent(
  task="Gather the Ecosystem section. window=<lookback>. Use the `web` search tool with NATURAL-LANGUAGE queries (NOT the `site:` operator, which this index handles poorly; a plain topic query returns the primary source as a top hit). Cover: AI model releases / provider announcements; framework/library updates relevant to a Python+TypeScript agent SDK team; MCP / agent-tooling news; security advisories in our deps. Run at least one query per preferred domain from Sources; when a result is on a preferred domain, prefer it, and prefer the primary source over secondary coverage. Keep only in-window items. <=6 bullets, a link + one-line 'why it matters to us' each. If the `web` tool is absent (gated on STRANDLY_SEARCH_MCP_URL), say so in one line.",
  instructions=<content of skills/brief/writer.md>,
  model="fast",
)
```

## Format

```markdown
# Morning brief: <YYYY-MM-DD>

## TL;DR
- <1-2 line genuine summary of everything below>

## Strands
- <feature or bug fix the team shipped, with a release or representative PR link>

## Ecosystem
- **<headline>**: <why it matters to us> ([source](link))
```

Both sections always present; an empty one is a single honest line ("Nothing notable shipped in the window.", "Web search not configured; ecosystem section skipped.").

## Writing style (applies to the brief file)

- No hard-wrapping: each paragraph and bullet is one unbroken line.
- No em dashes or en dashes; use a period, comma, colon, or parentheses.
