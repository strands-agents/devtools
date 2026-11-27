import type { WorkProduct } from "../types/workProduct";

/**
 * One normalized streamed chat event, mirroring the runtime's `HarnessEvent` as the poll route
 * relays it in `events[]` (see dashboard/api/handler.py `_chat`). `kind` drives rendering; the payload varies by kind:
 *   - "turn_start"  — `data.reason` names the boundary ("start" | "resume" | …)
 *   - "text"        — `text` is a streamed delta of the agent's reply
 *   - "tool_start"  — `tool` name + `toolUseId`; `data.input` is the (possibly partial) arguments
 *   - "tool_input"  — the call's COMPLETE arguments, keyed by the same `toolUseId`
 *   - "tool_result" — `data.status` + `data.content` (a list of `{text}` blocks)
 *   - "retry"       — a transient model error; the turn is being re-run
 */
export interface ChatEvent {
  kind: string;
  text?: string;
  tool?: string;
  toolUseId?: string;
  data?: {
    reason?: string;
    input?: string;
    status?: string;
    content?: { text?: string }[];
    /** tool_stream payload: the nested event's own kind ("tool_start" | "tool_result" | "text"). */
    kind?: string;
    /** tool_stream payload: the SUBAGENT's tool name (the outer `tool` is the streaming parent's). */
    tool?: string;
    /** tool_stream payload: a narration delta from the subagent. */
    text?: string;
    [k: string]: unknown;
  };
}

/** POST /api/chat launch result — a fire-and-forget turn on the session. */
export interface ChatLaunch {
  taskId: string;
  // "starting": sent, but the session is cold-starting — no task id yet (the run appears shortly).
  // A run already going on the session is superseded by this one (it ends "cancelled").
  status: string; // "accepted" | "rejected" | "starting"
  reason?: string;
  error?: string;
}

/**
 * GET /api/chat poll result, with the since-cursor streaming fields. `events` are the run's
 * buffered events at/after the cursor we sent; `nextSeq` is the cursor to send next. `result` /
 * `lastTurnResult` carry the finished turn's text on a terminal poll.
 */
export interface ChatPoll {
  // "cancelled": stopped, or superseded by a newer message on the session.
  status: string; // "running" | "completed" | "cancelled" | "failed" | "unknown"
  events: ChatEvent[];
  nextSeq?: number;
  result?: string;
  lastTurnResult?: string | null;
  error?: string;
}

/** The GitHub item a portal view has on screen — the `viewing` half of `InvocationContext`. */
export interface ViewingRef {
  type: "issue" | "pr" | "discussion";
  /** "owner/repo" */
  repo: string;
  number: number;
  url?: string;
}

/**
 * Ambient UI state sent with a chat turn — what the user is CURRENTLY viewing (the issue/PR of the
 * triage/review pane), so "what do you think about this one?" reaches the agent without a pasted
 * URL. Forwarded by /api/chat as the invoke payload's `invocationContext`; the runtime injects it
 * per turn as a clearly-labeled block. Purely additive: nothing is gated on it, and chat works identically
 * without it.
 */
export interface InvocationContext {
  /** The calling application, e.g. "maintainer-portal". */
  surface: string;
  /** The view within it, e.g. "triage" | "review" | "chat". */
  view?: string;
  viewing?: ViewingRef;
  /**
   * Who is talking — the signed-in maintainer's GitHub identity (the `viewer` of their browser
   * token, cached in auth.tsx), so the agent knows whose "I"/"me" it is hearing. Stamped centrally
   * by AgentChat at send time; anonymous (no token) turns simply omit it.
   */
  user?: { login: string; name?: string };
  /**
   * The user's standing "Strandly context" from Settings — free text about who they are / how they
   * want answers (see lib/userContext). Rendered by the runtime inside the same labeled ambient
   * block; clamped client-side to USER_CONTEXT_MAX_CHARS.
   */
  userContext?: string;
}

/** Options for launching a chat turn from the portal. */
export interface StartChatOptions {
  /** Provenance recorded on the run ledger. Defaults to "portal:chat". Never "post" (that is the publish path). */
  triggerSource?: string;
  /** Ambient UI state (what the user is viewing) to ride along with this turn. */
  invocationContext?: InvocationContext;
  /** This turn's harness model string, passed through; omitted runs the deployment's default. */
  model?: string;
  /** This turn's harness effort, passed through; omitted runs the model's default effort. */
  effort?: string;
}

/** One run of a session as the ledger recorded it: which model and effort answered. */
export interface SessionRun {
  taskId: string;
  startedAt?: string;
  status?: string;
  model?: string;
  effort?: string;
}

/**
 * One nested line of a streaming tool's subagent activity (a `tool_stream` event's rendering):
 * the child's tool calls/results and a rolling tail of its narration, shown indented under the
 * parent call — proof the call is alive and what it is doing.
 */
export interface SubActivityLine {
  kind: "tool" | "result" | "text";
  text: string;
  failed?: boolean;
}

/**
 * One ordered piece of an agent message: a stretch of text, a tool call (name + input; expandable
 * to the full arguments, with a streaming tool's subagent progress nested under it), or a tool
 * result (status + output). A message's blocks are in the order they streamed — text, the tool
 * calls that followed it, their results, more text — so the panel shows the work as it happened
 * instead of every tool first and all the text merged after.
 */
export type ChatBlock =
  /** `failed`: this block is an error caption (a run's failure), styled as such on its own. */
  | { kind: "text"; text: string; failed?: boolean }
  | { kind: "tool"; id?: string; name: string; input?: string; sub?: SubActivityLine[] }
  /** `id`: the call's tool-use id when known — the ONLY thing that pairs a result with its call. */
  | { kind: "result"; id?: string; status: string; text: string }
  /**
   * Text the agent added to a turn (an SDK `injected` block such as the caller context or the
   * environment, or a summary of earlier history), never the user's or the model's words. `source`
   * names what added it. Shown as a collapsed chip.
   */
  | { kind: "context"; source: string; text: string };

/** One rendered message in a chat transcript — loaded from history, or built live from a stream. */
export interface ChatMessage {
  role: "user" | "agent";
  /** The message's whole text (every text block joined) — what a plain bubble renders. */
  text: string;
  /** Ordered content (text + tool activity). When present it is what renders; `text` alone otherwise. */
  blocks?: ChatBlock[];
  timestamp?: string;
  /** True while the agent's reply is still streaming into this bubble. */
  streaming?: boolean;
  /** True when the turn ended in an error (rendered as an error bubble). */
  failed?: boolean;
  /** A small caption under the bubble — what happened to THIS message (not sent / joined mid-run / stopped). */
  note?: string;
  /** Renders `note` in the error tone (a send that failed). */
  noteFailed?: boolean;
  /** Panel-local identity so a caption can be updated after an async launch resolves. */
  id?: number;
  /**
   * The server's stable message id (paginated GET /api/sessions/{id}) — what an older page is
   * deduplicated against when it is prepended. Absent on live-streamed bubbles and legacy loads.
   */
  historyId?: string;
  /**
   * Every server id this row stands for when it was folded from several stored messages (an agent
   * turn's calls + results + follow-up — see chatView `foldToolTurns`). Page dedup checks all of them.
   */
  historyIds?: string[];
}

/**
 * GET /api/sessions/{id} distilled for the chat panel: the rendered transcript plus the session's
 * in-flight run when the ledger shows one (see lib/chat `sessionDetailActiveRun`). `activeTaskId`
 * is what lets the panel ATTACH to a run it did not launch — a mid-run reload, a colleague's
 * shared link, a mention-triggered run — and stream it like its own.
 */
export interface SessionSnapshot {
  messages: ChatMessage[];
  activeTaskId?: string;
  /**
   * Opaque cursor for the page BEFORE `messages` (`loadSession(id, { before })`), or `null` at the
   * beginning of the accessible history. Legacy (unpaginated) reads are the whole transcript → null.
   */
  nextCursor: string | null;
  /**
   * Why the accessible history ends where it does, when it is not the real beginning:
   * `memory_event_limit` (the Memory scan hit its event ceiling) or `ledger_window` (the ledger
   * fallback only covers the recent global window). Null/undefined when the history is complete.
   */
  historyBoundary?: HistoryBoundary | null;
  /** The session's human-readable title (set in the portal or by the agent), null when it has none. */
  title?: string | null;
  /** The session's recent runs, oldest first, with the model each one recorded. */
  runs?: SessionRun[];
}

export type HistoryBoundary = "memory_event_limit" | "ledger_window";

/** How much of a transcript to load (paginated GET /api/sessions/{id}?limit&before). */
export interface SessionHistoryQuery {
  /** Newest-N page size, 1–200 (server default 50). */
  limit?: number;
  /** Load the page BEFORE this opaque cursor (`SessionSnapshot.nextCursor`). */
  before?: string;
}

/**
 * One grouped session from GET /api/sessions (see dashboard/api/handler.py `_sessions`): a recent
 * window of runs collapsed by `session_id`, newest-active first. `description` is the session's
 * ORIGINATING prompt (oldest run in the window); `target` is the GitHub item URL when the run
 * carried one. Used by the Sessions list + the "talk to Strandly" picker so a session id never has
 * to be hand-typed.
 */
export interface SessionSummary {
  session_id: string;
  runs: number;
  tokens_total: number;
  last_activity?: string | null;
  last_status?: string | null;
  latest_prompt?: string | null;
  target?: string | null;
  description?: string | null;
  /** Human-readable title (renamed in the portal or set by the agent's tool); null when untitled. */
  title?: string | null;
}

// --- Scheduled jobs (/api/schedules*, ops/lambdas/schedules_api) ----------------------------------

export type ScheduleKind = "once" | "recurring";
export type ScheduleStatus = "active" | "paused" | "completed" | "expired";
export type SessionMode = "daily" | "single" | "per-run";
export type SyncState = "synced" | "pending" | "unsynced";

/** Immutable requester identity plus the interface surface used for limits and audit. */
export interface ScheduleActor {
  actor: string;
  surface: string;
  /** Verified Cognito issuer for portal subjects; absent on legacy audit records. */
  issuer?: string;
}

/** Where a run posts its result: a Slack channel (optionally a thread) or a GitHub thread. */
export interface NotifyTarget {
  kind: "slack" | "github";
  channel?: string;
  thread_ts?: string;
  url?: string;
}

export interface ScheduleLastRun {
  time: string;
  /** dispatched | busy | maybe-dispatched | error */
  status: string;
  session_id: string;
  /** schedule | manual | late */
  trigger: string;
}

/** One scheduled job. `expression` is `at(...)` (once), `cron(...)` or `rate(...)` (recurring). */
export interface Schedule {
  id: string;
  name: string;
  kind: ScheduleKind;
  expression: string;
  timezone: string;
  prompt: string;
  skill: string | null;
  session_mode: SessionMode;
  notify: NotifyTarget | null;
  status: ScheduleStatus;
  enabled: boolean;
  starts_at: string | null;
  expires_at: string | null;
  /** Optimistic-concurrency token: send it back on every write. */
  version: number;
  created_by: ScheduleActor;
  updated_by: ScheduleActor;
  created_at: string;
  updated_at: string;
  last_run: ScheduleLastRun | null;
  manual_run_at?: string | null;
  /** Whether EventBridge Scheduler matches the row (`unsynced`: the reconcile tick will retry). */
  sync_state: SyncState;
  sync_error: string | null;
  /** The next fire times (UTC ISO), empty unless active. */
  next_runs: string[];
}

export interface ScheduleLimits {
  min_interval_minutes: number;
  max_prompt_chars: number;
}

/** GET /api/schedules: the jobs plus what the create/edit form needs. */
export interface ScheduleList {
  jobs: Schedule[];
  skills: string[];
  session_modes: SessionMode[];
  limits: ScheduleLimits;
  now: string;
}

export interface ScheduleRevision {
  rev: number;
  /** create | update | pause | resume | revert | delete | ... */
  action: string;
  actor: ScheduleActor;
  at: string;
  /** The job definition after this change. */
  snapshot: Record<string, unknown>;
  /** Set when a job's own scheduled run made this change. */
  via?: { kind: string; job_id: string; session_id?: string; trigger?: string };
}

/** GET /api/schedules/{id}: `job` is null for a deleted job (its history stays). */
export interface ScheduleDetail {
  job: Schedule | null;
  revisions: ScheduleRevision[];
}

/** What create / preview send. */
export interface ScheduleSpec {
  name: string;
  expression: string;
  prompt: string;
  timezone: string;
  skill?: string | null;
  session_mode?: SessionMode;
  notify?: NotifyTarget | null;
  expires_at?: string | null;
  enabled?: boolean;
}

/** An edit: only the fields that change (`null` clears skill / notify / expires_at). */
export type ScheduleChanges = Partial<Omit<ScheduleSpec, "enabled">>;

export interface SchedulePreview {
  ok: boolean;
  errors: string[];
  kind: ScheduleKind | null;
  next_runs: string[];
}

export interface ScheduleRunOutcome {
  /** dispatched | busy | maybe-dispatched | error */
  status: string;
  session_id: string;
  task_id?: string;
  detail?: string;
}

/** One past run: a run-ledger row in one of the job's sessions. */
export interface ScheduleRun {
  task_id: string;
  session_id: string;
  status: string;
  started_at: string;
  ended_at?: string;
  duration_ms?: number;
  trigger_source?: string;
  error?: string;
}

export interface ScheduleRuns {
  runs: ScheduleRun[];
  /** Days of ledger history searched. */
  days: number;
  scanned: number;
  /** False when older matching runs may exist beyond what was read. */
  complete: boolean;
}

/**
 * The one interface every view programs against. `live` (src/api/client.ts) and `mock`
 * (src/api/mock.ts) both implement it; src/api/index.ts picks one from RuntimeConfig.dataSource.
 */
export interface PortalApi {
  /**
   * The newest DRAFT review WorkProduct for a PR's session, or null when Strandly hasn't drafted one
   * (GET /api/workproducts?session_id=&kind=review&limit=20). The PR page's "Strandly's review" card.
   */
  getReviewForSession(sessionId: string): Promise<WorkProduct | null>;
  /**
   * The newest DRAFT triage-family WorkProduct (kind `triage`, preferred, else `first-response`)
   * for an issue's session, or null (GET /api/workproducts?session_id=&limit=20). The issue page's
   * "Strandly's take" card.
   */
  getTriageForSession(sessionId: string): Promise<WorkProduct | null>;

  /**
   * Talk to the agent that owns this session. Launches a fire-and-forget chat turn (POST /api/chat,
   * `trigger_source: "portal:chat"`) and returns the task id to poll. The agent runs against its own
   * memory at `sessionId`, so "re-check finding 3 against the new commit" reaches the same agent
   * that produced the work product.
   */
  startChat(sessionId: string, message: string, opts?: StartChatOptions): Promise<ChatLaunch>;
  /** Poll a launched chat turn with an incremental since-cursor (GET /api/chat). */
  pollChat(taskId: string, sessionId: string, since?: number): Promise<ChatPoll>;
  /** Stop the session's in-flight turn (POST /api/chat `action: "cancel"`). */
  cancelChat(sessionId: string): Promise<{ status: string }>;
  /**
   * The transcript PLUS the session's in-flight run (if any) in one read (GET /api/sessions/{id}) —
   * what AgentChat mounts on, so a session whose agent is mid-run attaches and streams instead of
   * freezing. With a `query` it returns ONE page — the newest `limit` messages, or the page before
   * an opaque cursor — plus `nextCursor` to walk back. Without one it is the legacy full response.
   * A missing/fresh session (404 on the first page) is `{ messages: [], nextCursor: null }`; an
   * older page whose cursor no longer matches history rejects with `CursorExpiredError`
   * (src/api/errors.ts) so the panel can offer a reload; other failures propagate.
   */
  loadSession(sessionId: string, query?: SessionHistoryQuery): Promise<SessionSnapshot>;
  /**
   * Recent sessions (GET /api/sessions), grouped + newest-active-first — the Strandly tab's list.
   * `limit` clips client-side (the route itself serves a fixed recent window, ≤200).
   */
  listSessions(limit?: number): Promise<SessionSummary[]>;
  /**
   * Set (or, with an empty string, clear) a session's title (PATCH /api/sessions/{id}). Resolves to
   * the title the server stored (normalized, or null when cleared); rejects with ApiError otherwise.
   */
  renameSession(sessionId: string, title: string): Promise<string | null>;

  // Scheduled jobs. Writes take the `version` the view last read and reject with ApiError 409
  // (`code: "conflict"`) when the job changed meanwhile; 400 (`code: "invalid"`) carries the reason.
  /** Every job with its next runs, plus the skills / session modes / limits the form offers. */
  listSchedules(): Promise<ScheduleList>;
  /** One job and its revisions (`job: null` when it was deleted). */
  getSchedule(id: string): Promise<ScheduleDetail>;
  createSchedule(spec: ScheduleSpec): Promise<Schedule>;
  updateSchedule(id: string, changes: ScheduleChanges, expectedVersion: number): Promise<Schedule>;
  pauseSchedule(id: string, expectedVersion?: number): Promise<Schedule>;
  resumeSchedule(id: string, expectedVersion?: number): Promise<Schedule>;
  /** Launch the job now (its session per its session mode). */
  runSchedule(id: string): Promise<ScheduleRunOutcome>;
  /** Restore revision `rev`'s definition (re-creates a deleted job). */
  revertSchedule(id: string, rev: number, expectedVersion?: number): Promise<Schedule>;
  deleteSchedule(id: string, expectedVersion?: number): Promise<void>;
  /** Validate a spec without saving: errors, or the next 5 runs. `id` previews an edit of that job. */
  previewSchedule(spec: ScheduleSpec & { id?: string }): Promise<SchedulePreview>;
  /** Past runs: the ledger rows of the job's sessions, newest first. */
  listScheduleRuns(id: string, opts?: { days?: number; limit?: number }): Promise<ScheduleRuns>;
}
