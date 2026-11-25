import { describe, expect, it } from "vitest";
import type { Schedule, ScheduleRevision } from "../../api/types";
import {
  actorLabel,
  apiErrorMessage,
  canToggle,
  changesFromForm,
  defaultForm,
  describeSchedule,
  expressionFor,
  formatInZone,
  formErrors,
  formFromJob,
  presetFor,
  previewFromForm,
  relFuture,
  revisionChanges,
  revisionSource,
  runStatusColor,
  runStatusLabel,
  runTrigger,
  specFromForm,
  syncBadge,
  type ScheduleForm,
} from "./schedule";

const NOW = new Date("2030-01-01T12:00:00Z");
const form = (over: Partial<ScheduleForm> = {}): ScheduleForm => ({
  ...defaultForm(NOW, "UTC"),
  name: "Digest",
  prompt: "summarize",
  ...over,
});

const job = (over: Partial<Schedule> = {}): Schedule => ({
  id: "digest-abc123",
  name: "Digest",
  kind: "recurring",
  expression: "cron(0 9 * * ? *)",
  timezone: "Europe/Istanbul",
  prompt: "summarize",
  skill: null,
  session_mode: "daily",
  notify: null,
  status: "active",
  enabled: true,
  starts_at: null,
  expires_at: null,
  version: 3,
  created_by: { actor: "portal:alice@example.com", surface: "dashboard" },
  updated_by: { actor: "portal:alice@example.com", surface: "dashboard" },
  created_at: "2029-12-01T00:00:00Z",
  updated_at: "2029-12-02T00:00:00Z",
  last_run: null,
  sync_state: "synced",
  sync_error: null,
  next_runs: [],
  ...over,
});

describe("expressions from the form", () => {
  it("builds once / daily / weekly / every-N-hours / custom", () => {
    expect(expressionFor(form({ kind: "once", onceDate: "2030-01-02", onceTime: "15:30" }))).toBe("at(2030-01-02T15:30:00)");
    expect(expressionFor(form({ preset: "daily", time: "09:05" }))).toBe("cron(5 9 * * ? *)");
    expect(expressionFor(form({ preset: "weekly", time: "17:00", weekday: "FRI" }))).toBe("cron(0 17 ? * FRI *)");
    expect(expressionFor(form({ preset: "hours", everyHours: "1" }))).toBe("rate(1 hour)");
    expect(expressionFor(form({ preset: "hours", everyHours: "6" }))).toBe("rate(6 hours)");
    expect(expressionFor(form({ preset: "custom", custom: "  rate(30 minutes) " }))).toBe("rate(30 minutes)");
  });

  it("is empty while a needed part is missing", () => {
    expect(expressionFor(form({ kind: "once", onceDate: "" }))).toBe("");
    expect(expressionFor(form({ preset: "daily", time: "25:00" }))).toBe("");
    expect(expressionFor(form({ preset: "hours", everyHours: "1.5" }))).toBe("");
  });

  it("defaults to tomorrow 09:00 and daily at 09:00 in the given zone", () => {
    const f = defaultForm(NOW, "Pacific/Auckland");
    expect(f).toMatchObject({ kind: "recurring", preset: "daily", time: "09:00", onceTime: "09:00", timezone: "Pacific/Auckland" });
    expect(f.onceDate).toBe("2030-01-03"); // tomorrow in Auckland (UTC+13)
  });
});

describe("presets from an expression (editing)", () => {
  it("round-trips every preset", () => {
    for (const f of [
      form({ kind: "once", onceDate: "2030-02-03", onceTime: "08:15" }),
      form({ preset: "daily", time: "14:00" }),
      form({ preset: "weekly", weekday: "MON", time: "15:00" }),
      form({ preset: "hours", everyHours: "12" }),
    ]) {
      const expr = expressionFor(f);
      expect(expressionFor({ ...form(), ...presetFor(expr) })).toBe(expr);
    }
  });

  it("reads numeric cron days and leaves anything else to the raw box", () => {
    expect(presetFor("cron(0 15 ? * 2 *)")).toMatchObject({ preset: "weekly", weekday: "MON", time: "15:00" });
    expect(presetFor("cron(0 9 ? * MON-FRI *)")).toEqual({ kind: "recurring", preset: "custom", custom: "cron(0 9 ? * MON-FRI *)" });
    expect(presetFor("rate(1 day)")).toMatchObject({ preset: "custom" });
    expect(presetFor("cron(0/30 * * * ? *)")).toMatchObject({ preset: "custom" });
  });

  it("fills the form from a job", () => {
    const f = formFromJob(
      job({ expression: "cron(0 15 ? * MON *)", skill: "parity-sync", notify: { kind: "slack", channel: "C1234", thread_ts: "1.2" } }),
      NOW,
    );
    expect(f).toMatchObject({
      preset: "weekly",
      weekday: "MON",
      time: "15:00",
      skill: "parity-sync",
      timezone: "Europe/Istanbul",
      notifyKind: "slack",
      notifyChannel: "C1234",
      notifyThread: "1.2",
    });
  });
});

describe("request bodies", () => {
  it("create sends the spec with nulls for no skill / notify", () => {
    expect(specFromForm(form({ name: "  Digest  ", timezone: "" }))).toEqual({
      name: "Digest",
      expression: "cron(0 9 * * ? *)",
      prompt: "summarize",
      timezone: "UTC",
      skill: null,
      session_mode: "daily",
      notify: null,
      enabled: true,
    });
    expect(specFromForm(form({ notifyKind: "github", notifyUrl: " https://github.com/o/r/issues/1 " })).notify).toEqual({
      kind: "github",
      url: "https://github.com/o/r/issues/1",
    });
  });

  it("edit sends only what changed", () => {
    const j = job();
    expect(changesFromForm(j, formFromJob(j, NOW))).toEqual({});
    const edited = { ...formFromJob(j, NOW), prompt: "new", time: "10:00", notifyKind: "github" as const,
      notifyUrl: "https://github.com/o/r/issues/2" };
    expect(changesFromForm(j, edited)).toEqual({
      prompt: "new",
      expression: "cron(0 10 * * ? *)",
      notify: { kind: "github", url: "https://github.com/o/r/issues/2" },
    });
    // A kept home-channel target is no change; clearing it is.
    const home = job({ notify: { kind: "slack", channel: "C0HOME", thread_ts: "1.2" } });
    expect(changesFromForm(home, formFromJob(home, NOW))).toEqual({});
    expect(changesFromForm(home, { ...formFromJob(home, NOW), notifyKind: "" })).toEqual({ notify: null });
    // A stored DM target is never offered: the form shows the default, and saving clears it.
    const dm = job({ notify: { kind: "slack", channel: "D0ALICE", thread_ts: "1.2" } });
    expect(formFromJob(dm, NOW).notifyKind).toBe("");
    expect(changesFromForm(dm, formFromJob(dm, NOW))).toEqual({ notify: null });
    const cleared = changesFromForm(job({ skill: "parity-sync" }), { ...formFromJob(j, NOW), skill: "" });
    expect(cleared).toEqual({ skill: null });
  });
});

describe("form validation", () => {
  const limits = { min_interval_minutes: 60, max_prompt_chars: 10 };
  it("flags missing and malformed fields", () => {
    expect(formErrors(form(), limits)).toEqual({});
    expect(Object.keys(formErrors(form({ name: " ", prompt: "" })))).toEqual(["name", "prompt"]);
    expect(formErrors(form({ prompt: "x".repeat(11) }), limits).prompt).toMatch(/At most 10/);
    expect(formErrors(form({ preset: "custom", custom: "every day" })).custom).toBeTruthy();
    expect(formErrors(form({ preset: "hours", everyHours: "0" })).everyHours).toBeTruthy();
    expect(formErrors(form({ kind: "once", onceDate: "" })).onceDate).toBeTruthy();
    expect(formErrors(form({ notifyKind: "github", notifyUrl: "https://github.com/o/r" })).notifyUrl).toBeTruthy();
    expect(formErrors(form({ notifyKind: "github", notifyUrl: "https://github.com/o/r/pull/3" }))).toEqual({});
  });
});

describe("display", () => {
  it("describes schedules in words", () => {
    expect(describeSchedule("cron(0 14 * * ? *)", "UTC")).toBe("Daily at 14:00 (UTC)");
    expect(describeSchedule("cron(30 9 ? * MON *)", "Europe/Berlin")).toBe("Weekly on Monday at 09:30 (Europe/Berlin)");
    expect(describeSchedule("rate(1 hour)", "UTC")).toBe("Every hour");
    expect(describeSchedule("rate(6 hours)", "UTC")).toBe("Every 6 hours");
    expect(describeSchedule("rate(1 day)", "UTC")).toBe("Every day");
    expect(describeSchedule("rate(30 minutes)", "UTC")).toBe("Every 30 minutes");
    expect(describeSchedule("at(2030-01-02T09:00:00)", "America/New_York")).toBe("Once on Jan 2, 2030 at 09:00 (America/New_York)");
    expect(describeSchedule("cron(0 9 ? * MON-FRI *)", "UTC")).toBe("cron(0 9 ? * MON-FRI *) (UTC)");
  });

  it("formats times in the job's zone and relative to now", () => {
    expect(formatInZone("2030-01-07T14:00:00Z", "America/New_York")).toBe("Mon, Jan 7, 09:00");
    expect(formatInZone("2030-01-07T14:00:00Z", "Not/AZone")).toBe("Mon, Jan 7, 14:00");
    expect(formatInZone("garbage", "UTC")).toBe("garbage");
    const now = NOW.getTime();
    expect(relFuture("2030-01-01T12:00:20Z", now)).toBe("now");
    expect(relFuture("2030-01-01T12:45:00Z", now)).toBe("in 45m");
    expect(relFuture("2030-01-02T12:00:00Z", now)).toBe("in 24h");
    expect(relFuture("2030-01-05T12:00:00Z", now)).toBe("in 4d");
    expect(relFuture(null, now)).toBe("");
  });

  it("labels actors, statuses, triggers and sync state", () => {
    expect(actorLabel("portal:alice@example.com")).toBe("alice@example.com");
    expect(actorLabel("system:seed")).toBe("seed (system)");
    expect(actorLabel("slack:U1")).toBe("U1 (slack)");
    expect(runStatusColor("dispatched")).toBe("green");
    expect(runStatusColor("failed")).toBe("red");
    expect(runStatusLabel("maybe-dispatched")).toBe("acceptance unknown");
    expect(runStatusLabel("busy")).toBe("skipped (busy)");
    expect(runTrigger("scheduled:j")).toBe("schedule");
    expect(runTrigger("scheduled:j:manual")).toBe("manual");
    expect(runTrigger("portal:chat")).toBe("portal:chat");
    expect(syncBadge("synced")).toBeNull();
    expect(syncBadge("unsynced")).toEqual({ label: "unsynced", color: "red" });
    expect(canToggle(job({ status: "paused" }))).toBe(true);
    expect(canToggle(job({ status: "completed" }))).toBe(false);
  });

  it("lists what each revision changed", () => {
    const rev = (n: number, action: string, snapshot: Record<string, unknown>): ScheduleRevision => ({
      rev: n,
      action,
      actor: { actor: "portal:a", surface: "dashboard" },
      at: "2030-01-01T00:00:00Z",
      snapshot,
    });
    const base = { name: "n", expression: "rate(1 day)", prompt: "p", status: "active" };
    const changes = revisionChanges([
      rev(3, "pause", { ...base, prompt: "q", status: "paused" }),
      rev(2, "update", { ...base, prompt: "q" }),
      rev(1, "create", base),
    ]);
    expect(changes.get(3)).toEqual(["status"]);
    expect(changes.get(2)).toEqual(["prompt"]);
    expect(changes.get(1)).toEqual([]);
  });

  it("labels a revision a job's own scheduled run made", () => {
    const base = { rev: 2, action: "pause", actor: { actor: "slack:U1", surface: "agent" }, at: "2030-01-01T00:00:00Z", snapshot: {} };
    expect(revisionSource(base)).toBe("");
    expect(revisionSource({ ...base, via: { kind: "scheduled-run", job_id: "digest-abc123" } })).toBe(
      "from job digest-abc123's run",
    );
  });

  it("extracts the server's error message", () => {
    expect(apiErrorMessage(new Error('PUT /api/schedules/x → 409: {"error":"changed","code":"conflict"}'))).toBe("changed");
    expect(apiErrorMessage(new Error("GET /api/schedules → 502: Bad Gateway"))).toBe("Bad Gateway");
    expect(apiErrorMessage("plain")).toBe("plain");
  });
});

describe("edit preview payload", () => {
  it("uses id, never jobId, to preserve the existing job's state and rate phase", () => {
    const payload = previewFromForm(form(), "digest-abc123");
    expect(payload.id).toBe("digest-abc123");
    expect(payload).not.toHaveProperty("jobId");
    expect(previewFromForm(form())).not.toHaveProperty("id");
  });

  it("does not alter a completed one-shot's time when only its metadata changes", () => {
    const completed = job({ kind: "once", expression: "at(2000-01-01T09:15:37)", status: "completed", enabled: false });
    const edited = { ...formFromJob(completed), prompt: "new prompt" };
    expect(changesFromForm(completed, edited)).toEqual({ prompt: "new prompt" });
    expect(previewFromForm(edited, completed.id).expression).toBe(completed.expression);
    expect(formErrors(edited)).toEqual({});
  });
});
