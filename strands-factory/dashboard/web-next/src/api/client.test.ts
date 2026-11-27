import { afterEach, describe, expect, it, vi } from "vitest";
import { LiveApi } from "./client";
import { ApiError, CursorExpiredError, errorCode } from "./errors";
import { MockApi, MOCK_TRANSCRIPT_LENGTH } from "./mock";
import { MOCK_WORK_PRODUCTS } from "./fixtures";
import { sessionDetailToMessages } from "../lib/chat";

vi.mock("../auth/dashboardToken", () => ({
  getDashboardToken: () => null,
  isDashboardAuthEnabled: () => false,
}));

afterEach(() => vi.unstubAllGlobals());

const api = new LiveApi("https://portal.test");
function respond(body: unknown, status = 200) {
  // A fresh Response per call: a body can only be read once.
  const fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(body), { status })));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}
const url = (fetch: ReturnType<typeof vi.fn>) => String(fetch.mock.calls[0][0]);

describe("loadSession — paginated transcript reads", () => {
  it("first page: ?limit only, newest messages, cursor + active run preserved", async () => {
    const fetch = respond({
      session_id: "session/name",
      runs: 1,
      source: "memory",
      next_cursor: "opaque+/=",
      history_boundary: null,
      messages: [{ id: "m9", role: "assistant", text: "latest reply", timestamp: "2026-01-01T00:00:00Z" }],
      turns: [{ task_id: "running-task", status: "running", started_at: new Date().toISOString() }],
    });
    const snap = await api.loadSession("session/name", { limit: 50 });
    expect(url(fetch)).toBe("https://portal.test/api/sessions/session%2Fname?limit=50");
    expect(snap).toMatchObject({
      messages: [{ role: "agent", text: "latest reply", historyId: "m9" }],
      activeTaskId: "running-task",
      nextCursor: "opaque+/=",
      historyBoundary: null,
    });
  });

  it("older page: limit + url-encoded opaque cursor; an empty page stays empty (turns are metadata only)", async () => {
    const fetch = respond({
      session_id: "s",
      runs: 1,
      messages: [],
      next_cursor: null,
      history_boundary: null,
      turns: [{ task_id: "t1", status: "completed" }],
    });
    const snap = await api.loadSession("s", { limit: 50, before: "opaque+/=" });
    expect(url(fetch)).toBe("https://portal.test/api/sessions/s?limit=50&before=opaque%2B%2F%3D");
    expect(snap).toEqual({
      messages: [],
      activeTaskId: undefined,
      nextCursor: null,
      historyBoundary: null,
      title: null,
      runs: [{ taskId: "t1", status: "completed", startedAt: undefined }],
    });
  });

  it("surfaces history_boundary and failed ledger replies", async () => {
    respond({
      session_id: "s",
      runs: 1,
      messages: [{ id: "a", role: "assistant", text: "run failed", failed: true }],
      next_cursor: null,
      history_boundary: "ledger_window",
    });
    expect(await api.loadSession("s", { limit: 50 })).toMatchObject({
      historyBoundary: "ledger_window",
      nextCursor: null,
      messages: [{ failed: true, historyId: "a", blocks: [{ kind: "text", text: "run failed", failed: true }] }],
    });
  });

  it("maps 409 cursor_expired to CursorExpiredError", async () => {
    respond({ error: "history changed or expired; reload the newest page", code: "cursor_expired" }, 409);
    const err = await api.loadSession("s", { limit: 50, before: "stale" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(CursorExpiredError);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as CursorExpiredError).status).toBe(409);
    expect((err as CursorExpiredError).code).toBe("cursor_expired");
    expect((err as Error).message).toBe("History changed — reload");
  });

  it("a 409 without the code is an ordinary ApiError", async () => {
    respond({ error: "conflict" }, 409);
    const err = await api.loadSession("s", { limit: 50, before: "c" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).not.toBeInstanceOf(CursorExpiredError);
  });

  it("legacy call (no query) hits the bare path and returns the whole transcript", async () => {
    const fetch = respond({ session_id: "s", runs: 1, turns: [{ prompt: "question", result: "answer" }] });
    expect((await api.loadSession("s")).messages.map((m) => m.text)).toEqual(["question", "answer"]);
    expect(url(fetch)).toBe("https://portal.test/api/sessions/s");
    expect((await api.loadSession("s")).nextCursor).toBeNull();
    // Unpaginated + empty messages still falls back to the ledger turns.
    expect(sessionDetailToMessages({ session_id: "s", runs: 1, messages: [], turns: [{ result: "fallback" }] })).toMatchObject([
      { text: "fallback" },
    ]);
  });

  it("only treats a FIRST-page 404 as a fresh conversation", async () => {
    respond({ error: "session not found" }, 404);
    expect(await api.loadSession("s", { limit: 50 })).toEqual({ messages: [], nextCursor: null });
    expect(await api.loadSession("s")).toEqual({ messages: [], nextCursor: null });
    await expect(api.loadSession("s", { limit: 50, before: "cursor" })).rejects.toThrow("404");
  });

  it.each([400, 401, 403, 429, 500, 503])("surfaces HTTP %i instead of erasing history", async (status) => {
    respond({ error: "x" }, status);
    const err = await api.loadSession("s", { limit: 50 }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(status);
  });

  it("surfaces network failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    await expect(api.loadSession("s", { limit: 50 })).rejects.toThrow("offline");
  });
});

describe("errorCode", () => {
  it("reads a string code and ignores anything else", () => {
    expect(errorCode('{"code":"cursor_expired"}')).toBe("cursor_expired");
    expect(errorCode('{"code":409}')).toBeUndefined();
    expect(errorCode("not json")).toBeUndefined();
    expect(errorCode("")).toBeUndefined();
  });
});

describe("MockApi.loadSession honors limit/before", () => {
  const mock = new MockApi();
  const known = MOCK_WORK_PRODUCTS[0].session_id;

  it("pages a known session newest-first with opaque cursors down to the beginning", async () => {
    const first = await mock.loadSession(known, { limit: 50 });
    expect(first.messages).toHaveLength(50);
    expect(first.nextCursor).not.toBeNull();
    expect(first.historyBoundary).toBeNull();
    const ids = new Set(first.messages.map((m) => m.historyId));
    expect(ids.size).toBe(50);

    const seen = [...first.messages];
    let cursor = first.nextCursor;
    while (cursor) {
      const page = await mock.loadSession(known, { limit: 50, before: cursor });
      for (const m of page.messages) expect(ids.has(m.historyId)).toBe(false);
      page.messages.forEach((m) => ids.add(m.historyId));
      seen.unshift(...page.messages);
      cursor = page.nextCursor;
    }
    expect(seen).toHaveLength(MOCK_TRANSCRIPT_LENGTH);
    // Oldest first within and across pages.
    const times = seen.map((m) => Date.parse(m.timestamp!));
    expect([...times].sort((a, b) => a - b)).toEqual(times);
  });

  it("clamps limit, rejects a bad cursor as CursorExpiredError, and keeps unknown sessions fresh", async () => {
    expect((await mock.loadSession(known, { limit: 0 })).messages).toHaveLength(1);
    expect((await mock.loadSession(known, { limit: 999 })).messages).toHaveLength(MOCK_TRANSCRIPT_LENGTH);
    await expect(mock.loadSession(known, { limit: 50, before: "garbage" })).rejects.toBeInstanceOf(CursorExpiredError);
    expect(await mock.loadSession("portal-nobody-20260101-abcdefgh", { limit: 50 })).toEqual({
      messages: [],
      nextCursor: null,
      historyBoundary: null,
      title: null,
      runs: [],
    });
    // Legacy (no query) → everything.
    expect((await mock.loadSession(known)).messages.length).toBe(MOCK_TRANSCRIPT_LENGTH);
  });
});

describe("bounded work-product lookups", () => {
  it("getReviewForSession / getTriageForSession ask for a small newest-first window", async () => {
    const fetch = respond({ items: [] });
    await api.getReviewForSession("gh-o-r-pr-1");
    await api.getTriageForSession("gh-o-r-issue-1");
    const urls = fetch.mock.calls.map((c) => String(c[0]));
    expect(urls[0]).toBe("https://portal.test/api/workproducts?session_id=gh-o-r-pr-1&kind=review&limit=20");
    expect(urls[1]).toBe("https://portal.test/api/workproducts?session_id=gh-o-r-issue-1&limit=20");
  });
});

describe("schedules — live client", () => {
  const job = { id: "j/1", name: "J", version: 4 };
  const init = (fetch: ReturnType<typeof vi.fn>, i = 0) => fetch.mock.calls[i][1] as RequestInit;

  it("lists and reads jobs", async () => {
    const fetch = respond({ jobs: [job], skills: ["s"], session_modes: ["daily"], limits: {}, now: "t" });
    expect((await api.listSchedules()).jobs).toEqual([job]);
    expect(url(fetch)).toBe("https://portal.test/api/schedules");
    const detail = respond({ job, revisions: [] });
    expect(await api.getSchedule("j/1")).toEqual({ job, revisions: [] });
    expect(url(detail)).toBe("https://portal.test/api/schedules/j%2F1");
  });

  it("creates, updates with the expected version, and runs actions", async () => {
    let fetch = respond({ job });
    await api.createSchedule({ name: "J", expression: "rate(1 day)", prompt: "p", timezone: "UTC" });
    expect(init(fetch).method).toBe("POST");
    expect(JSON.parse(String(init(fetch).body))).toMatchObject({ name: "J", expression: "rate(1 day)" });

    fetch = respond({ job });
    expect(await api.updateSchedule("j/1", { prompt: "q" }, 4)).toEqual(job);
    expect(init(fetch).method).toBe("PUT");
    expect(JSON.parse(String(init(fetch).body))).toEqual({ prompt: "q", expected_version: 4 });

    fetch = respond({ job });
    await api.pauseSchedule("j/1", 4);
    expect(url(fetch)).toBe("https://portal.test/api/schedules/j%2F1/pause");
    expect(JSON.parse(String(init(fetch).body))).toEqual({ expected_version: 4 });

    fetch = respond({ job });
    await api.revertSchedule("j/1", 2, 4);
    expect(url(fetch)).toBe("https://portal.test/api/schedules/j%2F1/revert");
    expect(JSON.parse(String(init(fetch).body))).toEqual({ rev: 2, expected_version: 4 });

    fetch = respond({ run: { status: "maybe-dispatched", session_id: "sched-j" } });
    expect(await api.runSchedule("j/1")).toEqual({ status: "maybe-dispatched", session_id: "sched-j" });
    expect(url(fetch)).toBe("https://portal.test/api/schedules/j%2F1/run");

    fetch = respond({ deleted: "j/1", rev: 5 });
    await api.deleteSchedule("j/1", 4);
    expect(url(fetch)).toBe("https://portal.test/api/schedules/j%2F1?expected_version=4");
    expect(init(fetch).method).toBe("DELETE");
  });

  it("previews and lists past runs", async () => {
    let fetch = respond({ ok: true, errors: [], kind: "recurring", next_runs: ["a"] });
    await api.previewSchedule({ id: "j", name: "J", expression: "rate(1 day)", prompt: "p", timezone: "UTC" });
    expect(url(fetch)).toBe("https://portal.test/api/schedules/preview");
    expect(JSON.parse(String(init(fetch).body)).id).toBe("j");
    fetch = respond({ runs: [], days: 7, scanned: 0, complete: true });
    await api.listScheduleRuns("j", { days: 7 });
    expect(url(fetch)).toBe("https://portal.test/api/schedules/j/runs?days=7");
  });

  it("surfaces a stale write as a 409 conflict", async () => {
    respond({ error: "changed", code: "conflict" }, 409);
    await expect(api.updateSchedule("j", { prompt: "q" }, 1)).rejects.toMatchObject({ status: 409, code: "conflict" });
  });
});

describe("schedules — mock", () => {
  it("creates, edits with a version check, pauses, runs, deletes and restores", async () => {
    const mock = new MockApi();
    const before = (await mock.listSchedules()).jobs.length;
    const job = await mock.createSchedule({ name: "Digest", expression: "rate(2 hours)", prompt: "p", timezone: "UTC" });
    expect(job.next_runs).toHaveLength(5);
    expect((await mock.listSchedules()).jobs).toHaveLength(before + 1);
    await expect(
      mock.createSchedule({ name: "Fast", expression: "rate(30 minutes)", prompt: "p", timezone: "UTC" }),
    ).rejects.toMatchObject({ status: 400, code: "invalid" });

    const edited = await mock.updateSchedule(job.id, { prompt: "q" }, job.version);
    expect(edited).toMatchObject({ prompt: "q", version: 2 });
    await expect(mock.updateSchedule(job.id, { prompt: "r" }, job.version)).rejects.toMatchObject({ status: 409 });

    const paused = await mock.pauseSchedule(job.id, edited.version);
    expect(paused).toMatchObject({ status: "paused", enabled: false, next_runs: [] });
    const run = await mock.runSchedule(job.id);
    expect(run.session_id.startsWith(`sched-${job.id}-`)).toBe(true);
    expect((await mock.listScheduleRuns(job.id)).runs).toHaveLength(1);

    await mock.deleteSchedule(job.id);
    const gone = await mock.getSchedule(job.id);
    expect(gone.job).toBeNull();
    expect(gone.revisions.map((r) => r.action)).toEqual(["delete", "pause", "update", "create"]);
    const restored = await mock.revertSchedule(job.id, 2);
    expect(restored).toMatchObject({ id: job.id, prompt: "q", version: 5 });
  });

  it("previews: errors or approximate next runs", async () => {
    const mock = new MockApi();
    expect((await mock.previewSchedule({ name: "x", expression: "cron(0 9 * * ? *)", prompt: "p", timezone: "UTC" })).next_runs).toHaveLength(5);
    const past = await mock.previewSchedule({ name: "x", expression: "at(2000-01-01T00:00:00)", prompt: "p", timezone: "UTC" });
    expect(past).toMatchObject({ ok: false, next_runs: [] });
  });
});

describe("session titles", () => {
  it("loadSession surfaces the title", async () => {
    respond({ session_id: "s", runs: 1, messages: [], next_cursor: null, title: "Release prep" });
    expect((await api.loadSession("s", { limit: 1 })).title).toBe("Release prep");
  });

  it("renameSession PATCHes the session and returns the stored title", async () => {
    const fetch = respond({ session_id: "a/b", title: "Release prep" });
    await expect(api.renameSession("a/b", "Release prep")).resolves.toBe("Release prep");
    expect(url(fetch)).toBe("https://portal.test/api/sessions/a%2Fb");
    const init = fetch.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(String(init.body))).toEqual({ title: "Release prep" });
  });

  it("renameSession clears to null and surfaces failures as ApiError", async () => {
    respond({ session_id: "s", title: null });
    await expect(api.renameSession("s", "")).resolves.toBeNull();
    respond({ error: "session not found" }, 404);
    await expect(api.renameSession("s", "x")).rejects.toMatchObject({ status: 404 });
  });

  it("the mock stores, lists, clears and bounds titles like the route", async () => {
    const mock = new MockApi();
    const [first] = await mock.listSessions();
    expect(first.title).toBeNull();
    await expect(mock.renameSession(first.session_id, "  Named\n session ")).resolves.toBe("Named session");
    expect((await mock.listSessions())[0].title).toBe("Named session");
    expect((await mock.loadSession(first.session_id, { limit: 1 })).title).toBe("Named session");
    await expect(mock.renameSession(first.session_id, "x".repeat(81))).rejects.toBeInstanceOf(ApiError);
    await expect(mock.renameSession(first.session_id, "")).resolves.toBeNull();
    expect((await mock.listSessions())[0].title).toBeNull();
  });
});
