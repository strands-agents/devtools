import { renderToString } from "react-dom/server";
import { MantineProvider } from "@mantine/core";
import { describe, expect, it, vi } from "vitest";
import type { ScheduleStatus } from "../../api/types";
import { MockApi } from "../../api/mock";
import { defaultForm, type ScheduleForm as Form } from "./schedule";
import { ScheduleForm } from "./ScheduleForm";

const api = new MockApi();
vi.mock("../../api/ApiContext", () => ({ useApi: () => api }));

function render(over: Partial<Form>, jobId?: string, status?: ScheduleStatus): string {
  return renderToString(
    <MantineProvider>
      <ScheduleForm
        initial={{ ...defaultForm(new Date("2030-01-01T12:00:00Z"), "UTC"), ...over }}
        skills={["parity-sync"]}
        sessionModes={["daily", "single", "per-run"]}
        limits={{ min_interval_minutes: 60, max_prompt_chars: 20000 }}
        jobId={jobId}
        status={status}
        submitLabel="Save"
        onSubmit={async () => {}}
        onCancel={() => {}}
      />
    </MantineProvider>,
  );
}

describe("ScheduleForm renders every schedule kind", () => {
  it("explains that completed jobs require an explicit resume", () => {
    const html = render({ kind: "once" }, "job-1", "completed");
    expect(html).toContain("Editing its details or time does not schedule it again");
    expect(html).toContain("then select Resume");
  });

  it("once: date + time", () => {
    const html = render({ kind: "once" });
    expect(html).toContain('type="date"');
    expect(html).toContain('type="time"');
  });

  it("recurring presets and the raw expression box", () => {
    expect(render({ preset: "weekly" })).toContain("Day");
    expect(render({ preset: "hours" })).toContain("Every N hours");
    expect(render({ preset: "custom" })).toContain("EventBridge Scheduler syntax; at most once every hour");
  });

  it("notify targets, and the enabled switch only when creating", () => {
    expect(render({ notifyKind: "github" })).toContain("Issue, PR or discussion URL");
    expect(render({})).toContain("home channel when the deployment has one");
    expect(render({})).toContain("never post to a Slack DM");
    expect(render({})).not.toContain("Current Slack target");
    expect(render({})).not.toContain("Channel id");
    expect(render({})).not.toContain("Thread ts");
    expect(render({})).toContain("Off: create it paused");
    expect(render({}, "job-1")).not.toContain("Off: create it paused");
  });

  it("keeps a job's home-channel target but offers no Slack channel field", () => {
    const html = render({ notifyKind: "slack", notifyChannel: "C0HOME", notifyThread: "1.2" }, "job-1");
    expect(html).toContain("Current Slack target");
    expect(html).toContain("Keeps the Slack target");
    expect(html).toContain("only while it is the enabled home channel");
    expect(html).toContain("C0HOME");
    expect(html).not.toContain("Channel id");
    expect(html).not.toContain("Thread ts");
  });
});
