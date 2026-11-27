/**
 * Fake data layer — canned WorkProducts + diffs so the app runs and demos with no backend.
 * Toggle with VITE_DATA_SOURCE=mock (default) or config.json `dataSource: "mock"`.
 *
 * The shapes here are exactly the contract's (§1/§3/§4). Keep them realistic — they double as a
 * reference for the backend serializer. Repos reflect the current layout: the SDK monorepo
 * `strands-agents/harness-sdk`, plus `tools` / `samples` and the `strands-labs` org.
 */
import type { WorkProduct } from "../types/workProduct";
import type { Schedule } from "./types";

function iso(minutesAgo: number): string {
  return new Date(Date.now() - minutesAgo * 60_000).toISOString();
}

const REVIEW_1: WorkProduct = {
  work_product_id: "wp_review_0001",
  session_id: "gh-strands-agents-harness-sdk-e0b64059-pr-4212",
  kind: "review",
  status: "draft",
  created_at: iso(18),
  updated_at: iso(18),
  verdict: "changes-requested",
  title: "Cap retry backoff so Bedrock 429s don't hang the turn",
  target_url: "https://github.com/strands-agents/harness-sdk/pull/4212",
  repo: "strands-agents/harness-sdk",
  number: 4212,
  tldr:
    "One real correctness bug: the retry backoff overflows on the 10th attempt and the session " +
    "manager can drop the final assistant message. Tests pass but don't cover the overflow path. " +
    "Two nits suppressed (linter-owned).",
  evidence_ledger: { branch_sha: "a1b2c3d", tests_cmd: "pytest", tests_passed: 214, repros: 1 },
  findings: [
    {
      id: "f1",
      file: "src/agent/retry.py",
      line: 42,
      tier: "block",
      category: "correctness",
      confidence: 0.92,
      text:
        "`2 ** attempt` with `attempt` unbounded overflows the float cap at attempt≈1024 and, more " +
        "immediately, produces a >17-minute sleep by attempt 10. Clamp the exponent (e.g. " +
        "`min(attempt, MAX_BACKOFF_EXP)`).",
      suggestion: "        delay = base * (2 ** min(attempt, MAX_BACKOFF_EXP))",
      reachability: "Hit whenever Bedrock throttles ≥10× in one turn — observed in the throttle repro.",
      suppressed: false,
    },
    {
      id: "f2",
      file: "src/memory/session.py",
      line: 133,
      tier: "should-fix",
      category: "correctness",
      confidence: 0.71,
      text:
        "`events[:100]` truncates before the final assistant message is appended on long runs, so " +
        "the persisted transcript can miss the answer. Paginate or append before slicing.",
      reachability: "Runs longer than 100 events (multi-tool reviews).",
      suppressed: false,
    },
    {
      id: "f3",
      file: "src/agent/retry.py",
      line: 8,
      tier: "nit",
      category: "style",
      confidence: 0.35,
      text: "Prefer `logging.getLogger(__name__)` over the module-level root logger.",
      suppressed: true,
      suppress_reason: "Owned by ruff (rule already enforced elsewhere); not worth a review line.",
    },
    {
      id: "f4",
      file: "src/memory/session.py",
      line: 201,
      tier: "nit",
      category: "docs",
      confidence: 0.28,
      text: "Docstring says 'oldest-first' but the code returns newest-first.",
      suppressed: true,
      suppress_reason: "Cosmetic; folded into the should-fix comment above.",
    },
  ],
  recommended_labels: [],
  run_task_id: "task_9f3a21",
};

const REVIEW_2: WorkProduct = {
  work_product_id: "wp_review_0002",
  session_id: "gh-strands-agents-tools-a2986840-pr-318",
  kind: "review",
  status: "draft",
  created_at: iso(95),
  updated_at: iso(95),
  verdict: "approve",
  title: "Add use_google search tool",
  target_url: "https://github.com/strands-agents/tools/pull/318",
  repo: "strands-agents/tools",
  number: 318,
  tldr: "Clean addition of the `use_google` tool. Good tests, no correctness concerns. Approving.",
  evidence_ledger: { branch_sha: "ff01aa9", tests_cmd: "pytest tests/tools", tests_passed: 41, repros: 0 },
  findings: [
    {
      id: "g1",
      file: "src/strands_tools/use_google.py",
      line: 57,
      tier: "nit",
      category: "api-shape",
      confidence: 0.4,
      text: "Consider defaulting `num_results` to 5 to match `use_github`'s convention.",
      suppressed: false,
    },
  ],
  recommended_labels: [],
  run_task_id: "task_7c1b02",
};

const TRIAGE_1: WorkProduct = {
  work_product_id: "wp_triage_0001",
  session_id: "gh-strands-agents-harness-sdk-e0b64059-issue-4198",
  kind: "triage",
  status: "draft",
  created_at: iso(35),
  updated_at: iso(35),
  verdict: "accept",
  title: "Streaming stops mid-tool-call when the Bedrock socket idles",
  target_url: "https://github.com/strands-agents/harness-sdk/issues/4198",
  repo: "strands-agents/harness-sdk",
  number: 4198,
  tldr: "Real bug: streaming stops mid-tool-call on Bedrock when the socket idles. Repro attached.",
  suspected_area: "src/agent/streaming.py",
  repro_status: "reproduced",
  priority: "P1",
  recommended_labels: ["bug", "area/streaming", "size/M"],
  duplicates: [],
  draft_comment:
    "Thanks for the detailed repro! Confirmed — the stream terminates when the Bedrock socket idles " +
    "past 30s during a long tool call. Tracking as a P1; a keepalive on the event stream should fix it.",
  run_task_id: "task_a11c90",
};

const TRIAGE_2: WorkProduct = {
  work_product_id: "wp_triage_0002",
  session_id: "gh-strands-agents-harness-sdk-e0b64059-issue-4200",
  kind: "triage",
  status: "draft",
  created_at: iso(52),
  updated_at: iso(52),
  verdict: "duplicate" as WorkProduct["verdict"],
  title: "Stream terminates on idle (Bedrock)",
  target_url: "https://github.com/strands-agents/harness-sdk/issues/4200",
  repo: "strands-agents/harness-sdk",
  number: 4200,
  tldr: "Duplicate of #4198 (same idle-stream symptom).",
  repro_status: "not-reproduced",
  recommended_labels: ["duplicate"],
  duplicates: ["strands-agents/harness-sdk#4198"],
  draft_comment: "This looks like a duplicate of #4198 — closing in favor of that thread. Please follow along there.",
  run_task_id: "task_a11c91",
};

const TRIAGE_3: WorkProduct = {
  work_product_id: "wp_triage_0003",
  session_id: "gh-strands-agents-samples-ddde89b6-issue-77",
  kind: "triage",
  status: "draft",
  created_at: iso(70),
  updated_at: iso(70),
  verdict: "redirect",
  title: "SDK feature request filed on the samples repo",
  target_url: "https://github.com/strands-agents/samples/issues/77",
  repo: "strands-agents/samples",
  number: 77,
  tldr: "Feature request belongs on the harness-sdk monorepo, not samples. Suggest redirect.",
  suspected_area: "wrong-repo",
  repro_status: "needs-info",
  recommended_labels: ["needs-info", "wrong-repo"],
  duplicates: [],
  draft_comment: "This is an SDK feature request rather than a samples issue — could you re-file on strands-agents/harness-sdk?",
  run_task_id: "task_a11c92",
};

const FIRST_RESPONSE_1: WorkProduct = {
  work_product_id: "wp_fr_0001",
  session_id: "gh-strands-labs-robots-f48f7171-disc-512",
  kind: "first-response",
  status: "draft",
  created_at: iso(12),
  updated_at: iso(12),
  verdict: "needs-info",
  title: "Bedrock auth error running the robots sample",
  target_url: "https://github.com/strands-labs/robots/discussions/512",
  repo: "strands-labs/robots",
  number: 512,
  tldr: "Discussion awaiting reply — asker hit a Bedrock auth error; need region + model id to help.",
  suspected_area: "auth/region config",
  repro_status: "needs-info",
  recommended_labels: ["question"],
  duplicates: [],
  draft_comment:
    "Happy to help! Could you share the region you're calling and the model id? A `ValidationException` " +
    "here usually means the model isn't enabled in that region's Bedrock console.",
  run_task_id: "task_b22d01",
};

const FIRST_RESPONSE_2: WorkProduct = {
  work_product_id: "wp_fr_0002",
  session_id: "gh-strands-agents-harness-sdk-e0b64059-issue-4177",
  kind: "first-response",
  status: "draft",
  created_at: iso(240),
  updated_at: iso(240),
  verdict: "confirmed-bug",
  title: "Agent crashes on an empty tool result",
  target_url: "https://github.com/strands-agents/harness-sdk/issues/4177",
  repo: "strands-agents/harness-sdk",
  number: 4177,
  tldr: "P0: agent crashes on empty tool result. SLA breach risk — awaiting reply 4h.",
  suspected_area: "src/event_loop/loop.py",
  repro_status: "reproduced",
  priority: "P0",
  recommended_labels: ["bug", "P0", "area/tools"],
  duplicates: [],
  draft_comment:
    "Confirmed and reproduced — an empty tool result raises `IndexError` in the event loop. This is a " +
    "P0 for us; a fix is going up now. Thanks for flagging.",
  run_task_id: "task_b22d02",
};

export const MOCK_WORK_PRODUCTS: WorkProduct[] = [
  FIRST_RESPONSE_2, // P0, oldest → should sort to top by priority heuristic
  REVIEW_1,
  FIRST_RESPONSE_1,
  TRIAGE_1,
  TRIAGE_2,
  TRIAGE_3,
  REVIEW_2,
];

// A few already-curated/posted rows, so history + non-draft filters have something to show.
export const MOCK_HISTORY: WorkProduct[] = [
  {
    ...REVIEW_1,
    work_product_id: "wp_review_0001_prev",
    status: "posted",
    created_at: iso(1440),
    updated_at: iso(1400),
    posted_at: iso(1400),
    github_review_id: "PRR_kwABC123",
    curated_by: "example-maintainer",
    tldr: "Initial review posted; author addressed the backoff bug in a follow-up commit.",
  },
];

// --- Scheduled jobs --------------------------------------------------------------------------------

/** The model the mock deployment runs when a chat names none (the harness default). */
export const MOCK_DEFAULT_MODEL = "bedrock/global.anthropic.claude-opus-5-5";

/** The model providers the harness model resolution knows (strands_harness.models). */
export const MOCK_MODEL_PROVIDERS = ["anthropic", "bedrock", "bedrock-mantle", "google", "litellm", "ollama", "openai"];

/** The skills the mock's schedule form offers. */
export const MOCK_SCHEDULE_SKILLS = ["dependabot-review", "parity-sync", "test-simplify"];

/** Canned jobs, timed relative to now: two built-ins, a paused portal job and a once reminder. */
export function mockSchedules(): Schedule[] {
  const seed = { actor: "system:seed", surface: "system" };
  const alice = { actor: "portal:alice@example.com", surface: "dashboard" };
  const base = {
    starts_at: null,
    expires_at: null,
    manual_run_at: null,
    sync_state: "synced" as const,
    sync_error: null,
    next_runs: [],
  };
  return [
    {
      ...base,
      id: "daily-parity-sync",
      name: "daily-parity-sync",
      kind: "recurring",
      expression: "cron(0 14 * * ? *)",
      timezone: "UTC",
      prompt: "Run the parity-sync skill over every PR merged to strands-agents/harness-sdk yesterday (UTC).",
      skill: "parity-sync",
      session_mode: "daily",
      notify: null,
      status: "active",
      enabled: true,
      version: 2,
      created_by: seed,
      updated_by: alice,
      created_at: iso(60 * 24 * 20),
      updated_at: iso(60 * 24 * 2),
      last_run: {
        time: iso(60 * 20),
        status: "dispatched",
        session_id: `sched-daily-parity-sync-${new Date(Date.now() - 20 * 3_600_000).toISOString().slice(0, 10).replace(/-/g, "")}`,
        trigger: "schedule",
      },
    },
    {
      ...base,
      id: "weekly-test-simplify",
      name: "weekly-test-simplify",
      kind: "recurring",
      expression: "cron(0 15 ? * MON *)",
      timezone: "UTC",
      prompt: "Run the test-simplify skill over the SDK and evals suites; keep at most 3 PRs open.",
      skill: "test-simplify",
      session_mode: "daily",
      notify: null,
      status: "active",
      enabled: true,
      version: 1,
      created_by: seed,
      updated_by: seed,
      created_at: iso(60 * 24 * 20),
      updated_at: iso(60 * 24 * 20),
      last_run: { time: iso(60 * 24 * 3), status: "busy", session_id: "sched-weekly-test-simplify-20300101", trigger: "schedule" },
    },
    {
      ...base,
      id: "triage-sweep-a1b2c3",
      name: "Triage sweep",
      kind: "recurring",
      expression: "rate(6 hours)",
      timezone: "Europe/Istanbul",
      prompt: "Look for new issues without a first response in strands-agents/harness-sdk and draft one.",
      skill: null,
      session_mode: "single",
      notify: { kind: "slack", channel: "C0123ABCD" },
      status: "paused",
      enabled: false,
      version: 3,
      created_by: alice,
      updated_by: alice,
      created_at: iso(60 * 24 * 5),
      updated_at: iso(60 * 5),
      last_run: null,
      sync_state: "unsynced",
      sync_error: "ThrottlingException: Rate exceeded",
    },
    {
      ...base,
      id: "release-reminder-d4e5f6",
      name: "Release notes reminder",
      kind: "once",
      expression: `at(${new Date(Date.now() + 2 * 86_400_000).toISOString().slice(0, 10)}T09:00:00)`,
      timezone: "America/New_York",
      prompt: "Draft the release notes for the next harness-sdk release and post them on the tracking issue.",
      skill: null,
      session_mode: "per-run",
      notify: { kind: "github", url: "https://github.com/strands-agents/harness-sdk/issues/4100" },
      status: "active",
      enabled: true,
      version: 1,
      created_by: alice,
      updated_by: alice,
      created_at: iso(90),
      updated_at: iso(90),
      last_run: null,
    },
  ];
}
