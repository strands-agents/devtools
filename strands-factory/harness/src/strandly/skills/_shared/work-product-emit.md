# Work-product block (portal hand-off)

`code-review`, `integration-review`, `dependabot-review`, `triage`, and `first-response` end their final message with **one**
fenced block whose info string is `work-product`, containing one strict-JSON object. It is portal
metadata — the runtime strips it; it is never posted to GitHub. Invalid JSON = silently dropped.

Common fields: `kind` and `verdict` (required), `title` (the GitHub item's title — always include),
`tldr`, `target_url`, `repo`, `number`. Omit empty fields.

| `kind` | `verdict` | extra fields |
|---|---|---|
| `review` | `approve` \| `changes-requested` | `evidence_ledger` `{branch_sha, tests_cmd, tests_passed, repros}`; `findings[]` — every finding, posted **and** suppressed |
| `triage` | `accept` \| `defer` \| `redirect` \| `reject` \| `escalate` | — |
| `first-response` | `needs-info` \| `confirmed-bug` \| `duplicate` \| `wrong-repo` \| `works-as-intended` \| `escalate` | `priority` (P0–P3, confirmed bugs only), `repro_status`, `suspected_area`, `recommended_labels[]`, `duplicates[]`, `draft_comment` |

Finding object: `id`, `file`, `line` (one integer), `tier` (`block`/`should-fix`/`nit`), `category`,
`confidence` (a number 0–1 like `0.9` — not a word like `"high"`), `text`, `suggestion`, `reachability`,
`suppressed` (bool), `suppress_reason` (when suppressed).

```work-product
{"kind": "review", "verdict": "changes-requested", "title": "Cap retry backoff", "tldr": "one reachable bug in the retry path",
 "target_url": "https://github.com/o/r/pull/42", "repo": "o/r", "number": 42,
 "evidence_ledger": {"branch_sha": "abc1234", "tests_cmd": "pytest", "tests_passed": 128, "repros": 1},
 "findings": [{"id": "f1", "file": "src/retry.py", "line": 88, "tier": "block", "category": "correctness", "confidence": 0.9,
   "text": "Re-raise inside finally loses the original error.", "reachability": "any Agent.invoke() on a 429", "suppressed": false}]}
```
