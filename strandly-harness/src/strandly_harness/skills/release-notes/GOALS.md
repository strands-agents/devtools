# Goals: release-notes

Critic acceptance criteria when the `release-notes` skill is active — producing the curated layer
of release notes between two git refs, with validated examples.

- **Both refs are identified** (base + head) and the PR list's provenance is stated: parsed from an
  existing GitHub release body, or built via the compare API.
- **Every PR between the refs is categorized** (Major Features / Major Bug Fixes / Minor), the full
  categorization is visible in the report, and the buckets are proportionate (~3–8 Major Features,
  0–5 Major Bug Fixes — a 30-item "Major Features" list is a gap).
- **Every Major Feature has a code example**, and every example was **validated by a test that was
  actually run** with behavioral assertions (evidence in the report: the test code AND its pass
  output). A parse/import/instantiate-only check does not count as validation.
- **Unvalidated examples are marked, not hidden:** any example that couldn't be validated carries
  the inline `⚠️ NEEDS ENGINEER VALIDATION` marker AND the report shows the documented attempts
  with real error text (a Bedrock/deps/mock/simplify escalation, not a vague excuse).
- **No feature was dropped because validation failed** (Principle 3).
- **The notes match the format contract:** `## Major Features` with `### Name - [PR#N](link)`
  subsections in prose (no bullet-point descriptions), fenced examples with a language tag,
  optional `## Major Bug Fixes` bullets after a `---`, a final `---` separator, and **no**
  "Full Changelog" link.
- **The report has the three blocks** in order: validation evidence (collapsed per feature), the
  release-notes markdown, exclusions/why.
- **Any AWS resources created for validation** followed the e2e-test boundary (`ManagedBy=strandly`
  tag, `strandly-managed-*` names) and were deleted — or their ids are called out explicitly.
- **Nothing was published or posted** (no release created/edited, no comments) unless the user
  explicitly asked.
