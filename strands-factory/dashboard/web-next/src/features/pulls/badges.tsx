/** Small presentational atoms shared by the PRs list + detail: state / decision / CI / labels / ±. */
import { Avatar, Badge, Group, Text } from "@mantine/core";
import type { CiState, PullState, ReviewDecision, ReviewState } from "./data";
import classes from "./Pull.module.css";

export function StateBadge({ state }: { state: PullState }) {
  const color = state === "open" ? "frog" : state === "merged" ? "violet" : state === "closed" ? "red" : "gray";
  const label = state === "open" ? "Open" : state === "merged" ? "Merged" : state === "closed" ? "Closed" : "Draft";
  return (
    <Badge color={color} variant="light" radius="sm" size="sm">
      {label}
    </Badge>
  );
}

export function DecisionBadge({ decision }: { decision: ReviewDecision }) {
  if (!decision) return null;
  const [color, label] =
    decision === "APPROVED"
      ? ["frog", "Approved"]
      : decision === "CHANGES_REQUESTED"
        ? ["red", "Changes requested"]
        : ["yellow", "Review required"];
  return (
    <Badge color={color} variant="light" radius="sm" size="sm">
      {label}
    </Badge>
  );
}

export function reviewStateLabel(state: ReviewState | "REQUESTED"): string {
  switch (state) {
    case "APPROVED":
      return "Approved";
    case "CHANGES_REQUESTED":
      return "Changes requested";
    case "COMMENTED":
      return "Commented";
    case "DISMISSED":
      return "Dismissed";
    case "REQUESTED":
      return "Requested";
    default:
      return "Pending";
  }
}

export function reviewStateColor(state: ReviewState | "REQUESTED"): string {
  switch (state) {
    case "APPROVED":
      return "frog";
    case "CHANGES_REQUESTED":
      return "red";
    case "REQUESTED":
      return "yellow";
    default:
      return "gray";
  }
}

export function ReviewStateBadge({ state }: { state: ReviewState | "REQUESTED" }) {
  return (
    <Badge color={reviewStateColor(state)} variant="light" radius="sm" size="sm">
      {reviewStateLabel(state)}
    </Badge>
  );
}

/** Check-run / status-context outcome → badge color. */
export function checkColor(status: string): string {
  switch (status) {
    case "success":
      return "frog";
    case "failure":
    case "error":
    case "timed_out":
    case "action_required":
    case "startup_failure":
      return "red";
    case "pending":
    case "expected":
    case "in_progress":
    case "queued":
      return "yellow";
    default:
      return "gray"; // neutral / skipped / cancelled / stale
  }
}

export function CheckBadge({ status }: { status: string }) {
  return (
    <Badge color={checkColor(status)} variant="light" radius="sm" size="sm">
      {status.replace(/_/g, " ")}
    </Badge>
  );
}

export function CiDot({ state, title }: { state: CiState; title?: string }) {
  if (!state) return null;
  const cls = state === "success" ? classes.dotSuccess : state === "failure" ? classes.dotFailure : classes.dotPending;
  return <span className={`${classes.dot} ${cls}`} title={title ?? `CI ${state}`} aria-label={`CI ${state}`} />;
}

export function DiffStat({ additions, deletions }: { additions: number; deletions: number }) {
  return (
    <span className={classes.stat}>
      <span className={classes.statAdd}>+{additions}</span> <span className={classes.statDel}>−{deletions}</span>
    </span>
  );
}

export function Who({ login, avatarUrl, size = 18 }: { login: string; avatarUrl?: string; size?: number }) {
  return (
    <Group gap={4} wrap="nowrap" style={{ minWidth: 0 }}>
      <Avatar src={avatarUrl || undefined} size={size} radius="xl" alt="">
        {login.slice(0, 1).toUpperCase()}
      </Avatar>
      <Text size="sm" c="dimmed" className="truncate" span>
        {login}
      </Text>
    </Group>
  );
}
