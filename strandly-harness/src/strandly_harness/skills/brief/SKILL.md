---
name: brief
description: >
  Produce the team's daily morning brief: a short, scannable summary of what changed in the last day. Two sections: (1) Strands, a high-level summary of features and bug fixes the team shipped (read from harness-sdk merged PRs and any release that day, summarized at the feature level, NOT a list of individual PRs/issues); and (2) Ecosystem, external AI/industry news (model releases, framework updates, security advisories). TRIGGER on "morning brief", "daily brief", "daily digest", "team update", "what's new", "what happened (today|yesterday|this week)". SKIP for a specific one-off question about a single PR/issue/topic (just answer it directly). Produces one Markdown file plus a short TL;DR streamed to the terminal.
allowed-tools: bash file_editor use_github think spawn
---

# Morning brief

Consolidate a day of change into one scannable brief the whole team reads. Value = consolidation + honesty: one artifact, every claim linked to a real tool result, nothing invented.

## Inputs

The invoking prompt gives the lookback window (e.g. "the last 24h") and the output path. Today's date comes from your environment context.

## Sources

The two curated lists the gatherers use. To change what the brief covers, edit these bullets, nothing else.

**Team repos** (Strands section; scope all GitHub queries to these):

- `strands-agents/harness-sdk`

**Preferred ecosystem domains** (Ecosystem section; prefer results and primary-source URLs from these):

- Hacker News (`news.ycombinator.com`)
- AWS news (`aboutamazon.com`)
- Anthropic News (`anthropic.com`)

## Procedure

1. Gather the two sources in parallel: `spawn` one gatherer per source (system_prompt `skills/brief/writer.md`, which holds the gatherer contract), each returning link-bearing bullets or "nothing new". Scoped to one source? Spawn only that one.
2. Merge into the format below. One line per bullet, links inline; an empty section gets one honest line, never padding.
3. Write the TL;DR LAST: a genuine 1-2 line summary of the sections, no claim not already below.
4. `file_editor`-write the Markdown to the output path (if it is a directory, name the file `morning-brief-<today's date>.md`; `bash`-create parent dirs as needed). Follow the writing style below.
5. Print the TL;DR to the terminal so a CLI caller sees the gist without opening the file.

### Gatherer tasks

```
# Strands
spawn(
  prompt="Gather the Strands section. repos=<the team repos from Sources>, window=<lookback>. Via use_github: PRs MERGED in the window + any RELEASE published in the window (check releases/tags). Summarize at the FEATURE level, grouping related work into a few bullets (e.g. 'Added Bedrock KB native citations'), NOT one bullet per PR. <=6 bullets, most-relevant link each (the release, or a representative PR). Lead with the release if one shipped; else 'nothing notable shipped in the window'.",
  system_prompt="skills/brief/writer.md",
  model="fast",  # gathering + summarizing is mechanical legwork — cheap tier
)

# Ecosystem
spawn(
  prompt="Gather the Ecosystem section. window=<lookback>. Use the `web` search tool with NATURAL-LANGUAGE queries (NOT the `site:` operator, which this index handles poorly; a plain topic query returns the primary source as a top hit). Cover: AI model releases / provider announcements; framework/library updates relevant to a Python+TypeScript agent SDK team; MCP / agent-tooling news; security advisories in our deps. Run at least one query per preferred domain from Sources; when a result is on a preferred domain, prefer it, and prefer the primary source over secondary coverage. Keep only in-window items. <=6 bullets, a link + one-line 'why it matters to us' each. If the `web` tool is absent (gated on STRANDLY_SEARCH_MCP_URL), say so in one line.",
  system_prompt="skills/brief/writer.md",
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
