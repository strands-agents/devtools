/**
 * Live API client — implements PortalApi against the dashboard HTTP API (dashboard/api/handler.py).
 *
 * Auth: the ONLY bearer this client attaches is the dashboard's Cognito/JWT token (currently a
 * stub that holds nothing — real PKCE is a follow-up). It does NOT — and must not — import the
 * GitHub token module (src/github/token.ts): the user's GitHub token never rides on a dashboard-API
 * request. GitHub reads/writes go through src/github/client.ts (browser → api.github.com) instead.
 */
import type { WorkProduct, WorkProductKind } from "../types/workProduct";
import type {
  ChatLaunch,
  ChatPoll,
  PortalApi,
  Schedule,
  ScheduleChanges,
  ScheduleDetail,
  ScheduleList,
  SchedulePreview,
  ScheduleRunOutcome,
  ScheduleRuns,
  ScheduleSpec,
  SessionHistoryQuery,
  SessionSnapshot,
  SessionSummary,
  StartChatOptions,
} from "./types";
import { getDashboardToken, isDashboardAuthEnabled } from "../auth/dashboardToken";
import { sessionDetailActiveRun, sessionDetailRuns, sessionDetailToMessages, type SessionDetail } from "../lib/chat";
import { ApiError, CursorExpiredError, errorCode, errorText } from "./errors";
import { personHeaders } from "../lib/person";

function qs(params: Record<string, string | number | undefined>): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

/**
 * Rows asked for by the per-item "newest draft" lookups (getReviewForSession / getTriageForSession).
 * `/api/workproducts?session_id=` is newest-first (handler.py `by_session`, ScanIndexForward=False),
 * and the newest draft of a kind sits within the latest few runs' artifacts — the handler's default
 * of 50 full work products (each carrying findings + drafted text) is far more than those cards need.
 */
const WORK_PRODUCT_LOOKUP_LIMIT = 20;

export class LiveApi implements PortalApi {
  constructor(private readonly apiBase: string) {}

  private async req<T>(path: string, init?: RequestInit): Promise<T> {
    // The advisory "who are you" login (src/lib/person.ts) rides on every request.
    const headers: Record<string, string> = { "content-type": "application/json", ...personHeaders() };
    // Dashboard bearer only (Cognito/JWT). Never the GitHub token — see module header.
    const token = getDashboardToken();
    // When Cognito is configured but no token is held, the JWT authorizer would 401 every call.
    // Refuse before the fetch so unauthenticated views prompt sign-in instead of hammering the API.
    if (isDashboardAuthEnabled() && !token) {
      throw new Error("Sign in to the portal to load this data.");
    }
    if (token) headers.authorization = `Bearer ${token}`;
    const r = await fetch(this.apiBase + path, { ...init, headers: { ...headers, ...(init?.headers ?? {}) } });
    if (!r.ok) {
      const body = await r.text().catch(() => "");
      const code = errorCode(body);
      const message = `${init?.method ?? "GET"} ${path} → ${r.status}: ${body.slice(0, 300)}`;
      if (r.status === 409 && code === "cursor_expired") throw new CursorExpiredError();
      throw new ApiError(message, r.status, code, errorText(body));
    }
    return (await r.json()) as T;
  }

  /** GET /api/workproducts filtered to one session (newest-first), bounded by `limit`. */
  private async listWorkProducts(q: { session_id: string; kind?: WorkProductKind; limit: number }): Promise<WorkProduct[]> {
    const res = await this.req<{ items: WorkProduct[] }>(
      `/api/workproducts${qs({ session_id: q.session_id, kind: q.kind, limit: q.limit })}`,
    );
    return res.items ?? [];
  }

  async getReviewForSession(sessionId: string): Promise<WorkProduct | null> {
    // Newest draft review for this PR's session, if any. The dashboard API filters by
    // session_id + kind; we pick the newest `draft` client-side (a session can also hold
    // curated/posted reviews from earlier turns, which are not what the console opens on).
    const items = await this.listWorkProducts({ session_id: sessionId, kind: "review", limit: WORK_PRODUCT_LOOKUP_LIMIT });
    const drafts = items.filter((w) => w.status === "draft");
    drafts.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    return drafts[0] ?? null;
  }

  async getTriageForSession(sessionId: string): Promise<WorkProduct | null> {
    // Newest draft triage for this item's session, preferring a full `triage` over a
    // `first-response` when both exist. Two kinds qualify, so the kind filter is client-side
    // (the API's `kind` param takes one value).
    const items = await this.listWorkProducts({ session_id: sessionId, limit: WORK_PRODUCT_LOOKUP_LIMIT });
    const drafts = items
      .filter((w) => w.status === "draft" && (w.kind === "triage" || w.kind === "first-response"))
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    return drafts.find((w) => w.kind === "triage") ?? drafts[0] ?? null;
  }

  // --- Chat with the agent (talk to the agent that did the task) --------------------------------
  // These reach the deployed runtime via POST/GET /api/chat (launch + since-cursor poll). They carry
  // `trigger_source: "portal:chat"` and NEVER `mode: "post"` — this is a conversation with the
  // agent, not a publish handshake (the agent posts to GitHub only through its own tools).

  startChat(sessionId: string, message: string, opts: StartChatOptions = {}): Promise<ChatLaunch> {
    return this.req<ChatLaunch>(`/api/chat`, {
      method: "POST",
      body: JSON.stringify({
        session_id: sessionId,
        message,
        trigger_source: opts.triggerSource ?? "portal:chat",
        // Ambient UI state (what the user is viewing) — forwarded to the runtime as the invoke
        // payload's `invocationContext` and injected per turn. Omitted when the view has none.
        ...(opts.invocationContext ? { invocation_context: opts.invocationContext } : {}),
        // Passed through: the runtime's harness model resolution decides (a 400 with its reason).
        ...(opts.model ? { model: opts.model } : {}),
        ...(opts.effort ? { effort: opts.effort } : {}),
      }),
    });
  }

  pollChat(taskId: string, sessionId: string, since?: number): Promise<ChatPoll> {
    return this.req<ChatPoll>(`/api/chat${qs({ task_id: taskId, session_id: sessionId, since })}`);
  }

  async cancelChat(sessionId: string): Promise<{ status: string }> {
    return this.req<{ status: string }>(`/api/chat`, {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, action: "cancel" }),
    });
  }

  async loadSession(sessionId: string, query?: SessionHistoryQuery): Promise<SessionSnapshot> {
    // GET /api/sessions/{id} returns the verbatim conversation (from AgentCore Memory when wired,
    // else the ledger reconstruction) AND the ledger `turns` either way — which is where a run
    // still in flight shows up (`status: "running"` + its task_id). Surfacing it here is what lets
    // AgentChat attach to a run it did not launch.
    //
    // With `query` (`?limit[&before]`, see dashboard/README.md "Opt-in session transcript
    // pagination") the response is ONE page — the newest `limit` messages, or the page before an
    // opaque cursor — plus `next_cursor` / `history_boundary`; `turns` is then metadata only (still
    // enough for the active-run attach). Without it the full transcript comes back (strandly-mcp
    // still reads that shape).
    //
    // A 404 on the FIRST page means the session hasn't run yet — start fresh. A 404 (or anything
    // else) on an OLDER page is a real failure: returning `[]` there would silently erase history.
    try {
      const detail = await this.req<SessionDetail>(
        `/api/sessions/${encodeURIComponent(sessionId)}${qs({ limit: query?.limit, before: query?.before })}`,
      );
      return {
        messages: sessionDetailToMessages(detail),
        activeTaskId: sessionDetailActiveRun(detail)?.taskId,
        nextCursor: detail.next_cursor ?? null,
        historyBoundary: detail.history_boundary ?? null,
        title: detail.title ?? null,
        runs: sessionDetailRuns(detail),
      };
    } catch (e) {
      if (e instanceof ApiError && e.status === 404 && !query?.before) return { messages: [], nextCursor: null };
      throw e;
    }
  }

  async listSessions(limit?: number): Promise<SessionSummary[]> {
    // GET /api/sessions serves a fixed recent window grouped into sessions, newest-active-first
    // (handler.py `_sessions` over SESSIONS_WINDOW = 200 ledger rows; the route takes no `limit`
    // or cursor, so there is nothing to bound server-side — the response is already ≤200 compact
    // summaries). `limit` clips client-side; the Sessions page windows what it RENDERS.
    const res = await this.req<{ sessions: SessionSummary[] }>(`/api/sessions`);
    const sessions = res.sessions ?? [];
    return limit ? sessions.slice(0, limit) : sessions;
  }

  // --- Scheduled jobs (/api/schedules*) -----------------------------------------------------------

  async renameSession(sessionId: string, title: string): Promise<string | null> {
    const res = await this.req<{ title: string | null }>(`/api/sessions/${encodeURIComponent(sessionId)}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    });
    return res.title ?? null;
  }

  private schedulePath(id: string, action?: string): string {
    return `/api/schedules/${encodeURIComponent(id)}${action ? `/${action}` : ""}`;
  }

  private async scheduleAction(id: string, action: string, body: Record<string, unknown> = {}): Promise<Schedule> {
    const res = await this.req<{ job: Schedule }>(this.schedulePath(id, action), {
      method: "POST",
      body: JSON.stringify(body),
    });
    return res.job;
  }

  listSchedules(): Promise<ScheduleList> {
    return this.req<ScheduleList>(`/api/schedules`);
  }

  getSchedule(id: string): Promise<ScheduleDetail> {
    return this.req<ScheduleDetail>(this.schedulePath(id));
  }

  async createSchedule(spec: ScheduleSpec): Promise<Schedule> {
    const res = await this.req<{ job: Schedule }>(`/api/schedules`, { method: "POST", body: JSON.stringify(spec) });
    return res.job;
  }

  async updateSchedule(id: string, changes: ScheduleChanges, expectedVersion: number): Promise<Schedule> {
    const res = await this.req<{ job: Schedule }>(this.schedulePath(id), {
      method: "PUT",
      body: JSON.stringify({ ...changes, expected_version: expectedVersion }),
    });
    return res.job;
  }

  pauseSchedule(id: string, expectedVersion?: number): Promise<Schedule> {
    return this.scheduleAction(id, "pause", { expected_version: expectedVersion });
  }

  resumeSchedule(id: string, expectedVersion?: number): Promise<Schedule> {
    return this.scheduleAction(id, "resume", { expected_version: expectedVersion });
  }

  async runSchedule(id: string): Promise<ScheduleRunOutcome> {
    const res = await this.req<{ run: ScheduleRunOutcome }>(this.schedulePath(id, "run"), {
      method: "POST",
      body: "{}",
    });
    return res.run;
  }

  revertSchedule(id: string, rev: number, expectedVersion?: number): Promise<Schedule> {
    return this.scheduleAction(id, "revert", { rev, expected_version: expectedVersion });
  }

  async deleteSchedule(id: string, expectedVersion?: number): Promise<void> {
    await this.req<{ deleted: string }>(`${this.schedulePath(id)}${qs({ expected_version: expectedVersion })}`, {
      method: "DELETE",
    });
  }

  previewSchedule(spec: ScheduleSpec & { id?: string }): Promise<SchedulePreview> {
    return this.req<SchedulePreview>(`/api/schedules/preview`, { method: "POST", body: JSON.stringify(spec) });
  }

  listScheduleRuns(id: string, opts: { days?: number; limit?: number } = {}): Promise<ScheduleRuns> {
    return this.req<ScheduleRuns>(`${this.schedulePath(id, "runs")}${qs({ days: opts.days, limit: opts.limit })}`);
  }
}
