import { afterEach, describe, expect, it, vi } from "vitest";
import { MockApi, mockNextRuns } from "./mock";
import type { ScheduleSpec } from "./types";

vi.mock("./fixtures", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./fixtures")>();
  return {
    ...actual,
    mockSchedules: () => actual.mockSchedules().map((job) => job.kind === "once" ? {
      ...job, expression: "at(2000-01-01T09:00:37)", status: "completed", enabled: false,
    } : job),
  };
});

afterEach(() => vi.useRealTimers());

const spec: ScheduleSpec = { name: "Digest", expression: "cron(0 9 * * ? *)", timezone: "America/New_York", prompt: "Summarize" };
const NOW = Date.parse("2030-01-01T12:00:00Z");

describe("mock schedule timing", () => {
  it("uses the job's timezone, not UTC or the browser's timezone", () => {
    expect(mockNextRuns(spec.expression, NOW, 1, spec.timezone)).toEqual(["2030-01-01T14:00:00.000Z"]);
    expect(mockNextRuns("at(2030-01-01T09:00:37)", NOW, 1, spec.timezone)).toEqual(["2030-01-01T14:00:37.000Z"]);
    expect(mockNextRuns("cron(0 9 ? * MON *)", NOW, 1, "Europe/Istanbul")).toEqual(["2030-01-07T06:00:00.000Z"]);
  });

  it("skips nonexistent DST wall times and fires once during an overlap", () => {
    expect(mockNextRuns("cron(30 2 * * ? *)", Date.parse("2030-03-10T00:00:00Z"), 1, spec.timezone))
      .toEqual(["2030-03-11T06:30:00.000Z"]);
    expect(mockNextRuns("cron(30 1 * * ? *)", Date.parse("2030-11-03T00:00:00Z"), 2, spec.timezone))
      .toEqual(["2030-11-03T05:30:00.000Z", "2030-11-04T06:30:00.000Z"]);
  });

  it("keeps the rate's phase when previewing an existing job", () => {
    expect(mockNextRuns("rate(6 hours)", NOW, 2, "UTC", "2030-01-01T01:00:00Z"))
      .toEqual(["2030-01-01T13:00:00.000Z", "2030-01-01T19:00:00.000Z"]);
  });

  it("rejects unknown zones and distinguishes past UTC from future local time", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW);
    const api = new MockApi();
    expect(await api.previewSchedule({ ...spec, timezone: "Not/AZone" })).toMatchObject({ ok: false });
    expect(await api.previewSchedule({ ...spec, expression: "at(2030-01-01T09:00:00)" })).toMatchObject({ ok: true });
    expect(await api.previewSchedule({ ...spec, timezone: "UTC", expression: "at(2030-01-01T09:00:00)" })).toMatchObject({ ok: false });
  });
});

describe("mock completed and paused job edits", () => {
  it("allows metadata-only edits to a completed past one-shot without rescheduling", async () => {
    const api = new MockApi();
    const job = (await api.listSchedules()).jobs.find((j) => j.status === "completed")!;
    expect(await api.previewSchedule({ ...job, id: job.id, prompt: "new metadata" })).toMatchObject({ ok: true, next_runs: [] });
    const edited = await api.updateSchedule(job.id, { prompt: "new metadata" }, job.version);
    expect(edited).toMatchObject({ status: "completed", enabled: false, next_runs: [], expression: job.expression });
    await expect(api.resumeSchedule(job.id, edited.version)).rejects.toThrow("past");
  });

  it("a future edit remains completed until explicitly resumed", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW);
    const api = new MockApi();
    const job = (await api.listSchedules()).jobs.find((j) => j.status === "completed")!;
    const expression = "at(2030-01-02T09:00:37)";
    expect(await api.previewSchedule({ ...job, expression })).toMatchObject({ ok: true, next_runs: [] });
    const edited = await api.updateSchedule(job.id, { expression }, job.version);
    expect(edited).toMatchObject({ status: "completed", enabled: false, next_runs: [] });
    const resumed = await api.resumeSchedule(job.id, edited.version);
    expect(resumed).toMatchObject({ status: "active", enabled: true, next_runs: ["2030-01-02T14:00:37.000Z"] });
  });

  it("paused create and edit previews have no upcoming runs", async () => {
    const api = new MockApi();
    expect(await api.previewSchedule({ ...spec, enabled: false })).toMatchObject({ ok: true, next_runs: [] });
    const job = await api.createSchedule({ ...spec, enabled: false });
    expect(await api.previewSchedule({ ...job, enabled: true })).toMatchObject({ ok: true, next_runs: [] });
  });
});

it("new mock writes carry immutable portal subject and issuer, not an email actor", async () => {
  const job = await new MockApi().createSchedule(spec);
  expect(job.created_by).toEqual({
    actor: "portal:11111111-2222-3333-4444-555555555555",
    surface: "dashboard",
    issuer: "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_demo",
  });
  expect(job.updated_by).toEqual(job.created_by);
});
