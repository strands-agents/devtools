/**
 * One scheduled job (GET /api/schedules/{id}): its definition and next runs, run now / pause /
 * resume / edit / delete, its past runs (the ledger rows of its sessions, linked to the session
 * page) and its revision history with revert. A deleted job shows its history with "Restore".
 */
import { useRef, useState, type ReactNode } from "react";
import { Alert, Anchor, Badge, Button, Group, List, Modal, Stack, Table, Text, Title, Tooltip } from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import { notifications } from "@mantine/notifications";
import {
  IconAlertTriangle,
  IconArrowLeft,
  IconHistory,
  IconPencil,
  IconPlayerPause,
  IconPlayerPlay,
  IconRocket,
  IconTrash,
} from "@tabler/icons-react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useApi } from "../../api/ApiContext";
import type { Schedule, ScheduleList, ScheduleRevision } from "../../api/types";
import { Section } from "../../components/Section";
import { ErrorBlock, LoadingBlock } from "../../components/StateBlock";
import { relTimeAgo } from "../../lib/format";
import { ROUTES, sessionLink } from "../../lib/links";
import { useAsync } from "../../lib/useAsync";
import {
  actorLabel,
  apiErrorMessage,
  browserTimezone,
  changesFromForm,
  describeSchedule,
  formatInZone,
  formFromJob,
  isSlackDm,
  relFuture,
  revisionChanges,
  revisionSource,
  runStatusColor,
  runStatusLabel,
  runTrigger,
  SESSION_MODE_LABELS,
  statusColor,
  syncBadge,
} from "./schedule";
import { ScheduleForm } from "./ScheduleForm";
import classes from "./schedules.module.css";

interface Confirm {
  title: string;
  body: ReactNode;
  label: string;
  color?: string;
  run: () => Promise<void>;
}

function Facts({ job }: { job: Schedule }) {
  const notify = job.notify
    ? job.notify.kind === "slack"
      ? isSlackDm(job.notify)
        ? "Nowhere (scheduled jobs never post to a Slack DM)"
        : `Slack ${job.notify.channel}${job.notify.thread_ts ? ` (thread ${job.notify.thread_ts})` : ""}, only while it is the enabled home channel`
      : job.notify.url
    : "The home channel, when it is on";
  const rows: [string, ReactNode][] = [
    ["Schedule", describeSchedule(job.expression, job.timezone)],
    ["Expression", <code key="e">{job.expression}</code>],
    ["Session", SESSION_MODE_LABELS[job.session_mode] ?? job.session_mode],
    ["Skill", job.skill ?? "—"],
    ["Posts to", notify],
    ["Created", `${actorLabel(job.created_by.actor)} · ${relTimeAgo(job.created_at)}`],
    ["Updated", `${actorLabel(job.updated_by.actor)} · ${relTimeAgo(job.updated_at)}`],
  ];
  if (job.expires_at) rows.push(["Expires", formatInZone(job.expires_at, job.timezone)]);
  return (
    <dl className={classes.facts}>
      {rows.map(([k, v]) => (
        <div key={k} style={{ display: "contents" }}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

function Revisions({
  revisions,
  current,
  onRevert,
}: {
  revisions: ScheduleRevision[];
  current: Schedule | null;
  onRevert: (rev: ScheduleRevision) => void;
}) {
  const changed = revisionChanges(revisions);
  return (
    <Stack gap={8}>
      {revisions.map((r, i) => {
        // The newest revision IS the current definition; a delete has nothing to restore.
        const restorable = r.action !== "delete" && (current ? i > 0 : true);
        const fields = changed.get(r.rev) ?? [];
        return (
          <div key={r.rev} className={classes.revision}>
            <div>
              <Text size="sm">
                <b>#{r.rev}</b> {r.action} · {actorLabel(r.actor.actor)}
                {revisionSource(r) ? ` · ${revisionSource(r)}` : ""} · {relTimeAgo(r.at)}
              </Text>
              {fields.length ? (
                <Text size="xs" c="dimmed">
                  changed {fields.join(", ")}
                </Text>
              ) : null}
            </div>
            {restorable ? (
              <Button size="compact-xs" variant="light" onClick={() => onRevert(r)}>
                {current ? "Revert" : "Restore"}
              </Button>
            ) : null}
          </div>
        );
      })}
    </Stack>
  );
}

function PastRuns({ id, nonce }: { id: string; nonce: number }) {
  const api = useApi();
  const { data, loading, error } = useAsync(() => api.listScheduleRuns(id), [id, nonce]);
  if (loading && !data) return <LoadingBlock label="Loading runs…" />;
  if (error) return <ErrorBlock error={error} />;
  if (!data || data.runs.length === 0)
    return (
      <Text size="sm" c="dimmed">
        No runs in the last {data?.days ?? 30} days.
      </Text>
    );
  const tz = browserTimezone();
  return (
    <Stack gap={4}>
      <Table.ScrollContainer minWidth={420}>
        <Table verticalSpacing={4} fz="sm">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Started</Table.Th>
              <Table.Th>Status</Table.Th>
              <Table.Th>Session</Table.Th>
              <Table.Th>Trigger</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {data.runs.map((r) => (
              <Table.Tr key={r.task_id}>
                <Table.Td>
                  <Tooltip label={r.started_at}>
                    <span>{formatInZone(r.started_at, tz)}</span>
                  </Tooltip>
                </Table.Td>
                <Table.Td>
                  <Tooltip label={r.error ?? (r.duration_ms ? `${Math.round(r.duration_ms / 1000)} s` : r.status)}>
                    <Badge size="sm" variant="light" radius="sm" color={runStatusColor(r.status)}>
                      {r.status}
                    </Badge>
                  </Tooltip>
                </Table.Td>
                <Table.Td>
                  <Anchor component={Link} to={sessionLink(r.session_id)} size="sm">
                    {r.session_id}
                  </Anchor>
                </Table.Td>
                <Table.Td>{runTrigger(r.trigger_source)}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
      {data.complete ? null : (
        <Text size="xs" c="dimmed">
          Showing the newest runs found; older ones in the last {data.days} days may not be listed.
        </Text>
      )}
    </Stack>
  );
}

export function SchedulePage() {
  const { id = "" } = useParams();
  const api = useApi();
  const navigate = useNavigate();
  const isMobile = useMediaQuery("(max-width: 48em)");
  const { data, loading, error, reload } = useAsync(() => api.getSchedule(id), [id]);
  const [meta, setMeta] = useState<ScheduleList | null>(null);
  const [editing, setEditing] = useState(false);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [acting, setActing] = useState(false);
  const actionPending = useRef(false);
  const [runsNonce, setRunsNonce] = useState(0);

  const act = async (label: string, fn: () => Promise<void>) => {
    if (actionPending.current) return;
    actionPending.current = true;
    setActing(true);
    try {
      await fn();
    } catch (e) {
      notifications.show({
        color: "red",
        title: `${label} failed`,
        message: label === "Run now"
          ? `${apiErrorMessage(e)}. Acceptance may be unknown. Check the session and past runs before running again.`
          : apiErrorMessage(e),
      });
    } finally {
      actionPending.current = false;
      setActing(false);
    }
  };

  if (loading && !data) return <LoadingBlock label="Loading schedule…" />;
  if (error) return <ErrorBlock error={error} />;
  if (!data) return null;
  const job = data.job;

  const openEdit = () =>
    void act("Loading the form", async () => {
      setMeta(meta ?? (await api.listSchedules()));
      setEditing(true);
    });

  const runNow = () =>
    void act("Run now", async () => {
      if (!job) return;
      const run = await api.runSchedule(job.id);
      notifications.show({
        color: runStatusColor(run.status),
        title: `Run ${runStatusLabel(run.status)}`,
        message: run.detail ?? `Session ${run.session_id}`,
      });
      reload();
      setRunsNonce((n) => n + 1);
    });

  const toggle = () =>
    void act(job?.enabled ? "Pause" : "Resume", async () => {
      if (!job) return;
      if (job.enabled) await api.pauseSchedule(job.id, job.version);
      else await api.resumeSchedule(job.id, job.version);
      reload();
    });

  const askDelete = () =>
    job &&
    setConfirm({
      title: `Delete ${job.name}?`,
      body: "Its schedule is removed and it stops running. The revision history stays, so it can be restored.",
      label: "Delete",
      color: "red",
      run: async () => {
        await api.deleteSchedule(job.id, job.version);
        notifications.show({ color: "green", title: "Deleted", message: job.name });
        navigate(ROUTES.schedules);
      },
    });

  const askRevert = (r: ScheduleRevision) =>
    setConfirm({
      title: job ? `Revert to revision #${r.rev}?` : `Restore from revision #${r.rev}?`,
      body: job
        ? "The job's definition (schedule, prompt, skill, session, notify target) goes back to this revision."
        : "The job is re-created with this revision's definition.",
      label: job ? "Revert" : "Restore",
      run: async () => {
        await api.revertSchedule(id, r.rev, job ? job.version : data.revisions[0]?.rev);
        reload();
      },
    });

  const sync = job ? syncBadge(job.sync_state) : null;

  return (
    <Stack gap="md">
      <Anchor component={Link} to={ROUTES.schedules} size="sm">
        <Group gap={4}>
          <IconArrowLeft size={14} /> Schedules
        </Group>
      </Anchor>
      <Group justify="space-between" align="flex-start">
        <Group gap={8}>
          <Title order={2}>{job?.name ?? id}</Title>
          {job ? (
            <Badge variant="light" radius="sm" color={statusColor(job.status)}>
              {job.status}
            </Badge>
          ) : (
            <Badge variant="light" radius="sm" color="gray">
              deleted
            </Badge>
          )}
          {sync ? (
            <Badge variant="light" radius="sm" color={sync.color}>
              {sync.label}
            </Badge>
          ) : null}
        </Group>
        {job ? (
          <Group gap="xs">
            <Button size="xs" leftSection={<IconRocket size={14} />} onClick={runNow} loading={acting}>
              Run now
            </Button>
            <Button
              size="xs"
              variant="light"
              leftSection={job.enabled ? <IconPlayerPause size={14} /> : <IconPlayerPlay size={14} />}
              onClick={toggle}
              disabled={acting}
            >
              {job.enabled ? "Pause" : "Resume"}
            </Button>
            <Button size="xs" variant="light" leftSection={<IconPencil size={14} />} onClick={openEdit} disabled={acting}>
              Edit
            </Button>
            <Button size="xs" variant="light" color="red" leftSection={<IconTrash size={14} />} onClick={askDelete} disabled={acting}>
              Delete
            </Button>
          </Group>
        ) : null}
      </Group>

      {job?.sync_state === "unsynced" ? (
        <Alert color="red" variant="light" icon={<IconAlertTriangle size={16} />} title="Schedule out of sync">
          {job.sync_error ?? "The EventBridge schedule does not match yet"} — the hourly reconcile retries it.
        </Alert>
      ) : null}
      {!job ? (
        <Alert color="gray" variant="light" title="This job was deleted">
          Restore it from a revision below.
        </Alert>
      ) : null}

      {job ? (
        <>
          <Section title="Details">
            <Facts job={job} />
          </Section>
          <Section title="Next runs" description={job.status === "active" ? undefined : `The job is ${job.status}.`}>
            {job.next_runs.length ? (
              <List size="sm" spacing={2}>
                {job.next_runs.map((t) => (
                  <List.Item key={t}>
                    {formatInZone(t, job.timezone)}{" "}
                    <Text span size="xs" c="dimmed">
                      ({job.timezone}) · {relFuture(t)}
                    </Text>
                  </List.Item>
                ))}
              </List>
            ) : (
              <Text size="sm" c="dimmed">
                No upcoming runs.
              </Text>
            )}
          </Section>
          <Section title="Prompt">
            <pre className={classes.prompt}>{job.prompt}</pre>
          </Section>
          <Section title="Past runs" description="Runs in this job's sessions, newest first">
            <PastRuns id={job.id} nonce={runsNonce} />
          </Section>
        </>
      ) : null}

      <Section title="Revisions" icon={<IconHistory size={16} />}>
        <Revisions revisions={data.revisions} current={job} onRevert={askRevert} />
      </Section>

      <Modal opened={editing} onClose={() => setEditing(false)} title={`Edit ${job?.name ?? ""}`} size="lg" fullScreen={isMobile}>
        {job && meta ? (
          <ScheduleForm
            initial={formFromJob(job)}
            skills={meta.skills}
            sessionModes={meta.session_modes}
            limits={meta.limits}
            jobId={job.id}
            status={job.status}
            submitLabel="Save"
            onCancel={() => setEditing(false)}
            onSubmit={async (form) => {
              const changes = changesFromForm(job, form);
              if (Object.keys(changes).length > 0) await api.updateSchedule(job.id, changes, job.version);
              setEditing(false);
              reload();
            }}
          />
        ) : null}
      </Modal>

      <Modal opened={confirm !== null} onClose={() => setConfirm(null)} title={confirm?.title} centered>
        <Stack gap="md">
          <Text size="sm">{confirm?.body}</Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setConfirm(null)} disabled={acting}>
              Cancel
            </Button>
            <Button
              color={confirm?.color}
              loading={acting}
              onClick={() =>
                confirm &&
                void act(confirm.label, async () => {
                  await confirm.run();
                  setConfirm(null);
                })
              }
            >
              {confirm?.label}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
