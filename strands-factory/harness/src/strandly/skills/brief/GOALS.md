# Goals: brief

Acceptance checks when `brief` is active. Verify each with tools (re-read the written file; check claims against what the source tools returned in the transcript). Do not trust the narrative.

- **File written.** The brief exists at the output path (dated `morning-brief-<date>.md` when a directory was given) and the TL;DR was printed to the terminal. Described-but-not-written does NOT clear.
- **Nothing fabricated.** Spot-check at least one item per section: each must trace to a real tool result. An item no tool returned is a hard failure.
- **Unavailable sources stated, not faked.** An empty/errored/absent source yields one honest line, never invented content.
- **GitHub via `use_github` when available.** The `gh`/bash fallback is acceptable only when `use_github` was genuinely absent.
- **Strands is feature-level.** Grouped features/fixes leading with any release, not a per-PR/issue dump.
- **Ecosystem pursued the preferred domains.** The transcript shows natural-language queries aimed at each preferred domain the skill lists, not only generic ones, and prefers the primary-source URL over secondary coverage.
- **Every item linked.** A real URL on each (except the honest "nothing new" / "not configured" lines).
- **TL;DR summarizes.** Nothing in it that is not supported by a section below.
- **Both sections present**, scannable, one line per bullet, no padding.
- **Writing style.** Single-line bullets/paragraphs; no em or en dashes.
- **Scoped.** GitHub activity limited to the configured repos and the window.
