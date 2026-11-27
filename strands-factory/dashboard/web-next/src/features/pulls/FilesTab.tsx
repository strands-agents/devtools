/**
 * Files tab — REST `/pulls/:n/files` (paged to 300) rendered as per-file collapsible cards over
 * `DiffView`. Summary bar: count, ±, expand/collapse all, Wrap switch (defaults ON on phones).
 * Optional inline line comment (user's own words) when the viewer has a token + we know the head sha.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, Button, Group, Stack, Switch, Text, Textarea } from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import { notifications } from "@mantine/notifications";
import { IconChevronDown, IconChevronRight, IconExternalLink } from "@tabler/icons-react";
import type { GitHubClient } from "../../github/types";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlock";
import { fetchPullFiles, postLineComment, type FileStatus, type PullFile } from "./data";
import { splitPath, type DiffLine } from "./diff";
import { DiffView } from "./DiffView";
import { DiffStat } from "./badges";
import classes from "./Pull.module.css";

const COLLAPSE_OVER = 20;

function statusColor(s: FileStatus): string {
  switch (s) {
    case "added":
    case "copied":
      return "frog";
    case "removed":
      return "red";
    case "renamed":
      return "violet";
    default:
      return "gray";
  }
}

interface InlineTarget {
  index: number;
  line: number;
  side: "LEFT" | "RIGHT";
}

function InlineComposer({ target, onCancel, onSubmit, busy }: { target: InlineTarget; onCancel: () => void; onSubmit: (body: string) => void; busy: boolean }) {
  const [body, setBody] = useState("");
  return (
    <div className={classes.inlineComposer} onClick={(e) => e.stopPropagation()}>
      <Stack gap={6}>
        <Text size="xs" c="dimmed">
          Comment on {target.side === "LEFT" ? "old" : "new"} line {target.line}
        </Text>
        <Textarea
          value={body}
          onChange={(e) => setBody(e.currentTarget.value)}
          autosize
          minRows={2}
          maxRows={8}
          placeholder="Your comment (Markdown)…"
          autoFocus
          size="sm"
        />
        <Group gap={6} justify="flex-end">
          <Button variant="subtle" color="gray" size="xs" h={40} onClick={onCancel}>
            Cancel
          </Button>
          <Button size="xs" h={40} disabled={!body.trim()} loading={busy} onClick={() => onSubmit(body.trim())}>
            Comment
          </Button>
        </Group>
      </Stack>
    </div>
  );
}

function FileCard({
  file,
  open,
  onToggle,
  wrap,
  onLineComment,
}: {
  file: PullFile;
  open: boolean;
  onToggle: () => void;
  wrap: boolean;
  onLineComment?: (path: string, line: number, side: "LEFT" | "RIGHT", body: string) => Promise<void>;
}) {
  const { dir, base } = splitPath(file.filename);
  const [target, setTarget] = useState<InlineTarget | null>(null);
  const [busy, setBusy] = useState(false);

  const onLineClick = useCallback((l: DiffLine, index: number) => {
    setTarget((cur) => {
      if (cur?.index === index) return null;
      if (l.type === "del") return l.oldNo ? { index, line: l.oldNo, side: "LEFT" } : null;
      return l.newNo ? { index, line: l.newNo, side: "RIGHT" } : null;
    });
  }, []);

  const submit = async (body: string) => {
    if (!target || !onLineComment) return;
    setBusy(true);
    try {
      await onLineComment(file.filename, target.line, target.side, body);
      setTarget(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={classes.fileCard}>
      <button type="button" className={classes.fileHead} onClick={onToggle} aria-expanded={open}>
        {open ? <IconChevronDown size={16} /> : <IconChevronRight size={16} />}
        <span className={classes.filePath}>
          {file.previousFilename ? (
            <>
              {file.previousFilename} → <span className={classes.fileBase}>{file.filename}</span>
            </>
          ) : (
            <>
              {dir}
              <span className={classes.fileBase}>{base}</span>
            </>
          )}
        </span>
        {file.status !== "modified" ? (
          <Badge color={statusColor(file.status)} variant="light" radius="sm" size="xs">
            {file.status}
          </Badge>
        ) : null}
        <DiffStat additions={file.additions} deletions={file.deletions} />
      </button>
      {open ? (
        file.patch ? (
          <DiffView
            patch={file.patch}
            path={file.filename}
            wrap={wrap}
            activeIndex={target?.index ?? null}
            onLineClick={onLineComment ? onLineClick : undefined}
            afterLine={
              target ? <InlineComposer target={target} busy={busy} onCancel={() => setTarget(null)} onSubmit={submit} /> : null
            }
          />
        ) : (
          <div className={classes.noPatch}>
            No text diff (binary or too large).{" "}
            <a href={file.blobUrl} target="_blank" rel="noopener noreferrer">
              View on GitHub <IconExternalLink size={12} style={{ verticalAlign: "-2px" }} />
            </a>
          </div>
        )
      ) : null}
    </div>
  );
}

export function FilesTab({
  client,
  repo,
  number,
  headOid,
  canComment,
}: {
  client: GitHubClient;
  repo: string;
  number: number;
  headOid: string | null;
  canComment: boolean;
}) {
  const [files, setFiles] = useState<PullFile[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openMap, setOpenMap] = useState<Record<string, boolean>>({});
  const narrow = useMediaQuery("(max-width: 48em)", false, { getInitialValueInEffect: false });
  const [wrapPick, setWrapPick] = useState<boolean | null>(null);
  const wrap = wrapPick ?? !!narrow;

  // headOid: a refresh after new commits must refetch the diff, not keep the stale file list. The
  // read-cache entry is keyed by head too (`vary`), so a copy fetched under an older head — even one
  // left by an earlier visit, before this tab mounted — is never shown under the new one.
  useEffect(() => {
    let live = true;
    setFiles(null);
    setError(null);
    fetchPullFiles(client, repo, number, { vary: headOid ?? undefined })
      .then((r) => {
        if (!live) return;
        setFiles(r.files);
        setTruncated(r.truncated);
        setOpenMap({});
      })
      .catch((e) => live && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, [client, repo, number, headOid]);

  const defaultOpen = (files?.length ?? 0) <= COLLAPSE_OVER;
  const isOpen = (f: PullFile) => openMap[f.filename] ?? defaultOpen;
  const setAll = (v: boolean) => setOpenMap(Object.fromEntries((files ?? []).map((f) => [f.filename, v])));

  const totals = useMemo(
    () => (files ?? []).reduce((a, f) => ({ additions: a.additions + f.additions, deletions: a.deletions + f.deletions }), { additions: 0, deletions: 0 }),
    [files],
  );

  const onLineComment =
    canComment && headOid
      ? async (path: string, line: number, side: "LEFT" | "RIGHT", body: string) => {
          try {
            await postLineComment(client, repo, number, { body, commitId: headOid, path, line, side });
            notifications.show({ color: "green", message: `Comment posted on ${splitPath(path).base}:${line}` });
          } catch (e) {
            notifications.show({ color: "red", message: e instanceof Error ? e.message : String(e) });
            throw e;
          }
        }
      : undefined;

  if (error) return <ErrorBlock error={error} />;
  if (!files) return <LoadingBlock label="Loading files…" />;
  if (files.length === 0) return <EmptyBlock label="No changed files." />;

  return (
    <Stack gap="sm">
      <Group justify="space-between" wrap="wrap" gap="xs">
        <Group gap={8} wrap="wrap">
          <Text size="sm" fw={600} className="tabular">
            {files.length} file{files.length === 1 ? "" : "s"}
            {truncated ? "+" : ""}
          </Text>
          <DiffStat additions={totals.additions} deletions={totals.deletions} />
          {truncated ? (
            <Text size="xs" c="dimmed">
              showing first 300
            </Text>
          ) : null}
        </Group>
        <Group gap={6} wrap="nowrap">
          <Button variant="subtle" color="gray" size="xs" h={40} onClick={() => setAll(true)}>
            Expand all
          </Button>
          <Button variant="subtle" color="gray" size="xs" h={40} onClick={() => setAll(false)}>
            Collapse all
          </Button>
          <Switch label="Wrap" size="sm" checked={wrap} onChange={(e) => setWrapPick(e.currentTarget.checked)} styles={{ root: { minHeight: 40, display: "flex", alignItems: "center" } }} />
        </Group>
      </Group>
      {canComment && headOid ? (
        <Text size="xs" c="dimmed">
          Tap a line to comment on it.
        </Text>
      ) : null}
      <Stack gap={8}>
        {files.map((f) => (
          <FileCard
            key={f.sha + f.filename}
            file={f}
            open={isOpen(f)}
            onToggle={() => setOpenMap((m) => ({ ...m, [f.filename]: !isOpen(f) }))}
            wrap={wrap}
            onLineComment={onLineComment}
          />
        ))}
      </Stack>
    </Stack>
  );
}
