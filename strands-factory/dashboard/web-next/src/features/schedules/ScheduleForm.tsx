/**
 * The create / edit form for a scheduled job, with a live preview of the next 5 runs (the server
 * validates the spec exactly as a save would, so its errors show before submitting).
 */
import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Group,
  List,
  SegmentedControl,
  Select,
  Stack,
  Switch,
  Text,
  Textarea,
  TextInput,
} from "@mantine/core";
import { IconAlertTriangle, IconClockHour4 } from "@tabler/icons-react";
import { useApi } from "../../api/ApiContext";
import type { ScheduleLimits, SchedulePreview, ScheduleStatus, SessionMode } from "../../api/types";
import {
  apiErrorMessage,
  browserTimezone,
  formatInZone,
  formErrors,
  PRESETS,
  SESSION_MODE_LABELS,
  previewFromForm,
  timezoneOptions,
  WEEKDAYS,
  type NotifyKind,
  type RecurringPreset,
  type ScheduleForm as Form,
  type Weekday,
} from "./schedule";

const PREVIEW_DEBOUNCE_MS = 400;

/** The minimum interval in words: "hour", "2 hours", "90 minutes". */
function intervalText(minutes: number): string {
  if (minutes === 60) return "hour";
  return minutes % 60 === 0 ? `${minutes / 60} hours` : `${minutes} minutes`;
}

export function ScheduleForm({
  initial,
  skills,
  sessionModes,
  limits,
  jobId,
  status,
  submitLabel,
  onSubmit,
  onCancel,
}: {
  initial: Form;
  skills: string[];
  sessionModes: SessionMode[];
  limits?: ScheduleLimits;
  /** Set when editing: the preview keeps that job's phase. */
  jobId?: string;
  status?: ScheduleStatus;
  submitLabel: string;
  onSubmit: (form: Form) => Promise<void>;
  onCancel: () => void;
}) {
  const api = useApi();
  const [form, setForm] = useState<Form>(initial);
  const [showErrors, setShowErrors] = useState(false);
  const [saving, setSaving] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const [preview, setPreview] = useState<SchedulePreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const set = <K extends keyof Form>(key: K, value: Form[K]) => setForm((f) => ({ ...f, [key]: value }));

  const errors = useMemo(() => formErrors(form, limits), [form, limits]);
  const shown = (key: keyof Form) => (showErrors ? errors[key] : undefined);
  const zones = useMemo(() => timezoneOptions(form.timezone), [form.timezone]);
  const localZone = browserTimezone();

  // The preview is about WHEN: placeholder name/prompt keep "fill these in" out of its errors.
  const spec = previewFromForm(form, jobId);
  const previewKey = JSON.stringify(spec);
  useEffect(() => {
    setPreview(null);
    setPreviewing(Boolean(spec.expression));
    if (!spec.expression) return;
    let live = true;
    const timer = setTimeout(() => {
      api
        .previewSchedule({ ...JSON.parse(previewKey) })
        .then((p) => live && setPreview(p))
        .catch((e) => live && setPreview({ ok: false, errors: [apiErrorMessage(e)], kind: null, next_runs: [] }))
        .finally(() => live && setPreviewing(false));
    }, PREVIEW_DEBOUNCE_MS);
    return () => {
      live = false;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, previewKey]);

  const submit = async () => {
    setShowErrors(true);
    setServerError(null);
    if (Object.keys(errors).length > 0) return;
    setSaving(true);
    try {
      await onSubmit(form);
    } catch (e) {
      setServerError(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Stack gap="sm">
      {status === "completed" ? (
        <Alert color="blue" title="This one-shot job has completed">
          Editing its details or time does not schedule it again. To run it again automatically, choose a future time,
          save, then select Resume.
        </Alert>
      ) : null}
      <TextInput
        label="Name"
        value={form.name}
        onChange={(e) => set("name", e.currentTarget.value)}
        error={shown("name")}
        maxLength={200}
        data-autofocus
      />
      <Textarea
        label="Prompt"
        description={limits ? `${form.prompt.length.toLocaleString()} / ${limits.max_prompt_chars.toLocaleString()} characters` : undefined}
        value={form.prompt}
        onChange={(e) => set("prompt", e.currentTarget.value)}
        error={shown("prompt")}
        autosize
        minRows={4}
        maxRows={14}
      />
      <Select
        label="Skill"
        description="Activated before the prompt"
        placeholder="None"
        data={skills}
        value={form.skill || null}
        onChange={(v) => set("skill", v ?? "")}
        clearable
        searchable
      />

      <SegmentedControl
        value={form.kind}
        onChange={(v) => set("kind", v as Form["kind"])}
        data={[
          { value: "recurring", label: "Recurring" },
          { value: "once", label: "Once" },
        ]}
      />
      {form.kind === "once" ? (
        <Group grow align="flex-start">
          <TextInput
            type="date"
            label="Date"
            value={form.onceDate}
            onChange={(e) => set("onceDate", e.currentTarget.value)}
            error={shown("onceDate")}
          />
          <TextInput
            type="time"
            label="Time"
            step={1}
            value={form.onceTime}
            onChange={(e) => set("onceTime", e.currentTarget.value)}
            error={shown("onceTime")}
          />
        </Group>
      ) : (
        <Stack gap="xs">
          <SegmentedControl
            size="xs"
            value={form.preset}
            onChange={(v) => set("preset", v as RecurringPreset)}
            data={PRESETS}
          />
          {form.preset === "daily" || form.preset === "weekly" ? (
            <Group grow align="flex-start">
              {form.preset === "weekly" ? (
                <Select
                  label="Day"
                  data={WEEKDAYS.map((d) => ({ value: d, label: d.charAt(0) + d.slice(1).toLowerCase() }))}
                  value={form.weekday}
                  onChange={(v) => v && set("weekday", v as Weekday)}
                  allowDeselect={false}
                />
              ) : null}
              <TextInput
                type="time"
                label="At"
                value={form.time}
                onChange={(e) => set("time", e.currentTarget.value)}
                error={shown("time")}
              />
            </Group>
          ) : form.preset === "hours" ? (
            <TextInput
              type="number"
              label="Every N hours"
              min={1}
              max={168}
              value={form.everyHours}
              onChange={(e) => set("everyHours", e.currentTarget.value)}
              error={shown("everyHours")}
            />
          ) : (
            <TextInput
              label="Expression"
              description={`EventBridge Scheduler syntax; at most once every ${intervalText(limits?.min_interval_minutes ?? 60)}`}
              placeholder="cron(0 9 ? * MON-FRI *)  or  rate(2 hours)"
              value={form.custom}
              onChange={(e) => set("custom", e.currentTarget.value)}
              error={shown("custom")}
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
            />
          )}
        </Stack>
      )}
      <Select
        label="Timezone"
        data={zones}
        value={form.timezone}
        onChange={(v) => v && set("timezone", v)}
        searchable
        allowDeselect={false}
        limit={50}
      />

      <PreviewBox preview={preview} loading={previewing} timezone={form.timezone} localZone={localZone} />

      <Select
        label="Session"
        description="Which conversation each run continues"
        data={sessionModes.map((m) => ({ value: m, label: SESSION_MODE_LABELS[m] ?? m }))}
        value={form.sessionMode}
        onChange={(v) => v && set("sessionMode", v as SessionMode)}
        allowDeselect={false}
      />

      <Stack gap={6}>
        <Text size="sm" fw={500}>
          Post the result to
        </Text>
        <SegmentedControl
          size="xs"
          value={form.notifyKind}
          onChange={(v) => set("notifyKind", v as NotifyKind)}
          data={[
            { value: "", label: "Default" },
            // A home-channel thread is set only from that thread; here it can only be kept.
            ...(initial.notifyKind === "slack" && initial.notifyChannel
              ? [{ value: "slack", label: "Current Slack target" }]
              : []),
            { value: "github", label: "GitHub" },
          ]}
        />
        {form.notifyKind === "slack" ? (
          <Text size="xs" c="dimmed">
            Keeps the Slack target {form.notifyChannel}
            {form.notifyThread ? ` (thread ${form.notifyThread})` : ""}. Runs post there only while it
            is the enabled home channel. A home-channel thread is set by asking Strandly there.
          </Text>
        ) : form.notifyKind === "github" ? (
          <TextInput
            label="Issue, PR or discussion URL"
            placeholder="https://github.com/owner/repo/issues/123"
            value={form.notifyUrl}
            onChange={(e) => set("notifyUrl", e.currentTarget.value)}
            error={shown("notifyUrl")}
          />
        ) : (
          <Text size="xs" c="dimmed">
            The team&apos;s Slack home channel when the deployment has one, else nowhere. Scheduled
            jobs never post to a Slack DM.
          </Text>
        )}
      </Stack>

      {jobId ? null : (
        <Switch
          label="Enabled"
          description="Off: create it paused"
          checked={form.enabled}
          onChange={(e) => set("enabled", e.currentTarget.checked)}
        />
      )}

      {serverError ? (
        <Alert color="red" icon={<IconAlertTriangle size={16} />} title="Not saved">
          {serverError}
        </Alert>
      ) : null}

      <Group justify="flex-end" mt="xs">
        <Button variant="default" onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
        <Button onClick={submit} loading={saving}>
          {submitLabel}
        </Button>
      </Group>
    </Stack>
  );
}

function PreviewBox({
  preview,
  loading,
  timezone,
  localZone,
}: {
  preview: SchedulePreview | null;
  loading: boolean;
  timezone: string;
  localZone: string;
}) {
  if (!preview) {
    return (
      <Text size="sm" c="dimmed">
        {loading ? "Checking…" : "Fill in the schedule to preview its next runs."}
      </Text>
    );
  }
  if (!preview.ok) {
    return (
      <Alert color="orange" variant="light" icon={<IconAlertTriangle size={16} />} title="This schedule would be refused">
        {preview.errors.join("; ")}
      </Alert>
    );
  }
  return (
    <Alert color="frog" variant="light" icon={<IconClockHour4 size={16} />} title={loading ? "Next runs (updating…)" : "Next runs"}>
      {preview.next_runs.length === 0 ? (
        <Text size="sm">No upcoming runs.</Text>
      ) : (
        <List size="sm" spacing={2}>
          {preview.next_runs.map((t) => (
            <List.Item key={t}>
              {formatInZone(t, timezone)} <Text span c="dimmed" size="xs">({timezone})</Text>
              {localZone !== timezone ? (
                <Text span c="dimmed" size="xs">
                  {" "}· {formatInZone(t, localZone)} your time
                </Text>
              ) : null}
            </List.Item>
          ))}
        </List>
      )}
    </Alert>
  );
}
