/**
 * /issues/:owner/:repo/:number — one issue: title, meta, body, comments, and the maintainer's own
 * quick actions (assign me / labels / close-reopen / comment) plus the Strandly shortcut.
 *
 * Writes are the USER's decisions under the user's token (never AI text). Each write is optimistic:
 * patch local state, call GitHub, on failure revert + notify; on success refetch quietly.
 */
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActionIcon,
  Anchor,
  Avatar,
  Badge,
  Button,
  Checkbox,
  Group,
  Paper,
  Popover,
  ScrollArea,
  Stack,
  Text,
  Textarea,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import {
  IconArrowLeft,
  IconCircleCheck,
  IconExternalLink,
  IconFlag,
  IconMessageCircle,
  IconRefresh,
  IconRotateClockwise,
  IconSend,
  IconTag,
  IconUserPlus,
} from "@tabler/icons-react";
import { useNavigate, useParams } from "react-router-dom";
import { useApi } from "../../api/ApiContext";
import { Markdown } from "../../components/Markdown";
import { NoTokenState } from "../../components/NoTokenState";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlock";
import { useGitHub } from "../../github/GitHubContext";
import { relTime, relTimeAgo } from "../../lib/format";
import { itemSessionLink, ROUTES } from "../../lib/links";
import { useBack } from "../../lib/useBack";
import { itemToSessionId } from "../../lib/sessionId";
import type { WorkProduct } from "../../types/workProduct";
import {
  fetchEarlierComments,
  fetchIssueDetail,
  ISSUE_COMMENTS_PAGE,
  isNotFoundError,
  mergeRefreshedDetail,
  setIssueState,
  withEarlierComments,
  type GhLabel,
  type IssueComment,
  type IssueDetail,
} from "./data";
import { LabelDot, LabelPill } from "../../components/LabelPill";
import classes from "./issues.module.css";

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function fail(title: string, e: unknown) {
  notifications.show({ color: "red", title, message: errMsg(e) });
}

/** True once the window has scrolled past `px`. State flips only at the threshold (not per frame). */
function useScrolledPast(px: number): boolean {
  const [past, setPast] = useState(() => typeof window !== "undefined" && window.scrollY > px);
  useEffect(() => {
    const onScroll = () => setPast(window.scrollY > px);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [px]);
  return past;
}

export function IssuePage() {
  const { owner = "", repo: name = "", number: numStr = "" } = useParams();
  const number = /^\d+$/.test(numStr) ? Number(numStr) : NaN;
  const validRoute = !!owner && !!name && Number.isInteger(number) && number > 0;
  const repo = `${owner}/${name}`;
  const navigate = useNavigate();
  const goBack = useBack(ROUTES.issues);
  const api = useApi();
  const { client, viewer, isMock } = useGitHub();
  const unavailable = !client || isMock;

  const [loaded, setLoaded] = useState<IssueDetail | null>(null);
  // Only a detail for THIS route is rendered or written against: `loaded` can still hold the previous
  // issue for a frame after navigation (the reset in `load` runs post-paint).
  const issue = loaded && loaded.repo === repo && loaded.number === number ? loaded : null;
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const reqId = useRef(0);
  // The routed identity as of the latest render — what an async continuation checks it still owns.
  const routeRef = useRef(`${repo}#${number}`);
  routeRef.current = `${repo}#${number}`;

  /**
   * @param quiet keep the rendered issue while refetching (after a write / Refresh) instead of
   *   dropping it; @param fresh bypass the GitHub read cache (the Refresh button).
   */
  const load = useCallback(
    (quiet: boolean, fresh = false) => {
      const id = ++reqId.current;
      if (!quiet) {
        // A new issue (route change): drop the previous one so its body/comments don't show under
        // the new ref while this one loads, and forget any earlier-page load it had in flight.
        setLoaded(null);
        setNotFound(false);
        setLoadingEarlier(false);
      }
      setLoading(true);
      setError(null);
      if (!validRoute) {
        setNotFound(true);
        setLoading(false);
        return;
      }
      if (!client || unavailable) {
        setLoading(false);
        return;
      }
      fetchIssueDetail(client, owner, name, number, { fresh })
        .then((d) => {
          if (id !== reqId.current) return;
          setNotFound(d === null);
          // A quiet refresh re-reads only the newest comments page; keep earlier pages already loaded.
          if (d) setLoaded((prev) => (quiet ? mergeRefreshedDetail(prev, d) : d));
        })
        .catch((e: unknown) => {
          if (id !== reqId.current) return;
          // GitHub pairs `issue: null` with an error for a bad number — that's "not found", not a fault.
          if (isNotFoundError(errMsg(e))) setNotFound(true);
          else setError(errMsg(e));
        })
        .finally(() => id === reqId.current && setLoading(false));
    },
    [client, unavailable, validRoute, owner, name, number],
  );
  useEffect(() => load(false), [load]);

  /** After any write: refetch quietly (newest comments page only; earlier pages stay loaded). */
  const refresh = useCallback(() => load(true), [load]);

  // "Strandly's take" — optional; never blocks the page.
  const [take, setTake] = useState<WorkProduct | null>(null);
  useEffect(() => {
    let live = true;
    setTake(null);
    if (!validRoute) return;
    api
      .getTriageForSession(itemToSessionId(repo, "issue", number))
      .then((wp) => live && wp && setTake(wp))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [api, validRoute, number, repo]);

  const compact = useScrolledPast(72);

  // "Load N earlier comments": the page before the oldest loaded one (GraphQL `before:` cursor),
  // ISSUE_COMMENTS_PAGE at a time, merged by id INTO THE CURRENT issue. Owned by the issue identity,
  // not the detail request id: a quiet refresh after a write (same issue) must not strand the flag or
  // drop the page; only navigating to another issue does.
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  async function loadEarlier() {
    if (!client || !issue?.earlierCursor || loadingEarlier) return;
    const key = `${issue.repo}#${issue.number}`;
    setLoadingEarlier(true);
    try {
      const page = await fetchEarlierComments(client, owner, name, number, issue.earlierCursor);
      // Stitch into the CURRENT detail of the same issue (a quiet refresh may have replaced it).
      setLoaded((cur) => (cur && `${cur.repo}#${cur.number}` === key ? withEarlierComments(cur, page) : cur));
    } catch (e) {
      if (routeRef.current === key) fail("Couldn't load earlier comments", e);
    } finally {
      if (routeRef.current === key) setLoadingEarlier(false);
    }
  }

  // --- Writes (optimistic) ----------------------------------------------------------------------

  const [busy, setBusy] = useState<string | null>(null);

  async function assignMe() {
    if (!client || !viewer || !issue) return;
    if (issue.assignees.some((a) => a.login === viewer.login)) return;
    const prev = issue;
    setLoaded({ ...issue, assignees: [...issue.assignees, { login: viewer.login, avatarUrl: "" }] });
    setBusy("assign");
    try {
      await client.assign(repo, number, [viewer.login]);
      refresh();
    } catch (e) {
      setLoaded(prev);
      fail("Couldn't assign", e);
    } finally {
      setBusy(null);
    }
  }

  async function toggleLabel(label: GhLabel, on: boolean) {
    if (!client || !issue) return;
    const prev = issue;
    setLoaded({
      ...issue,
      labels: on ? [...issue.labels, label] : issue.labels.filter((l) => l.name !== label.name),
    });
    try {
      if (on) await client.addLabels(repo, number, [label.name]);
      else await client.removeLabel(repo, number, label.name);
      refresh();
    } catch (e) {
      setLoaded(prev);
      fail(on ? "Couldn't add label" : "Couldn't remove label", e);
    }
  }

  async function toggleState() {
    if (!client || !issue) return;
    const prev = issue;
    const next = issue.state === "OPEN" ? "closed" : "open";
    setLoaded({ ...issue, state: next === "open" ? "OPEN" : "CLOSED" });
    setBusy("state");
    try {
      await setIssueState(client, repo, number, next);
      refresh();
    } catch (e) {
      setLoaded(prev);
      fail(next === "closed" ? "Couldn't close" : "Couldn't reopen", e);
    } finally {
      setBusy(null);
    }
  }

  const [draft, setDraft] = useState("");
  async function send() {
    const body = draft.trim();
    if (!client || !issue || !body) return;
    const prev = issue;
    const local: IssueComment = {
      id: `local-${Date.now()}`,
      author: viewer ? { login: viewer.login, avatarUrl: "" } : null,
      body,
      createdAt: new Date().toISOString(),
      url: issue.url,
    };
    setLoaded({ ...issue, comments: [...issue.comments, local], commentsTotal: issue.commentsTotal + 1 });
    setDraft("");
    setBusy("comment");
    try {
      await client.postComment(repo, number, body);
      refresh();
    } catch (e) {
      setLoaded(prev);
      setDraft(body);
      fail("Couldn't post comment", e);
    } finally {
      setBusy(null);
    }
  }

  function openInStrandly() {
    navigate(itemSessionLink(repo, "issue", number), {
      state: { viewing: { type: "issue", repo, number, url: issue?.url ?? `https://github.com/${repo}/issues/${number}` } },
    });
  }

  // --- Render -----------------------------------------------------------------------------------

  const ref = `${repo}#${number}`;
  const isOpen = issue?.state !== "CLOSED";
  const assignedToMe = !!viewer && !!issue?.assignees.some((a) => a.login === viewer.login);

  return (
    <Stack gap="md">
      <div className={classes.sticky}>
        <ActionIcon variant="subtle" color="gray" size={40} aria-label="Back" onClick={goBack}>
          <IconArrowLeft size={20} />
        </ActionIcon>
        <Text size="sm" c="dimmed" className="tabular" component="span" style={{ display: "flex", minWidth: 0 }}>
          <span className="truncate">{repo}</span>
          <span style={{ flexShrink: 0 }}>#{number}</span>
        </Text>
        {issue ? <StateBadge open={isOpen} /> : null}
        {compact && issue ? <span className={classes.stickyTitle}>{issue.title}</span> : null}
        {!unavailable && validRoute ? (
          <Tooltip label="Refresh">
            <ActionIcon
              variant="subtle"
              color="gray"
              size={40}
              aria-label="Refresh"
              style={{ marginLeft: "auto" }}
              loading={loading && !!issue}
              onClick={() => load(true, true)}
            >
              <IconRefresh size={18} />
            </ActionIcon>
          </Tooltip>
        ) : null}
      </div>

      {unavailable ? (
        <NoTokenState message="Add a GitHub token to read and act on issues." />
      ) : loading && !issue ? (
        <LoadingBlock />
      ) : error && !issue ? (
        <ErrorBlock error={error} />
      ) : notFound ? (
        <EmptyBlock label={`${ref} isn't an issue we can see.`} />
      ) : issue ? (
        <>
          <Stack gap="xs">
            <Title order={2} style={{ lineHeight: 1.25, overflowWrap: "anywhere" }}>
              {issue.title}
            </Title>
            <Group gap={10} wrap="wrap" align="center">
              {issue.author ? (
                <Group gap={6} wrap="nowrap">
                  <Avatar src={issue.author.avatarUrl} alt="" size={20} radius="xl" />
                  <Text size="sm">{issue.author.login}</Text>
                </Group>
              ) : null}
              <Text size="sm" c="dimmed">
                opened {relTimeAgo(issue.createdAt)}
              </Text>
              {issue.commentsTotal ? (
                <Group gap={3} wrap="nowrap">
                  <IconMessageCircle size={14} stroke={1.75} style={{ opacity: 0.7 }} />
                  <Text size="sm" c="dimmed" className="tabular">
                    {issue.commentsTotal}
                  </Text>
                </Group>
              ) : null}
              {issue.milestone ? (
                <Group gap={4} wrap="nowrap">
                  <IconFlag size={14} stroke={1.75} style={{ opacity: 0.7 }} />
                  <Text size="sm" c="dimmed">
                    {issue.milestone}
                  </Text>
                </Group>
              ) : null}
            </Group>
            {issue.labels.length || issue.assignees.length ? (
              <Group gap={6} wrap="wrap" align="center">
                {issue.labels.map((l) => (
                  <LabelPill key={l.name} label={l} variant="filled" />
                ))}
                {issue.assignees.length ? (
                  <Avatar.Group spacing={6} ml={issue.labels.length ? 4 : 0}>
                    {issue.assignees.map((a) => (
                      <Tooltip key={a.login} label={`Assigned: ${a.login}`}>
                        <Avatar src={a.avatarUrl || undefined} alt={a.login} size={24} radius="xl">
                          {a.login.slice(0, 2).toUpperCase()}
                        </Avatar>
                      </Tooltip>
                    ))}
                  </Avatar.Group>
                ) : null}
              </Group>
            ) : null}
          </Stack>

          {error ? <ErrorBlock error={error} /> : null}

          <Group gap={8} wrap="wrap">
            <Button h={40} radius="xl" leftSection={<IconMessageCircle size={16} />} onClick={openInStrandly}>
              Open in Strandly
            </Button>
            <Button
              h={40} radius="xl"
              variant="light"
              color="gray"
              leftSection={<IconUserPlus size={16} />}
              onClick={assignMe}
              disabled={!viewer || assignedToMe}
              loading={busy === "assign"}
            >
              {assignedToMe ? "Assigned to me" : "Assign me"}
            </Button>
            <LabelPicker all={issue.repoLabels} selected={issue.labels} onToggle={toggleLabel} />
            <Button
              h={40} radius="xl"
              variant="light"
              color={isOpen ? "red" : "frog"}
              leftSection={isOpen ? <IconCircleCheck size={16} /> : <IconRotateClockwise size={16} />}
              onClick={toggleState}
              loading={busy === "state"}
            >
              {isOpen ? "Close" : "Reopen"}
            </Button>
            <Button
              h={40} radius="xl"
              variant="subtle"
              color="gray"
              component="a"
              href={issue.url}
              target="_blank"
              rel="noopener noreferrer"
              rightSection={<IconExternalLink size={14} />}
            >
              Open on GitHub
            </Button>
          </Group>

          {take ? <StrandlyTake wp={take} /> : null}

          <Paper p="md" className={classes.body}>
            {issue.body.trim() ? (
              <Markdown text={issue.body} />
            ) : (
              <Text c="dimmed" size="sm" fs="italic">
                No description provided.
              </Text>
            )}
          </Paper>

          {issue.comments.length ? (
            <Stack gap={8}>
              {issue.earlierCursor ? (
                <Button
                  variant="light"
                  color="gray"
                  radius="xl"
                  h={40}
                  loading={loadingEarlier}
                  onClick={() => void loadEarlier()}
                  style={{ alignSelf: "center" }}
                >
                  Load {Math.min(ISSUE_COMMENTS_PAGE, Math.max(1, issue.commentsTotal - issue.comments.length))} earlier
                  {issue.commentsTotal - issue.comments.length === 1 ? " comment" : " comments"}
                  {issue.commentsTotal > issue.comments.length ? ` · ${issue.commentsTotal - issue.comments.length} not shown` : ""}
                </Button>
              ) : issue.commentsTotal > issue.comments.length ? (
                <Text size="sm" c="dimmed" ta="center">
                  {issue.commentsTotal - issue.comments.length} earlier{" "}
                  {issue.commentsTotal - issue.comments.length === 1 ? "comment" : "comments"} —{" "}
                  <Anchor href={issue.url} target="_blank" rel="noopener noreferrer" size="sm">
                    read on GitHub
                  </Anchor>
                </Text>
              ) : null}
              {issue.comments.map((c) => (
                <CommentCard key={c.id} c={c} />
              ))}
            </Stack>
          ) : null}

          <Paper p="md">
            <Stack gap="sm">
              <Textarea
                autosize
                minRows={3}
                placeholder={viewer ? `Comment as ${viewer.login}…` : "Comment…"}
                value={draft}
                onChange={(e) => setDraft(e.currentTarget.value)}
                aria-label="Comment"
              />
              <Group justify="flex-end">
                <Button
                  h={40}
                  leftSection={<IconSend size={16} />}
                  onClick={send}
                  disabled={!draft.trim()}
                  loading={busy === "comment"}
                >
                  Send
                </Button>
              </Group>
            </Stack>
          </Paper>
        </>
      ) : null}
    </Stack>
  );
}

function StateBadge({ open }: { open: boolean }) {
  return (
    <Badge color={open ? "frog" : "violet"} variant="light" radius="sm" size="sm" style={{ flexShrink: 0 }}>
      {open ? "Open" : "Closed"}
    </Badge>
  );
}

const CommentCard = memo(function CommentCard({ c }: { c: IssueComment }) {
  return (
    <Paper p="md" className={classes.body}>
      <Stack gap={6}>
        <Group gap={8} wrap="nowrap" style={{ minWidth: 0 }}>
          <Avatar src={c.author?.avatarUrl || undefined} alt="" size={24} radius="xl">
            {(c.author?.login ?? "?").slice(0, 2).toUpperCase()}
          </Avatar>
          <Text size="sm" fw={600} className="truncate">
            {c.author?.login ?? "ghost"}
          </Text>
          <Anchor
            href={c.url}
            target="_blank"
            rel="noopener noreferrer"
            size="sm"
            c="dimmed"
            ml="auto"
            style={{ flex: "0 0 auto" }}
            title={new Date(c.createdAt).toLocaleString()}
          >
            {relTime(c.createdAt)}
          </Anchor>
        </Group>
        <Markdown text={c.body} />
      </Stack>
    </Paper>
  );
});

function StrandlyTake({ wp }: { wp: WorkProduct }) {
  return (
    <Paper p="sm" style={{ background: "var(--portal-surface-2)" }}>
      <Group gap={8} align="flex-start" wrap="nowrap">
        <IconMessageCircle size={16} stroke={1.75} style={{ opacity: 0.7, marginTop: 2, flex: "0 0 auto" }} />
        <Stack gap={2} style={{ minWidth: 0 }}>
          <Group gap={6}>
            <Text size="sm" c="dimmed" fw={600}>
              Strandly's take
            </Text>
            <Badge size="xs" variant="light" color="gray" radius="sm">
              {wp.verdict}
            </Badge>
            {wp.priority ? (
              <Badge size="xs" variant="light" color="gray" radius="sm">
                {wp.priority}
              </Badge>
            ) : null}
          </Group>
          <Text size="sm" c="dimmed" style={{ overflowWrap: "anywhere" }}>
            {wp.tldr}
          </Text>
        </Stack>
      </Group>
    </Paper>
  );
}

function LabelPicker({
  all,
  selected,
  onToggle,
}: {
  all: GhLabel[];
  selected: GhLabel[];
  onToggle: (label: GhLabel, on: boolean) => void;
}) {
  const [q, setQ] = useState("");
  const on = useMemo(() => new Set(selected.map((l) => l.name)), [selected]);
  const shown = useMemo(() => {
    const t = q.trim().toLowerCase();
    const list = t ? all.filter((l) => l.name.toLowerCase().includes(t)) : all;
    return [...list].sort((a, b) => Number(on.has(b.name)) - Number(on.has(a.name)) || a.name.localeCompare(b.name));
  }, [all, q, on]);
  return (
    <Popover width={280} position="bottom-start" shadow="md" withinPortal>
      <Popover.Target>
        <Button h={40} radius="xl" variant="light" color="gray" leftSection={<IconTag size={16} />}>
          Labels{selected.length ? ` · ${selected.length}` : ""}
        </Button>
      </Popover.Target>
      <Popover.Dropdown p="xs" style={{ border: "none", background: "var(--portal-surface)" }}>
        <Stack gap="xs">
          <TextInput size="sm" placeholder="Filter labels" value={q} onChange={(e) => setQ(e.currentTarget.value)} aria-label="Filter labels" />
          <ScrollArea.Autosize mah={280} type="auto">
            <Stack gap={2}>
              {shown.length === 0 ? (
                <Text size="sm" c="dimmed" p="xs">
                  No labels.
                </Text>
              ) : (
                shown.map((l) => (
                  <Checkbox
                    key={l.name}
                    checked={on.has(l.name)}
                    onChange={(e) => onToggle(l, e.currentTarget.checked)}
                    label={
                      <Group gap={6} wrap="nowrap">
                        <LabelDot color={l.color} />
                        <span style={{ overflowWrap: "anywhere" }}>{l.name}</span>
                      </Group>
                    }
                    styles={{ root: { padding: "10px 6px", borderRadius: 8 }, body: { alignItems: "center" } }}
                  />
                ))
              )}
            </Stack>
          </ScrollArea.Autosize>
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}
