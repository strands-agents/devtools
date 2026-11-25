/**
 * Schedules page logic, kept pure so it unit-tests without a DOM: the create/edit form model and its
 * mapping to and from EventBridge Scheduler expressions (presets for the common cadences, a raw
 * cron/rate box for the rest), human-readable descriptions, and the badges' colours.
 *
 * Expressions follow the Scheduler dialect the API validates: `at(yyyy-mm-ddThh:mm:ss)` in the
 * job's timezone; `cron(min hour day-of-month month day-of-week year)` with exactly one `?` day
 * field; `rate(N unit)` with a singular unit for 1.
 */
import type {
  NotifyTarget,
  Schedule,
  ScheduleChanges,
  ScheduleKind,
  ScheduleLimits,
  ScheduleRevision,
  ScheduleSpec,
  SessionMode,
} from "../../api/types";

export const WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"] as const;
export type Weekday = (typeof WEEKDAYS)[number];
const WEEKDAY_NAMES: Record<Weekday, string> = {
  MON: "Monday",
  TUE: "Tuesday",
  WED: "Wednesday",
  THU: "Thursday",
  FRI: "Friday",
  SAT: "Saturday",
  SUN: "Sunday",
};
/** Scheduler cron numbers day-of-week 1-7 = SUN-SAT. */
const CRON_DAY_NUMBERS: Record<string, Weekday> = {
  "1": "SUN",
  "2": "MON",
  "3": "TUE",
  "4": "WED",
  "5": "THU",
  "6": "FRI",
  "7": "SAT",
};

export type RecurringPreset = "daily" | "weekly" | "hours" | "custom";

export const PRESETS: { value: RecurringPreset; label: string }[] = [
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "hours", label: "Every N hours" },
  { value: "custom", label: "Cron / rate" },
];

export const SESSION_MODE_LABELS: Record<SessionMode, string> = {
  daily: "One thread per day",
  single: "One thread for ever",
  "per-run": "A new thread per run",
};

/** "slack" only keeps the job's current home-channel target: the portal never names a Slack channel. */
export type NotifyKind = "" | "slack" | "github";

/** The create/edit form's state. Strings throughout so inputs bind directly. */
export interface ScheduleForm {
  name: string;
  prompt: string;
  /** "" = no skill. */
  skill: string;
  kind: ScheduleKind;
  /** once: local date (YYYY-MM-DD) and time (HH:MM) in `timezone`. */
  onceDate: string;
  onceTime: string;
  preset: RecurringPreset;
  /** daily / weekly: HH:MM in `timezone`. */
  time: string;
  weekday: Weekday;
  everyHours: string;
  /** custom: a raw `cron(...)` / `rate(...)`. */
  custom: string;
  timezone: string;
  sessionMode: SessionMode;
  notifyKind: NotifyKind;
  notifyChannel: string;
  notifyThread: string;
  notifyUrl: string;
  /** Create only: start paused when false. */
  enabled: boolean;
}

const pad = (n: number) => String(n).padStart(2, "0");
const TIME_RE = /^([01]\d|2[0-3]):([0-5]\d)$/;
const ONCE_TIME_RE = /^([01]\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/** The browser's IANA timezone, else UTC. */
export function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** Every IANA timezone the browser knows (UTC first), for the timezone picker. */
export function timezoneOptions(current?: string): string[] {
  const intl = Intl as unknown as { supportedValuesOf?: (key: string) => string[] };
  let zones: string[] = [];
  try {
    zones = intl.supportedValuesOf?.("timeZone") ?? [];
  } catch {
    zones = [];
  }
  const all = new Set(["UTC", ...zones]);
  if (current) all.add(current);
  return [...all];
}

/** The local date (YYYY-MM-DD) of `when` in `tz`. */
function localDate(when: Date, tz: string): string {
  try {
    const parts = new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" })
      .formatToParts(when);
    const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
    return `${get("year")}-${get("month")}-${get("day")}`;
  } catch {
    return when.toISOString().slice(0, 10);
  }
}

export function defaultForm(now: Date = new Date(), timezone: string = browserTimezone()): ScheduleForm {
  return {
    name: "",
    prompt: "",
    skill: "",
    kind: "recurring",
    onceDate: localDate(new Date(now.getTime() + 86_400_000), timezone),
    onceTime: "09:00",
    preset: "daily",
    time: "09:00",
    weekday: "MON",
    everyHours: "6",
    custom: "",
    timezone,
    sessionMode: "daily",
    notifyKind: "",
    notifyChannel: "",
    notifyThread: "",
    notifyUrl: "",
    enabled: true,
  };
}

/** The Scheduler expression the form describes ("" when a needed part is missing). */
export function expressionFor(form: ScheduleForm): string {
  if (form.kind === "once") {
    if (!DATE_RE.test(form.onceDate) || !ONCE_TIME_RE.test(form.onceTime)) return "";
    return `at(${form.onceDate}T${form.onceTime.length === 5 ? `${form.onceTime}:00` : form.onceTime})`;
  }
  if (form.preset === "custom") return form.custom.trim();
  if (form.preset === "hours") {
    const n = Number(form.everyHours);
    if (!Number.isInteger(n) || n < 1) return "";
    return `rate(${n} ${n === 1 ? "hour" : "hours"})`;
  }
  const m = TIME_RE.exec(form.time);
  if (!m) return "";
  const [hour, minute] = [Number(m[1]), Number(m[2])];
  if (form.preset === "weekly") return `cron(${minute} ${hour} ? * ${form.weekday} *)`;
  return `cron(${minute} ${hour} * * ? *)`;
}

type PresetFields = Pick<ScheduleForm, "kind" | "onceDate" | "onceTime" | "preset" | "time" | "weekday" | "everyHours" | "custom">;

const AT_RE = /^at\((\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})\)$/;
const RATE_RE = /^rate\(([1-9]\d*) (minute|minutes|hour|hours|day|days)\)$/;
const CRON_RE = /^cron\((.*)\)$/;
const NUM_RE = /^\d{1,2}$/;

/** The form fields an existing expression maps to: a preset when it is one, else the raw box. */
export function presetFor(expression: string): Partial<PresetFields> {
  const expr = expression.trim();
  const at = AT_RE.exec(expr);
  if (at) return { kind: "once", onceDate: at[1], onceTime: `${at[2]}:${at[3]}${at[4] === "00" ? "" : `:${at[4]}`}` };
  const rate = RATE_RE.exec(expr);
  if (rate && rate[2].startsWith("hour")) return { kind: "recurring", preset: "hours", everyHours: rate[1] };
  const cron = CRON_RE.exec(expr);
  if (cron) {
    const f = cron[1].trim().split(/\s+/);
    if (f.length === 6 && NUM_RE.test(f[0]) && NUM_RE.test(f[1]) && f[3] === "*" && f[5] === "*") {
      const [minute, hour] = [Number(f[0]), Number(f[1])];
      if (minute <= 59 && hour <= 23) {
        const time = `${pad(hour)}:${pad(minute)}`;
        if (f[2] === "*" && f[4] === "?") return { kind: "recurring", preset: "daily", time };
        const day = (WEEKDAYS as readonly string[]).includes(f[4]) ? (f[4] as Weekday) : CRON_DAY_NUMBERS[f[4]];
        if (f[2] === "?" && day) return { kind: "recurring", preset: "weekly", time, weekday: day };
      }
    }
  }
  return { kind: "recurring", preset: "custom", custom: expr };
}

/** The form for editing `job`. */
export function formFromJob(job: Schedule, now: Date = new Date()): ScheduleForm {
  const base = defaultForm(now, job.timezone);
  return {
    ...base,
    ...presetFor(job.expression),
    name: job.name,
    prompt: job.prompt,
    skill: job.skill ?? "",
    timezone: job.timezone,
    sessionMode: job.session_mode,
    // A stored Slack DM target is never honored, so the form shows (and saving sends) the default.
    notifyKind: isSlackDm(job.notify) ? "" : job.notify?.kind ?? "",
    notifyChannel: isSlackDm(job.notify) ? "" : job.notify?.channel ?? "",
    notifyThread: isSlackDm(job.notify) ? "" : job.notify?.thread_ts ?? "",
    notifyUrl: job.notify?.url ?? "",
    enabled: job.enabled,
  };
}

/** A Slack DM target: a scheduled job never posts to one. */
export const isSlackDm = (t: NotifyTarget | null | undefined) => t?.kind === "slack" && (t.channel ?? "").startsWith("D");

export function notifyFor(form: ScheduleForm): NotifyTarget | null {
  if (form.notifyKind === "slack") {
    const target: NotifyTarget = { kind: "slack", channel: form.notifyChannel.trim() };
    if (form.notifyThread.trim()) target.thread_ts = form.notifyThread.trim();
    return target;
  }
  if (form.notifyKind === "github") return { kind: "github", url: form.notifyUrl.trim() };
  return null;
}

/** The create (and preview) request body. */
export function specFromForm(form: ScheduleForm): ScheduleSpec {
  return {
    name: form.name.trim(),
    expression: expressionFor(form),
    prompt: form.prompt,
    timezone: form.timezone || "UTC",
    skill: form.skill || null,
    session_mode: form.sessionMode,
    notify: notifyFor(form),
    enabled: form.enabled,
  };
}

/** Preview uses the existing job id so edits preserve status and rate phase. */
export function previewFromForm(form: ScheduleForm, id?: string): ScheduleSpec & { id?: string } {
  const spec = specFromForm(form);
  const preview = { ...spec, name: spec.name || "preview", prompt: spec.prompt || "preview" };
  return id ? { ...preview, id } : preview;
}

/** Same destination (fields the server does not return compare as absent). */
const notifyPlace = (t: NotifyTarget | null) =>
  t ? JSON.stringify([t.kind, t.channel ?? null, t.thread_ts ?? null, t.url ?? null]) : "null";
const sameNotify = (a: NotifyTarget | null, b: NotifyTarget | null) => notifyPlace(a) === notifyPlace(b);

/** An edit's request body: only what differs from `job` (empty when nothing changed). */
export function changesFromForm(job: Schedule, form: ScheduleForm): ScheduleChanges {
  const spec = specFromForm(form);
  const changes: ScheduleChanges = {};
  if (spec.name !== job.name) changes.name = spec.name;
  if (spec.prompt !== job.prompt) changes.prompt = spec.prompt;
  if ((spec.skill ?? null) !== (job.skill ?? null)) changes.skill = spec.skill ?? null;
  if (spec.expression !== job.expression) changes.expression = spec.expression;
  if (spec.timezone !== job.timezone) changes.timezone = spec.timezone;
  if (spec.session_mode !== job.session_mode) changes.session_mode = spec.session_mode;
  if (!sameNotify(spec.notify ?? null, job.notify)) changes.notify = spec.notify ?? null;
  return changes;
}

/** What is wrong with the form before asking the server (which validates everything again). */
export function formErrors(form: ScheduleForm, limits?: ScheduleLimits): Partial<Record<keyof ScheduleForm, string>> {
  const errors: Partial<Record<keyof ScheduleForm, string>> = {};
  if (!form.name.trim()) errors.name = "Give the job a name";
  if (!form.prompt.trim()) errors.prompt = "The prompt is empty";
  else if (limits && form.prompt.length > limits.max_prompt_chars)
    errors.prompt = `At most ${limits.max_prompt_chars.toLocaleString()} characters`;
  if (form.kind === "once") {
    if (!DATE_RE.test(form.onceDate)) errors.onceDate = "Pick a date";
    if (!ONCE_TIME_RE.test(form.onceTime)) errors.onceTime = "Pick a time";
  } else if (form.preset === "custom") {
    if (!/^(cron|rate)\(.+\)$/.test(form.custom.trim())) errors.custom = "A cron(...) or rate(...) expression";
  } else if (form.preset === "hours") {
    const n = Number(form.everyHours);
    if (!Number.isInteger(n) || n < 1 || n > 168) errors.everyHours = "A whole number of hours, 1-168";
  } else if (!TIME_RE.test(form.time)) errors.time = "Pick a time";
  if (form.notifyKind === "github" && !/^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/(issues|pull|discussions)\/\d+$/.test(form.notifyUrl.trim()))
    errors.notifyUrl = "A GitHub issue, PR or discussion URL";
  return errors;
}

// --- display ---------------------------------------------------------------------------------------

function formatAt(date: string, time: string): string {
  const d = new Date(`${date}T00:00:00Z`);
  const day = Number.isNaN(d.getTime())
    ? date
    : d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
  return `${day} at ${time}`;
}

/** "Daily at 14:00 (UTC)", "Weekly on Monday at 09:00 (Europe/Berlin)", "Once on Jan 2, 2030 at 09:00 (…)". */
export function describeSchedule(expression: string, timezone: string): string {
  const p = presetFor(expression);
  const tz = ` (${timezone || "UTC"})`;
  if (p.kind === "once" && p.onceDate && p.onceTime) return `Once on ${formatAt(p.onceDate, p.onceTime)}${tz}`;
  if (p.preset === "daily") return `Daily at ${p.time}${tz}`;
  if (p.preset === "weekly" && p.weekday) return `Weekly on ${WEEKDAY_NAMES[p.weekday]} at ${p.time}${tz}`;
  if (p.preset === "hours") return Number(p.everyHours) === 1 ? "Every hour" : `Every ${p.everyHours} hours`;
  const rate = RATE_RE.exec(expression.trim());
  if (rate) {
    const unit = rate[2].replace(/s$/, "");
    return Number(rate[1]) === 1 ? `Every ${unit}` : `Every ${rate[1]} ${unit}s`;
  }
  return `${expression.trim()}${tz}`;
}

/** `iso` as a wall-clock time in `tz` ("Tue, Jan 7, 09:00"); the raw string when unparsable. */
export function formatInZone(iso: string, tz: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const opts: Intl.DateTimeFormatOptions = {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  };
  try {
    return d.toLocaleString("en-US", { ...opts, timeZone: tz });
  } catch {
    return d.toLocaleString("en-US", { ...opts, timeZone: "UTC" });
  }
}

/** "in 5m", "in 3h", "in 2d" (or "now"); "" when unparsable. */
export function relFuture(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "";
  const ms = Date.parse(iso) - now;
  if (!Number.isFinite(ms)) return "";
  const min = Math.round(ms / 60_000);
  if (min < 1) return "now";
  if (min < 60) return `in ${min}m`;
  const hr = Math.round(min / 60);
  if (hr < 48) return `in ${hr}h`;
  return `in ${Math.round(hr / 24)}d`;
}

/** The actor id without its namespace for display: `portal:alice@x` → `alice@x`. */
export function actorLabel(actor: string | undefined): string {
  if (!actor) return "unknown";
  const [ns, ...rest] = actor.split(":");
  const id = rest.join(":");
  if (!id) return actor;
  return ns === "portal" ? id : `${id} (${ns})`;
}

export function statusColor(status: string): string {
  return status === "active" ? "green" : status === "paused" ? "yellow" : "gray";
}

/** Launch outcomes (`last_run.status`) and ledger run statuses. */
export function runStatusColor(status: string | undefined): string {
  switch (status) {
    case "dispatched":
    case "completed":
      return "green";
    case "maybe-dispatched":
    case "running":
      return "blue";
    case "busy":
    case "cancelled":
      return "yellow";
    case "error":
    case "failed":
      return "red";
    default:
      return "gray";
  }
}

export function runStatusLabel(status: string | undefined): string {
  if (status === "maybe-dispatched") return "acceptance unknown";
  if (status === "busy") return "skipped (busy)";
  return status || "unknown";
}

/** How a past run started: `scheduled:<id>[:manual|:late]` → schedule / manual / late, else as is. */
export function runTrigger(source: string | undefined): string {
  if (!source) return "";
  const [ns, , how] = source.split(":");
  if (ns !== "scheduled") return source;
  return how || "schedule";
}

/** The sync badge, or null when the schedule matches the row. */
export function syncBadge(state: string): { label: string; color: string } | null {
  if (state === "unsynced") return { label: "unsynced", color: "red" };
  if (state === "pending") return { label: "syncing", color: "blue" };
  return null;
}

/** Whether the enabled switch applies (a completed or expired job is resumed from its page). */
export function canToggle(job: Schedule): boolean {
  return job.status === "active" || job.status === "paused";
}

const DEFINITION_KEYS = ["name", "expression", "timezone", "prompt", "skill", "session_mode", "notify", "status", "expires_at"];

/** Where a revision came from beyond its actor: a job's own scheduled run, or nothing. */
export function revisionSource(rev: ScheduleRevision): string {
  return rev.via?.job_id ? `from job ${rev.via.job_id}'s run` : "";
}

/**
 * What each revision changed relative to the one before it (revisions are newest first): the
 * definition fields whose value differs. The oldest revision lists nothing (it is the start).
 */
export function revisionChanges(revisions: ScheduleRevision[]): Map<number, string[]> {
  const out = new Map<number, string[]>();
  for (let i = 0; i < revisions.length; i++) {
    const cur = revisions[i];
    const prev = revisions[i + 1];
    if (!prev) {
      out.set(cur.rev, []);
      continue;
    }
    out.set(
      cur.rev,
      DEFINITION_KEYS.filter((k) => JSON.stringify(cur.snapshot[k] ?? null) !== JSON.stringify(prev.snapshot[k] ?? null)),
    );
  }
  return out;
}

/** A failed write's message without the request line the client prefixes (`PUT /api/… → 400: `). */
export function apiErrorMessage(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e);
  const body = raw.replace(/^[A-Z]+ \S+ → \d+: /, "");
  try {
    const parsed = JSON.parse(body) as { error?: string };
    if (parsed && typeof parsed.error === "string") return parsed.error;
  } catch {
    // not JSON: the message as is
  }
  return body;
}
