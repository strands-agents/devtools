/**
 * Schedules — the scheduled jobs (GET /api/schedules): when each runs next, how its last run went
 * (linked to its session), who made it, an enabled switch, and "New schedule".
 */
import { useState } from "react";
import { Anchor, Badge, Button, Group, Modal, Paper, Stack, Switch, Text, Title, Tooltip } from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import { notifications } from "@mantine/notifications";
import { IconPlus } from "@tabler/icons-react";
import { Link, useNavigate } from "react-router-dom";
import { useApi } from "../../api/ApiContext";
import type { Schedule } from "../../api/types";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlock";
import { relTimeAgo } from "../../lib/format";
import { scheduleLink, sessionLink } from "../../lib/links";
import { useAsync } from "../../lib/useAsync";
import {
  actorLabel,
  apiErrorMessage,
  canToggle,
  defaultForm,
  describeSchedule,
  formatInZone,
  relFuture,
  runStatusColor,
  runStatusLabel,
  specFromForm,
  statusColor,
  syncBadge,
} from "./schedule";
import { ScheduleForm } from "./ScheduleForm";
import classes from "./schedules.module.css";

function ScheduleRow({ job, onToggle, busy }: { job: Schedule; onToggle: () => void; busy: boolean }) {
  const sync = syncBadge(job.sync_state);
  const next = job.next_runs[0];
  return (
    <Paper className={classes.row} p="sm">
      <div className={classes.rowBody}>
        <Group gap={6} wrap="nowrap" className={classes.rowTop}>
          <Anchor component={Link} to={scheduleLink(job.id)} fw={600} className={classes.rowTitle}>
            {job.name}
          </Anchor>
          {job.status !== "active" ? (
            <Badge size="sm" variant="light" radius="sm" color={statusColor(job.status)}>
              {job.status}
            </Badge>
          ) : null}
          {sync ? (
            <Tooltip label={job.sync_error ?? "The schedule will be re-synced"}>
              <Badge size="sm" variant="light" radius="sm" color={sync.color}>
                {sync.label}
              </Badge>
            </Tooltip>
          ) : null}
        </Group>
        <Text size="sm" c="dimmed">
          {describeSchedule(job.expression, job.timezone)}
        </Text>
        <div className={classes.rowMeta}>
          {next ? (
            <Tooltip label={`${formatInZone(next, job.timezone)} (${job.timezone})`}>
              <span>next {relFuture(next)}</span>
            </Tooltip>
          ) : (
            <span>no upcoming run</span>
          )}
          {job.last_run ? (
            <span>
              · last{" "}
              <Anchor component={Link} to={sessionLink(job.last_run.session_id)} size="xs">
                <Badge size="xs" variant="light" radius="sm" color={runStatusColor(job.last_run.status)} style={{ cursor: "pointer" }}>
                  {runStatusLabel(job.last_run.status)}
                </Badge>
              </Anchor>{" "}
              {relTimeAgo(job.last_run.time)}
            </span>
          ) : (
            <span>· never run</span>
          )}
          <span>· by {actorLabel(job.created_by.actor)}</span>
        </div>
      </div>
      <Tooltip label={canToggle(job) ? (job.enabled ? "Pause" : "Resume") : `${job.status}: resume it from its page`}>
        <div>
          <Switch
            checked={job.enabled}
            onChange={onToggle}
            disabled={busy || !canToggle(job)}
            aria-label={job.enabled ? `Pause ${job.name}` : `Resume ${job.name}`}
          />
        </div>
      </Tooltip>
    </Paper>
  );
}

export function SchedulesPage() {
  const api = useApi();
  const navigate = useNavigate();
  const isMobile = useMediaQuery("(max-width: 48em)");
  const { data, loading, error, reload } = useAsync(() => api.listSchedules(), []);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const toggle = async (job: Schedule) => {
    setBusy(job.id);
    try {
      if (job.enabled) await api.pauseSchedule(job.id, job.version);
      else await api.resumeSchedule(job.id, job.version);
      reload();
    } catch (e) {
      notifications.show({ color: "red", title: `Could not ${job.enabled ? "pause" : "resume"} ${job.name}`, message: apiErrorMessage(e) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <Stack gap="md">
      <Group justify="space-between" wrap="nowrap">
        <Title order={2}>Schedules</Title>
        <Button size="sm" radius="xl" leftSection={<IconPlus size={16} />} onClick={() => setCreating(true)} disabled={!data}>
          New schedule
        </Button>
      </Group>

      {loading && !data ? (
        <LoadingBlock label="Loading schedules…" />
      ) : error ? (
        <ErrorBlock error={error} />
      ) : !data || data.jobs.length === 0 ? (
        <EmptyBlock label="No scheduled jobs yet." />
      ) : (
        <Stack gap={6}>
          {data.jobs.map((job) => (
            <ScheduleRow key={job.id} job={job} busy={busy === job.id} onToggle={() => void toggle(job)} />
          ))}
        </Stack>
      )}

      <Modal opened={creating} onClose={() => setCreating(false)} title="New schedule" size="lg" fullScreen={isMobile}>
        {data ? (
          <ScheduleForm
            initial={defaultForm()}
            skills={data.skills}
            sessionModes={data.session_modes}
            limits={data.limits}
            submitLabel="Create"
            onCancel={() => setCreating(false)}
            onSubmit={async (form) => {
              const job = await api.createSchedule(specFromForm(form));
              setCreating(false);
              notifications.show({ color: "green", title: "Schedule created", message: describeSchedule(job.expression, job.timezone) });
              navigate(scheduleLink(job.id));
            }}
          />
        ) : null}
      </Modal>
    </Stack>
  );
}
