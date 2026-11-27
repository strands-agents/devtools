# Integration audit report

The auditor's output. Fill every field; a gap is `UNAVAILABLE (<why>)`.

```
IMPLEMENTS: <protocol at file:line> — declared integrationType: <x> → matches | should be <y> | redirect to <primitive>
ARTIFACT ≡ SOURCE: IDENTICAL | DIFFERS (<files>) | UNAVAILABLE (<why>)
CLASSIFICATION: local-only | strands-ecosystem | declared-vendor | undeclared-third-party
CHAIN: intact | broken at <link> | unverifiable at <link>

Scorecard:
| Signal | Value | Source |
| Submitter | @login, created …, N repos, N followers; org member / contributor (N commits) / none | gh api users/…, orgs/…/public_members |
| Org | created …, is_verified …, N repos, N members | gh api orgs/… |
| Repo | created …, pushed …, fork/archived, ★N, forks N, contributors N, commits N, releases N, license, SECURITY.md, CI | gh api repos/… |
| Registry | first …, latest vX (…), N releases, yanked […], downloads/mo N | pypi.org / registry.npmjs.org / pypistats |
| Registry ↔ repo | MATCH / MISMATCH(<slug>) / UNVERIFIABLE | project_urls |
| Attestation | Trusted Publishing <owner/repo> <workflow> / none | pypi.org/integrity |
| SDK pin | requires_dist …; extra present | pypi json |
| Advisories | none / <ids> | api.osv.dev |
| Install | strands-agents==v + pkg==v → OK / CONFLICT; import → OK / <err> | pip |

Egress map:
| Destination | Data | When | Built at (file:line) | Declared? |

SDK surface: <imports; public | private (file:line)>   Hazards: none | <file:line — verdict>
Claims vs code: "<claim>" → <file:line | not found>

Findings (most severe first):
1. [🔴|🟡|⚪ fit|provenance|data-flow|security|installability|description|catalog-policy] <file>:<line> — <what> — <evidence>
Corrected entry (when fields need fixing): <yaml>
Maintainer questions: <tier evidence, editorial risk shape, anything UNAVAILABLE>
Skipped: <what needed credentials you don't have>
```
