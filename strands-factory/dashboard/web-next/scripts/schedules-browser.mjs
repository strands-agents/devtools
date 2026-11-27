/** Optional local UI regression: install playwright separately, then run `node scripts/schedules-browser.mjs`.
 * Uses only Vite's in-memory mock API; no credentials, AWS or external requests.
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { chromium } from "playwright";

const port = 5187;
const origin = `http://127.0.0.1:${port}`;
const server = spawn(process.execPath, ["node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], { stdio: ["ignore", "pipe", "pipe"] });
let browser;
try {
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Vite did not start")), 30_000);
    server.stdout.on("data", (data) => { if (data.toString().includes(String(port))) { clearTimeout(timeout); resolve(); } });
    server.stderr.on("data", (data) => process.stderr.write(data));
    server.on("exit", (code) => { clearTimeout(timeout); reject(new Error(`Vite exited ${code}`)); });
  });
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 1000 }, timezoneId: "UTC" });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/*", async (route) => {
    const url = route.request().url();
    if (!url.startsWith(origin)) return route.abort();
    // Expose the unmodified MockApi instance for fixture setup and request assertions.
    if (new URL(url).pathname === "/src/api/index.ts") {
      const response = await route.fetch();
      const body = (await response.text()).replace("instance = new MockApi();", "instance = window.__scheduleApi = new MockApi();");
      assert(body.includes("window.__scheduleApi"));
      return route.fulfill({ response, body });
    }
    return route.continue();
  });
  await page.goto(`${origin}/#/schedules`);
  await page.getByRole("button", { name: "New schedule", exact: true }).waitFor();
  await page.evaluate(() => {
    window.__previews = [];
    window.__updates = [];
    const api = window.__scheduleApi;
    const preview = api.previewSchedule.bind(api);
    api.previewSchedule = (spec) => { window.__previews.push(spec); return preview(spec); };
    const update = api.updateSchedule.bind(api);
    api.updateSchedule = (id, changes, version) => { window.__updates.push({ id, changes, version }); return update(id, changes, version); };
  });

  await page.getByRole("button", { name: "New schedule", exact: true }).click();
  let dialog = page.getByRole("dialog");
  await dialog.getByLabel("Name", { exact: true }).fill("Browser regression job");
  await dialog.getByLabel("Prompt", { exact: true }).fill("Original prompt");
  await dialog.getByText("Next runs", { exact: true }).waitFor();
  await dialog.getByRole("button", { name: "Create", exact: true }).click();
  await page.getByRole("heading", { name: "Browser regression job", exact: true }).waitFor();
  const createdId = await page.evaluate(() => window.__scheduleApi.schedules.find((j) => j.name === "Browser regression job").id);
  console.log("PASS create and live preview");

  await page.getByRole("button", { name: "Edit", exact: true }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByLabel("Prompt", { exact: true }).fill("Updated prompt");
  await page.waitForFunction((id) => window.__previews.some((p) => p.id === id && p.prompt === "Updated prompt"), createdId);
  assert.equal(await page.evaluate(() => window.__previews.some((p) => "jobId" in p)), false);
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await page.getByText("Updated prompt", { exact: true }).waitFor();
  assert.equal(await page.evaluate(() => window.__updates.at(-1).version), 1);
  console.log("PASS edit sends id in preview and expected version in save");

  await page.getByRole("button", { name: "Run now", exact: true }).click();
  await page.getByText("Run dispatched", { exact: true }).waitFor();
  await page.getByRole("cell", { name: "manual", exact: true }).waitFor();
  assert(await page.locator(`a[href*="sched-${createdId}"]`).count() > 0);
  console.log("PASS run now and past-run session link");

  await page.getByRole("button", { name: "Revert", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Revert", exact: true }).click();
  await page.getByText("Original prompt", { exact: true }).waitFor();
  console.log("PASS revision revert");

  await page.evaluate(() => {
    const api = window.__scheduleApi;
    const run = api.runSchedule.bind(api);
    window.__runCalls = 0;
    api.runSchedule = async (id) => {
      window.__runCalls++;
      await run(id);
      throw new TypeError("Failed to fetch");
    };
    const button = [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === "Run now");
    button.click(); button.click();
  });
  await page.getByText(/Acceptance may be unknown/).waitFor();
  assert.equal(await page.evaluate(() => window.__runCalls), 1);
  console.log("PASS duplicate click prevented and unknown acceptance warns before retry");

  const completedId = await page.evaluate(() => {
    const api = window.__scheduleApi;
    const job = api.schedules.find((j) => j.kind === "once");
    Object.assign(job, { expression: "at(2000-01-01T09:00:37)", status: "completed", enabled: false });
    location.hash = `/schedules/${job.id}`;
    return job.id;
  });
  await page.getByText("The job is completed.", { exact: true }).waitFor();
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByText("This one-shot job has completed", { exact: true }).waitFor();
  assert.equal(await dialog.getByLabel("Time", { exact: true }).inputValue(), "09:00:37");
  await dialog.getByLabel("Prompt", { exact: true }).fill("Completed metadata edit");
  await dialog.getByLabel("Time", { exact: true }).fill("");
  await dialog.getByText("Fill in the schedule to preview its next runs.", { exact: true }).waitFor();
  await dialog.getByLabel("Time", { exact: true }).fill("09:00:37");
  await dialog.getByText("No upcoming runs.", { exact: true }).waitFor();
  console.log("PASS clearing an incomplete expression resets the pending preview");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await page.getByText("Completed metadata edit", { exact: true }).waitFor();
  const metadata = await page.evaluate(() => window.__updates.at(-1));
  assert.deepEqual(metadata.changes, { prompt: "Completed metadata edit" });
  console.log("PASS completed past one-shot metadata edit preserves time/seconds and completed state");

  await page.getByRole("button", { name: "Edit", exact: true }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByLabel("Date", { exact: true }).fill("2099-01-02");
  await page.waitForFunction((id) => window.__previews.some((p) => p.id === id && p.expression === "at(2099-01-02T09:00:37)"), completedId);
  await dialog.getByText("No upcoming runs.", { exact: true }).waitFor();
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await page.getByText("at(2099-01-02T09:00:37)", { exact: true }).waitFor();
  await page.getByText("The job is completed.", { exact: true }).waitFor();
  await page.getByRole("button", { name: "Resume", exact: true }).click();
  await page.getByRole("button", { name: "Pause", exact: true }).waitFor();
  await page.waitForFunction((id) => window.__scheduleApi.schedules.find((j) => j.id === id).status === "active", completedId);
  const next = await page.evaluate(async (id) => (await window.__scheduleApi.getSchedule(id)).job.next_runs, completedId);
  assert.deepEqual(next, ["2099-01-02T14:00:37.000Z"]);
  console.log("PASS future edit stays completed until explicit Resume; New York time resolves correctly");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByLabel("Prompt", { exact: true }).fill("Mobile metadata edit");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await page.getByText("Mobile metadata edit", { exact: true }).waitFor();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.getByRole("button", { name: "Pause", exact: true }).click();
  await page.getByRole("button", { name: "Resume", exact: true }).waitFor();
  await page.getByRole("button", { name: "Resume", exact: true }).click();
  await page.getByRole("button", { name: "Pause", exact: true }).waitFor();
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Delete", exact: true }).click();
  await page.getByRole("button", { name: "New schedule", exact: true }).waitFor();
  assert.equal(await page.evaluate((id) => window.__scheduleApi.schedules.some((j) => j.id === id), completedId), false);
  console.log("PASS mobile 390px edit/pause/resume/delete without horizontal page overflow");

  await page.getByRole("button", { name: "New schedule", exact: true }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByLabel("Name", { exact: true }).fill("Mobile once job");
  await dialog.getByLabel("Prompt", { exact: true }).fill("One-time mobile task");
  await dialog.getByText("Once", { exact: true }).click();
  await dialog.getByLabel("Date", { exact: true }).fill("2099-02-03");
  await dialog.getByLabel("Time", { exact: true }).fill("12:34:56");
  await dialog.getByText("Next runs", { exact: true }).waitFor();
  await dialog.getByRole("button", { name: "Create", exact: true }).click();
  await page.getByRole("heading", { name: "Mobile once job", exact: true }).waitFor();
  assert.equal(await page.evaluate(() => window.__scheduleApi.schedules.find((j) => j.name === "Mobile once job").expression), "at(2099-02-03T12:34:56)");
  console.log("PASS mobile one-shot create with second-precision preview");
  assert.deepEqual(errors, []);
  console.log("PASS no browser runtime errors; all scheduler browser regressions passed");
} finally {
  await browser?.close();
  server.kill("SIGTERM");
}
