/**
 * Mock PortalApi — serves the fixtures with a little latency so `npm run dev` works with no backend:
 * canned sessions, a long paginated transcript for the fixture sessions, and a streamed canned reply
 * to any chat message. Nothing is persisted; a reload resets to the fixtures.
 */
import type { WorkProduct } from "../types/workProduct";
import type {
  ChatEvent,
  ChatLaunch,
  ChatMessage,
  ChatPoll,
  NotifyTarget,
  PortalApi,
  Schedule,
  ScheduleChanges,
  ScheduleDetail,
  ScheduleList,
  SchedulePreview,
  ScheduleRevision,
  ScheduleRunOutcome,
  ScheduleRuns,
  ScheduleSpec,
  SessionHistoryQuery,
  SessionRun,
  SessionSnapshot,
  SessionSummary,
  StartChatOptions,
} from "./types";
import {
  MOCK_DEFAULT_MODEL,
  MOCK_HISTORY,
  MOCK_MODEL_PROVIDERS,
  MOCK_SCHEDULE_SKILLS,
  MOCK_WORK_PRODUCTS,
  mockSchedules,
} from "./fixtures";
import { ApiError, CursorExpiredError } from "./errors";
import { presetFor } from "../features/schedules/schedule";
import { cleanTitle, SESSION_TITLE_MAX_LEN } from "../lib/sessionTitle";

const delay = (ms = 180) => new Promise((r) => setTimeout(r, ms));
const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v)) as T;

/**
 * A canned agent reply, echoing the user's message so the streamed turn reads coherently. Written
 * in Markdown (a heading, a table, a list, code) the way the real agent answers, so the demo shows
 * the panel RENDERING it — not the raw `|---|` and `**` the agent typed.
 */
function mockReply(message: string): string {
  const asked = message.trim().slice(0, 200) || "your question";
  return (
    `Here's a **mock reply** — no runtime is running behind this demo. You asked: _"${asked}"_.\n\n` +
    `### What I checked\n\n` +
    `| Check | Result |\n|---|---|\n| Session memory | loaded (mock) |\n| Finding vs \`HEAD\` | still holds |\n\n` +
    `In a live deployment I'd:\n\n` +
    `1. re-read this item's context from my session memory,\n` +
    `2. check the finding against the current commit, and\n` +
    `3. answer here.\n\n` +
    `Toggle to a live API base to talk to the real agent.`
  );
}

/** A reply split into small text-delta events so the panel visibly streams. */
function textDeltas(reply: string): ChatEvent[] {
  const words = reply.split(" ");
  const chunks: string[] = [];
  for (let i = 0; i < words.length; i += 8) chunks.push(words.slice(i, i + 8).join(" ") + " ");
  return chunks.map((text) => ({ kind: "text", text }) as ChatEvent);
}

/** Every text delta of a script joined — the runtime's `lastTurnResult` for a turn is exactly this. */
function streamedText(events: ChatEvent[]): string {
  return events.map((e) => (e.kind === "text" ? e.text ?? "" : "")).join("");
}

/**
 * The full event script a mock turn streams, shaped like a real turn — narration, then a tool call
 * (with its complete `tool_input` arguments, so the expandable detail view has something real to
 * show), more narration, a `subagent` call (dispatched to the background, so its result is the
 * dispatch acknowledgement), then the reply in a few text deltas. Text and tool activity INTERLEAVE on purpose: the panel must
 * render them in this order, not every tool first and the narration merged into the reply.
 */
function mockChatEvents(message: string): ChatEvent[] {
  return [
    { kind: "turn_start", data: { reason: "start" } },
    ...textDeltas("Let me pull up what this session already knows."),
    { kind: "tool_start", tool: "read_session_memory", toolUseId: "mock-tool-1", data: { input: "" } },
    {
      kind: "tool_input",
      tool: "read_session_memory",
      toolUseId: "mock-tool-1",
      data: {
        input: JSON.stringify({ session_id: "mock", window: "recent", include: ["findings", "turns"] }, null, 2),
      },
    },
    {
      kind: "tool_result",
      toolUseId: "mock-tool-1",
      data: { status: "success", content: [{ text: "loaded prior context for this session (mock)" }] },
    },
    ...textDeltas("Context loaded — double-checking the finding against the head commit before I answer."),
    { kind: "tool_start", tool: "subagent", toolUseId: "mock-subagent-1", data: { input: '{"task": "double-check the finding against the head commit"}' } },
    {
      kind: "tool_result",
      toolUseId: "mock-subagent-1",
      data: { status: "success", content: [{ text: "Background task dispatched (mock); the report arrives when it finishes." }] },
    },
    ...textDeltas(mockReply(message)),
  ];
}

/** One live (or replayed) mock chat run. */
interface MockChatTask {
  sessionId: string;
  events: ChatEvent[];
  /** High-water mark of the cursor a poll has been answered with — past the end means fully drained. */
  served: number;
  /** The FINAL turn's text (`lastTurnResult` — all its deltas joined); null after a cancel (keep what the client streamed). */
  reply: string | null;
  /** Stopped, or superseded by a newer message: polls report `cancelled` once the script is served. */
  cancelled?: boolean;
}

/** Messages in a known mock session's canned transcript — long enough to need several history pages. */
export const MOCK_TRANSCRIPT_LENGTH = 130;

/**
 * A deterministic canned transcript for a fixture session: alternating user asks and short agent
 * replies, oldest first, each with a stable id so the chat can dedup pages. Every fourth turn is
 * stored THE WAY THE SDK STORES IT — the assistant message with the tool call, then a user-role
 * message holding only the tool result, then the assistant's follow-up — so the chat's fold of
 * stored turns (chatView `foldToolTurns`) is exercised with no backend. Every sixth reply carries a
 * fenced code block (the Markdown highlighter). Plain prose otherwise — long enough to page (see
 * MOCK_TRANSCRIPT_LENGTH), small per message.
 */
function mockTranscript(sessionId: string): ChatMessage[] {
  const out: ChatMessage[] = [];
  const base = Date.now() - MOCK_TRANSCRIPT_LENGTH * 90_000;
  let turn = 0;
  while (out.length < MOCK_TRANSCRIPT_LENGTH) {
    const timestamp = () => new Date(base + out.length * 90_000).toISOString();
    const historyId = () => `${sessionId}#${out.length}`;
    turn += 1;
    const ask = `Mock question ${turn}: what changed since the last check?`;
    const context = `# Caller context (what the user is currently viewing)\n\n> {"view": "triage", "number": ${turn}}`;
    out.push({
      role: "user",
      text: ask,
      blocks: [{ kind: "text", text: ask }, { kind: "context", source: "caller_context:injector", text: context }],
      timestamp: timestamp(),
      historyId: historyId(),
    });
    const text =
      turn % 6 === 0
        ? `Mock reply ${turn} — the retry lives here:\n\n\`\`\`ts\nexport function retry(n: number): boolean {\n  return n < 3; // one more\n}\n\`\`\`\n\nInline \`code\` too.`
        : `Mock reply ${turn} — nothing new since the previous turn; the finding still holds.`;
    if (turn % 4 === 0) {
      const callId = `${historyId()}-t1`;
      out.push({
        role: "agent",
        text: "Let me check the log.",
        blocks: [
          { kind: "text", text: "Let me check the log." },
          { kind: "tool", id: callId, name: "shell", input: JSON.stringify({ command: "git log --oneline -3" }) },
        ],
        timestamp: timestamp(),
        historyId: historyId(),
      });
      out.push({
        role: "user",
        text: "",
        blocks: [{ kind: "result", id: callId, status: "success", text: "abc123 fix retry\nabc122 docs\nabc121 tests" }],
        timestamp: timestamp(),
        historyId: historyId(),
      });
    }
    out.push({ role: "agent", text, blocks: [{ kind: "text", text }], timestamp: timestamp(), historyId: historyId() });
  }
  return out.slice(0, MOCK_TRANSCRIPT_LENGTH);
}

// --- Scheduled jobs (in memory) --------------------------------------------------------------------

const MOCK_ACTOR = {
  actor: "portal:11111111-2222-3333-4444-555555555555",
  surface: "dashboard",
  issuer: "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_demo",
};
const SCHEDULE_DEFINITION = ["name", "kind", "expression", "timezone", "prompt", "skill", "session_mode", "notify", "status", "expires_at"] as const;

/** The live client's error shape, so views handle mock and live failures alike. */
function scheduleError(method: string, path: string, status: number, code: string, error: string): ApiError {
  return new ApiError(`${method} ${path} → ${status}: ${JSON.stringify({ error, code })}`, status, code);
}

/** Local calendar fields represented as UTC, for offset arithmetic without the browser's timezone. */
function wallTime(time: number, timezone: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: timezone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(new Date(time));
  const get = (key: string) => Number(parts.find((p) => p.type === key)?.value);
  return Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"), get("second"));
}

/** Resolve a wall clock in its zone; skip DST gaps and fire once at the first overlap. */
function instantForWall(wall: number, timezone: string): number | null {
  const offsets = new Set([-86_400_000, 0, 86_400_000].map((d) => wallTime(wall + d, timezone) - (wall + d)));
  const candidates = [...offsets].map((offset) => wall - offset).filter((t) => wallTime(t, timezone) === wall);
  return candidates.length ? Math.min(...candidates) : null;
}

/** Demo support for daily/weekly presets, rates and one-shots, including IANA timezone/DST rules. */
export function mockNextRuns(expression: string, now: number, count = 5, timezone = "UTC", startsAt?: string | null): string[] {
  const p = presetFor(expression);
  const out: string[] = [];
  if (p.kind === "once") {
    const wall = Date.parse(`${expression.trim().slice(3, -1)}Z`);
    if (!Number.isFinite(wall)) return [];
    const t = instantForWall(wall, timezone);
    return t !== null && t > now ? [new Date(t).toISOString()] : [];
  }
  const rate = /^rate\(([1-9]\d*) (minute|hour|day)s?\)$/.exec(expression.trim());
  if (rate) {
    const step = Number(rate[1]) * { minute: 60_000, hour: 3_600_000, day: 86_400_000 }[rate[2] as "minute" | "hour" | "day"];
    const start = startsAt ? Date.parse(startsAt) : now;
    const first = Math.max(1, Math.floor((now - start) / step) + 1);
    for (let i = 0; i < count; i++) out.push(new Date(start + (first + i) * step).toISOString());
    return out;
  }
  if ((p.preset === "daily" || p.preset === "weekly") && p.time) {
    const [h, m] = p.time.split(":").map(Number);
    const days = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];
    const d = new Date(wallTime(now, timezone));
    d.setUTCHours(h, m, 0, 0);
    while (out.length < count) {
      const t = instantForWall(d.getTime(), timezone);
      if (t !== null && t > now && (p.preset === "daily" || days[d.getUTCDay()] === p.weekday)) out.push(new Date(t).toISOString());
      d.setUTCDate(d.getUTCDate() + 1);
    }
  }
  return out;
}

/** Like the server: never a Slack DM; the portal may keep or clear a job's Slack target, never set one. */
function mockNotifyErrors(notify: NotifyTarget | null | undefined, original?: Schedule): string[] {
  if (notify?.kind !== "slack") return [];
  if ((notify.channel ?? "").startsWith("D")) return ["a scheduled job never posts to a Slack DM"];
  const kept = original?.notify;
  const same = kept?.kind === "slack" && kept.channel === notify.channel && (kept.thread_ts ?? null) === (notify.thread_ts ?? null);
  return same ? [] : ["a job posts to Slack only in the home channel: ask Strandly from the home-channel thread it should post in"];
}

function mockScheduleErrors(spec: ScheduleSpec, now: number, original?: Schedule): string[] {
  if (!spec.name?.trim()) return ["name must be 1-200 characters"];
  if (!spec.prompt?.trim()) return ["prompt is empty"];
  const expr = spec.expression?.trim() ?? "";
  if (!/^(at|cron|rate)\(.+\)$/.test(expr)) return [`not an at(), cron() or rate() expression: '${expr}'`];
  const rate = /^rate\((\d+) minutes?\)$/.exec(expr);
  if (rate && Number(rate[1]) < 60) return [`${expr} runs more often than once an hour, the minimum interval`];
  try {
    wallTime(now, spec.timezone);
  } catch {
    return [`unknown timezone: '${spec.timezone}'`];
  }
  const unchangedCompletedTime = original?.status === "completed" && original.expression === expr && original.timezone === spec.timezone;
  if (expr.startsWith("at(") && !unchangedCompletedTime && mockNextRuns(expr, now, 1, spec.timezone).length === 0)
    return [`${expr} in ${spec.timezone} is in the past or not a valid local time`];
  return [];
}

export class MockApi implements PortalApi {
  private store: WorkProduct[] = clone(MOCK_WORK_PRODUCTS);
  private history: WorkProduct[] = clone(MOCK_HISTORY);
  // Live chat turns launched this session, keyed by task id (so poll can stream the canned reply).
  private chatTasks = new Map<string, MockChatTask>();
  private chatRuns = new Map<string, SessionRun[]>();
  private titles = new Map<string, string>();
  private schedules: Schedule[] = mockSchedules();
  private scheduleRevisions = new Map<string, ScheduleRevision[]>(
    this.schedules.map((j) => [j.id, [{ rev: j.version, action: j.version === 1 ? "create" : "update", actor: j.updated_by, at: j.updated_at, snapshot: snapshotOf(j) }]]),
  );

  private all(): WorkProduct[] {
    return [...this.store, ...this.history];
  }

  async getReviewForSession(sessionId: string): Promise<WorkProduct | null> {
    await delay(80);
    // Newest draft review for the session (known mock PR sessions have one; unknown PRs → null).
    const drafts = this.all()
      .filter((w) => w.session_id === sessionId && w.kind === "review" && w.status === "draft")
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    return drafts[0] ? clone(drafts[0]) : null;
  }

  async getTriageForSession(sessionId: string): Promise<WorkProduct | null> {
    await delay(80);
    // Newest draft triage-family artifact for the session, preferring `triage` over `first-response`.
    const drafts = this.all()
      .filter(
        (w) =>
          w.session_id === sessionId &&
          w.status === "draft" &&
          (w.kind === "triage" || w.kind === "first-response"),
      )
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    const hit = drafts.find((w) => w.kind === "triage") ?? drafts[0];
    return hit ? clone(hit) : null;
  }

  // --- Chat (canned streaming reply so the panel works with no backend) -------------------------

  async startChat(sessionId: string, message: string, opts: StartChatOptions = {}): Promise<ChatLaunch> {
    await delay(120);
    // The live runtime's harness model resolution refuses an unknown provider (the only check
    // mirrored here, so the panel's refusal path works with no backend).
    const provider = opts.model?.includes("/") ? opts.model.split("/")[0] : "bedrock";
    if (opts.model && !MOCK_MODEL_PROVIDERS.includes(provider)) {
      const error = `Unknown model provider '${provider}' in '${opts.model}'. Supported providers: ${MOCK_MODEL_PROVIDERS.join(", ")}.`;
      throw new ApiError(`POST /api/chat → 400: ${JSON.stringify({ error })}`, 400, undefined, error);
    }
    // Supersede parity with the runtime: a message launched while the session's mock run is still
    // streaming cancels that run (it reports `cancelled`) and starts a new one.
    this.cancelRunning(sessionId);
    const taskId = "mock-chat-" + Math.random().toString(36).slice(2, 8);
    const events = mockChatEvents(message);
    this.chatTasks.set(taskId, { sessionId, events, served: 0, reply: streamedText(events) });
    const run: SessionRun = {
      taskId,
      startedAt: new Date().toISOString(),
      status: "running",
      model: opts.model ?? MOCK_DEFAULT_MODEL,
      effort: opts.effort ?? (opts.model ? "auto" : "medium"),
    };
    this.chatRuns.set(sessionId, [...(this.chatRuns.get(sessionId) ?? []), run]);
    return { taskId, status: "accepted" };
  }

  async pollChat(taskId: string, _sessionId: string, since = 0): Promise<ChatPoll> {
    await delay(450);
    const task = this.chatTasks.get(taskId);
    const events = task?.events ?? [];
    const cursor = since > 0 ? since : 0; // a negative cursor (tail attach) starts at the head here
    const window = events.slice(cursor, cursor + 2); // two events per poll → a visible stream
    const nextSeq = cursor + window.length;
    if (task) task.served = Math.max(task.served, nextSeq);
    const done = nextSeq >= events.length;
    return {
      status: done ? (task?.cancelled ? "cancelled" : "completed") : "running",
      events: window,
      nextSeq,
      result: done ? (task?.reply ?? undefined) : undefined,
      lastTurnResult: done ? task?.reply : undefined,
    };
  }

  async cancelChat(sessionId: string): Promise<{ status: string }> {
    await delay(80);
    return { status: this.cancelRunning(sessionId) ? "cancelled" : "idle" };
  }

  /**
   * Stop the session's in-flight mock run: truncate its script at what was already served, so the
   * next poll reports it cancelled. `reply: null` keeps whatever the panel streamed.
   */
  private cancelRunning(sessionId: string): boolean {
    for (const task of this.chatTasks.values()) {
      if (task.sessionId === sessionId && !task.cancelled && task.served < task.events.length) {
        task.events.length = task.served;
        task.reply = null;
        task.cancelled = true;
        return true;
      }
    }
    return false;
  }

  async loadSession(sessionId: string, query?: SessionHistoryQuery): Promise<SessionSnapshot> {
    await delay(80);
    // A session the fixtures know (one with work products) carries a long canned transcript so the
    // chat's history paging is exercised with no backend; anything else is a fresh conversation.
    // Honors `limit`/`before` exactly like the live route: newest page first, opaque cursor for the
    // page before it, null at the beginning, 409-equivalent on a cursor that no longer parses.
    const all = this.all().some((w) => w.session_id === sessionId) ? mockTranscript(sessionId) : [];
    const title = this.titles.get(sessionId) ?? null;
    const runs = [...(this.chatRuns.get(sessionId) ?? [])];
    if (!query) return { messages: all, nextCursor: null, title, runs };
    const limit = Math.max(1, Math.min(query.limit ?? 50, 200));
    let end = all.length;
    if (query.before !== undefined) {
      const m = /^mock-before-(\d+)$/.exec(query.before);
      const at = m ? Number(m[1]) : NaN;
      if (!Number.isInteger(at) || at < 0 || at > all.length) throw new CursorExpiredError();
      end = at;
    }
    const start = Math.max(0, end - limit);
    return {
      messages: all.slice(start, end),
      nextCursor: start > 0 ? `mock-before-${start}` : null,
      historyBoundary: null,
      title,
      runs,
    };
  }

  async listSessions(limit?: number): Promise<SessionSummary[]> {
    await delay();
    // Group the fixture work products by session (mirrors the handler's `_sessions`) so the demo
    // Sessions list lines up with the mock items, newest-active first.
    const bySession = new Map<string, SessionSummary>();
    for (const wp of [...this.all()].sort((a, b) => b.updated_at.localeCompare(a.updated_at))) {
      const existing = bySession.get(wp.session_id);
      if (existing) {
        existing.runs += 1;
        existing.description = `${wp.kind}: ${wp.tldr}`; // oldest-seen wins (originating ask)
      } else {
        bySession.set(wp.session_id, {
          session_id: wp.session_id,
          runs: 1,
          tokens_total: 0,
          last_activity: wp.updated_at,
          last_status: wp.status === "posted" ? "completed" : "running",
          latest_prompt: wp.tldr,
          target: wp.target_url,
          description: `${wp.kind}: ${wp.tldr}`,
        });
      }
    }
    const sessions = [...bySession.values()].map((s) => ({ ...s, title: this.titles.get(s.session_id) ?? null }));
    return limit ? sessions.slice(0, limit) : sessions;
  }

  async renameSession(sessionId: string, title: string): Promise<string | null> {
    await delay(80);
    const clean = cleanTitle(title);
    const path = `/api/sessions/${sessionId}`;
    if (clean.length > SESSION_TITLE_MAX_LEN) {
      throw new ApiError(`PATCH ${path} → 400: title must be at most ${SESSION_TITLE_MAX_LEN} characters`, 400);
    }
    if (clean) this.titles.set(sessionId, clean);
    else this.titles.delete(sessionId);
    return clean || null;
  }

  // --- Scheduled jobs -------------------------------------------------------------------------------

  private withRuns(job: Schedule): Schedule {
    return { ...clone(job), next_runs: job.status === "active" ? mockNextRuns(job.expression, Date.now(), 5, job.timezone, job.starts_at ?? job.created_at) : [] };
  }

  private findSchedule(id: string, method = "GET", path = `/api/schedules/${id}`): Schedule {
    const job = this.schedules.find((j) => j.id === id);
    if (!job) throw scheduleError(method, path, 404, "not_found", `no job '${id}'`);
    return job;
  }

  private writeSchedule(job: Schedule, next: Partial<Schedule>, action: string, expected?: number): Schedule {
    if (expected !== undefined && expected !== job.version)
      throw scheduleError("PUT", `/api/schedules/${job.id}`, 409, "conflict", `job '${job.id}' changed; reload and retry`);
    Object.assign(job, next, { version: job.version + 1, updated_by: MOCK_ACTOR, updated_at: new Date().toISOString() });
    job.enabled = job.status === "active";
    this.scheduleRevisions.get(job.id)?.unshift({ rev: job.version, action, actor: MOCK_ACTOR, at: job.updated_at, snapshot: snapshotOf(job) });
    return this.withRuns(job);
  }

  async listSchedules(): Promise<ScheduleList> {
    await delay();
    return {
      jobs: [...this.schedules].sort((a, b) => a.name.localeCompare(b.name)).map((j) => this.withRuns(j)),
      skills: [...MOCK_SCHEDULE_SKILLS],
      session_modes: ["daily", "single", "per-run"],
      limits: { min_interval_minutes: 60, max_prompt_chars: 20000 },
      now: new Date().toISOString(),
    };
  }

  async getSchedule(id: string): Promise<ScheduleDetail> {
    await delay(80);
    const job = this.schedules.find((j) => j.id === id);
    const revisions = clone(this.scheduleRevisions.get(id) ?? []);
    if (!job && revisions.length === 0) throw scheduleError("GET", `/api/schedules/${id}`, 404, "not_found", `no job '${id}'`);
    return { job: job ? this.withRuns(job) : null, revisions };
  }

  async createSchedule(spec: ScheduleSpec): Promise<Schedule> {
    await delay();
    const errors = [...mockScheduleErrors(spec, Date.now()), ...mockNotifyErrors(spec.notify)];
    if (errors.length) throw scheduleError("POST", "/api/schedules", 400, "invalid", errors[0]);
    const slug = spec.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) || "job";
    const now = new Date().toISOString();
    const job: Schedule = {
      id: `${slug}-${Math.random().toString(16).slice(2, 8)}`,
      name: spec.name.trim(),
      kind: spec.expression.startsWith("at(") ? "once" : "recurring",
      expression: spec.expression.trim(),
      timezone: spec.timezone || "UTC",
      prompt: spec.prompt,
      skill: spec.skill ?? null,
      session_mode: spec.session_mode ?? "daily",
      notify: spec.notify ?? null,
      status: spec.enabled === false ? "paused" : "active",
      enabled: spec.enabled !== false,
      starts_at: null,
      expires_at: spec.expires_at ?? null,
      version: 1,
      created_by: MOCK_ACTOR,
      updated_by: MOCK_ACTOR,
      created_at: now,
      updated_at: now,
      last_run: null,
      sync_state: "synced",
      sync_error: null,
      next_runs: [],
    };
    this.schedules.push(job);
    this.scheduleRevisions.set(job.id, [{ rev: 1, action: "create", actor: MOCK_ACTOR, at: now, snapshot: snapshotOf(job) }]);
    return this.withRuns(job);
  }

  async updateSchedule(id: string, changes: ScheduleChanges, expectedVersion: number): Promise<Schedule> {
    await delay();
    const job = this.findSchedule(id, "PUT");
    const merged = { ...job, ...changes } as Schedule;
    const errors = [
      ...mockScheduleErrors({ ...merged, skill: merged.skill, notify: merged.notify }, Date.now(), job),
      ...mockNotifyErrors(merged.notify, job),
    ];
    if (errors.length) throw scheduleError("PUT", `/api/schedules/${id}`, 400, "invalid", errors[0]);
    const kind = merged.expression.startsWith("at(") ? "once" : "recurring";
    return this.writeSchedule(job, { ...changes, kind } as Partial<Schedule>, "update", expectedVersion);
  }

  async pauseSchedule(id: string, expectedVersion?: number): Promise<Schedule> {
    await delay(80);
    const job = this.findSchedule(id, "POST");
    return job.status === "paused" ? this.withRuns(job) : this.writeSchedule(job, { status: "paused" }, "pause", expectedVersion);
  }

  async resumeSchedule(id: string, expectedVersion?: number): Promise<Schedule> {
    await delay(80);
    const job = this.findSchedule(id, "POST");
    const errors = mockScheduleErrors(job, Date.now());
    if (errors.length) throw scheduleError("POST", `/api/schedules/${id}/resume`, 400, "invalid", errors[0]);
    return job.status === "active" ? this.withRuns(job) : this.writeSchedule(job, { status: "active" }, "resume", expectedVersion);
  }

  async runSchedule(id: string): Promise<ScheduleRunOutcome> {
    await delay();
    const job = this.findSchedule(id, "POST");
    const now = new Date();
    const day = now.toISOString().slice(0, 10).replace(/-/g, "");
    const session = job.session_mode === "single" ? `sched-${id}` : `sched-${id}-${day}`;
    job.last_run = { time: now.toISOString(), status: "dispatched", session_id: session, trigger: "manual" };
    return { status: "dispatched", session_id: session, task_id: `mock-${Date.now().toString(36)}` };
  }

  async revertSchedule(id: string, rev: number, expectedVersion?: number): Promise<Schedule> {
    await delay();
    const revision = (this.scheduleRevisions.get(id) ?? []).find((r) => r.rev === rev);
    if (!revision) throw scheduleError("POST", `/api/schedules/${id}/revert`, 404, "not_found", `job '${id}' has no revision ${rev}`);
    const snap = revision.snapshot as Partial<Schedule>;
    const job = this.schedules.find((j) => j.id === id);
    if (!job) {
      // A deleted job: re-create it under its id, as the next revision of its history.
      const history = this.scheduleRevisions.get(id) ?? [];
      const created = await this.createSchedule({ ...(snap as ScheduleSpec), enabled: snap.status !== "paused" });
      this.scheduleRevisions.delete(created.id);
      const restored = this.findSchedule(created.id);
      Object.assign(restored, { id, version: (history[0]?.rev ?? 0) + 1 });
      history.unshift({ rev: restored.version, action: "create", actor: MOCK_ACTOR, at: restored.created_at, snapshot: snapshotOf(restored) });
      return this.withRuns(restored);
    }
    const { name, expression, timezone, prompt, skill, session_mode, notify } = snap;
    return this.writeSchedule(job, { name, expression, timezone, prompt, skill, session_mode, notify } as Partial<Schedule>, "revert", expectedVersion);
  }

  async deleteSchedule(id: string, expectedVersion?: number): Promise<void> {
    await delay(80);
    const job = this.findSchedule(id, "DELETE");
    if (expectedVersion !== undefined && expectedVersion !== job.version)
      throw scheduleError("DELETE", `/api/schedules/${id}`, 409, "conflict", `job '${id}' changed; reload and retry`);
    this.schedules = this.schedules.filter((j) => j.id !== id);
    this.scheduleRevisions.get(id)?.unshift({ rev: job.version + 1, action: "delete", actor: MOCK_ACTOR, at: new Date().toISOString(), snapshot: snapshotOf(job) });
  }

  async previewSchedule(spec: ScheduleSpec & { id?: string }): Promise<SchedulePreview> {
    await delay(120);
    const now = Date.now();
    const original = spec.id ? this.findSchedule(spec.id, "POST") : undefined;
    const errors = [...mockScheduleErrors(spec, now, original), ...mockNotifyErrors(spec.notify, original)];
    if (errors.length) return { ok: false, errors, kind: null, next_runs: [] };
    const active = original ? original.status === "active" : spec.enabled !== false;
    return {
      ok: true, errors: [], kind: spec.expression.startsWith("at(") ? "once" : "recurring",
      next_runs: active ? mockNextRuns(spec.expression, now, 5, spec.timezone, original?.starts_at ?? original?.created_at) : [],
    };
  }

  async listScheduleRuns(id: string, opts: { days?: number; limit?: number } = {}): Promise<ScheduleRuns> {
    await delay(120);
    const job = this.schedules.find((j) => j.id === id);
    const runs = job?.last_run
      ? [
          {
            task_id: `mock-${id}-1`,
            session_id: job.last_run.session_id,
            status: job.last_run.status === "busy" ? "cancelled" : "completed",
            started_at: job.last_run.time,
            ended_at: new Date(Date.parse(job.last_run.time) + 4 * 60_000).toISOString(),
            duration_ms: 240_000,
            trigger_source: `scheduled:${id}${job.last_run.trigger === "manual" ? ":manual" : ""}`,
          },
        ]
      : [];
    return { runs: runs.slice(0, opts.limit ?? 50), days: opts.days ?? 30, scanned: runs.length, complete: true };
  }
}

function snapshotOf(job: Schedule): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const k of SCHEDULE_DEFINITION) if (job[k] !== null && job[k] !== undefined) out[k] = clone(job[k]);
  return out;
}
