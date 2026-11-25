import { ActionIcon, Avatar, Badge, Tooltip } from "@mantine/core";
import {
  IconCircleDot,
  IconExternalLink,
  IconGitPullRequest,
  IconGitPullRequestDraft,
  IconMessageCircle,
} from "@tabler/icons-react";
import type { KeyboardEvent, MouseEvent } from "react";
import { relTimeAgo } from "../../lib/format";
import { REASON_META, REASONS, type TodayRow as Row } from "./data";
import classes from "./Today.module.css";

export function TodayRow({ row, onOpen }: { row: Row; onOpen: (row: Row) => void }) {
  const draft = row.kind === "pr" && row.isDraft;
  const Icon = row.kind === "issue" ? IconCircleDot : draft ? IconGitPullRequestDraft : IconGitPullRequest;
  const kindClass = draft ? classes.kindDraft : row.kind === "issue" ? classes.kindIssue : classes.kindPr;
  const kindLabel = row.kind === "issue" ? "Issue" : draft ? "Draft pull request" : "Pull request";

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onOpen(row);
    }
  };
  const stop = (e: MouseEvent) => e.stopPropagation();

  return (
    <div
      className={classes.row}
      role="link"
      tabIndex={0}
      aria-label={`${kindLabel}: ${row.title} (${row.repo}#${row.number})`}
      onClick={() => onOpen(row)}
      onKeyDown={onKey}
    >
      <span className={`${classes.kind} ${kindClass}`} title={kindLabel}>
        <Icon size={20} stroke={1.75} />
      </span>

      <div className={classes.body}>
        <div className={classes.title}>{row.title}</div>

        <div className={classes.meta}>
          <span className={classes.ref}>
            {row.repo}#{row.number}
          </span>
          {row.author ? (
            <span className={classes.author}>
              <Avatar src={row.author.avatarUrl} alt="" size="sm" radius="xl" />
              <span className={classes.authorLogin}>{row.author.login}</span>
            </span>
          ) : null}
          <span className={classes.updated} title={new Date(row.updatedAt).toLocaleString()}>
            updated {relTimeAgo(row.updatedAt)}
          </span>
          {row.comments > 0 ? (
            <span className={classes.comments} title={`${row.comments} comments`}>
              <IconMessageCircle size={14} stroke={1.75} />
              {row.comments}
            </span>
          ) : null}
        </div>

        <div className={classes.chips}>
          {REASONS.filter((r) => row.reasons.has(r)).map((r) => (
            <Badge key={r} size="sm" variant="light" color={REASON_META[r].color} radius="sm">
              {REASON_META[r].label}
            </Badge>
          ))}
        </div>
      </div>

      <Tooltip label="Open on GitHub" withArrow>
        <ActionIcon
          component="a"
          href={row.url}
          target="_blank"
          rel="noopener noreferrer"
          variant="subtle"
          color="gray"
          size="lg"
          className={classes.open}
          aria-label="Open on GitHub"
          onClick={stop}
        >
          <IconExternalLink size={16} />
        </ActionIcon>
      </Tooltip>
    </div>
  );
}
