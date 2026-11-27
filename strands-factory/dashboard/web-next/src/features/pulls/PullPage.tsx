/**
 * /pulls/:owner/:repo/:number — PR detail built for review on a phone. Sticky compact header, then
 * title + meta, then Conversation | Files | Checks (tab in `?tab=`). A bottom action bar carries the
 * user's own review (Approve / Request changes / Comment → REST reviews). Every write is the user's
 * words under their token; "Open in Strandly" hands the item to the agent chat.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActionIcon,
  Badge,
  Button,
  Drawer,
  Group,
  Paper,
  SegmentedControl,
  Stack,
  Text,
  Textarea,
  Tooltip,
} from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import { notifications } from "@mantine/notifications";
import { IconArrowLeft, IconBrandGithub, IconExternalLink, IconMessageCircle, IconRefresh } from "@tabler/icons-react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useApi } from "../../api/ApiContext";
import type { WorkProduct } from "../../types/workProduct";
import { useGitHub } from "../../github/GitHubContext";
import { relTime, relTimeAgo } from "../../lib/format";
import { itemSessionLink, ROUTES } from "../../lib/links";
import { useBack } from "../../lib/useBack";
import { itemToSessionId } from "../../lib/sessionId";
import { Markdown } from "../../components/Markdown";
import { ErrorBlock, LoadingBlock } from "../../components/StateBlock";
import {
  fetchEarlierPages,
  fetchPullDetail,
  hasEarlier,
  mergeRefreshedConversation,
  notLoadedCount,
  submitReview,
  withConversation,
  withEarlierConversation,
  type PullDetail,
  type ReviewEvent,
  type TimelineItem,
} from "./data";
import { LabelPills } from "../../components/LabelPill";
import { CheckBadge, CiDot, DecisionBadge, DiffStat, ReviewStateBadge, StateBadge, Who, checkColor } from "./badges";
import { FilesTab } from "./FilesTab";
import { NoTokenState } from "../../components/NoTokenState";
import classes from "./Pull.module.css";

type Tab = "conversation" | "files" | "checks";
const TABS: Tab[] = ["conversation", "files", "checks"];

function useTab(): [Tab, (t: Tab) => void] {
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  const tab: Tab = (TABS as string[]).includes(raw ?? "") ? (raw as Tab) : "conversation";
  const set = useCallback(
    (t: Tab) => {
      const next = new URLSearchParams(params);
      if (t === "conversation") next.delete("tab");
      else next.set("tab", t);
      setParams(next, { replace: true });
    },
    [params, setParams],
  );
  return [tab, set];
}

// --- Conversation -------------------------------------------------------------------------------

/** Mergeability as GitHub reports it; UNKNOWN (still computing) renders nothing in the header. */
function MergeableBadge({ mergeable, showUnknown = false }: { mergeable: PullDetail["mergeable"]; showUnknown?: boolean }) {
  if (mergeable === "CONFLICTING")
    return (
      <Badge color="red" variant="light" radius="sm" size="sm">
        Conflicts
      </Badge>
    );
  if (mergeable === "MERGEABLE")
    return (
      <Badge color="frog" variant="light" radius="sm" size="sm">
        Mergeable
      </Badge>
    );
  return showUnknown ? (
    <Text size="xs" c="dimmed">
      mergeability pending
    </Text>
  ) : null;
}

function Entry({ item }: { item: TimelineItem }) {
  return (
    <div className={classes.entry}>
      <div className={classes.entryHead}>
        {item.author ? <Who login={item.author.login} avatarUrl={item.author.avatarUrl} /> : <span>ghost</span>}
        {item.kind === "review" && item.state ? <ReviewStateBadge state={item.state} /> : null}
        <span>{relTime(item.at)}</span>
        {item.url ? (
          <ActionIcon
            component="a"
            href={item.url}
            target="_blank"
            rel="noopener noreferrer"
            variant="subtle"
            color="gray"
            size={32}
            my={-8}
            ml="auto"
            aria-label="Open on GitHub"
          >
            <IconExternalLink size={14} />
          </ActionIcon>
        ) : null}
      </div>
      {item.body.trim() ? <Markdown text={item.body} /> : null}
    </div>
  );
}

function StrandlyReviewCard({ repo, number }: { repo: string; number: number }) {
  const api = useApi();
  const [wp, setWp] = useState<WorkProduct | null>(null);
  useEffect(() => {
    let live = true;
    api
      .getReviewForSession(itemToSessionId(repo, "pr", number))
      .then((r) => live && setWp(r))
      .catch(() => {
        /* optional card — swallow */
      });
    return () => {
      live = false;
    };
  }, [api, repo, number]);
  if (!wp) return null;
  const color = wp.verdict === "approve" ? "frog" : wp.verdict === "changes-requested" ? "red" : "gray";
  return (
    <Paper p="md">
      <Stack gap={6}>
        <Group gap={8} wrap="wrap">
          <Text fw={650} size="sm">
            Strandly's review
          </Text>
          <Badge color={color} variant="light" radius="sm" size="sm">
            {String(wp.verdict).replace(/-/g, " ")}
          </Badge>
          <Text size="xs" c="dimmed">
            {wp.status} · {relTime(wp.updated_at)}
          </Text>
        </Group>
        {wp.tldr ? <Markdown text={wp.tldr} /> : null}
      </Stack>
    </Paper>
  );
}

function Composer({ onSubmit, disabled }: { onSubmit: (body: string) => Promise<void>; disabled: boolean }) {
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const send = async () => {
    const b = body.trim();
    if (!b) return;
    setBusy(true);
    try {
      await onSubmit(b);
      setBody("");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Paper p="sm">
      <Stack gap={6}>
        <Textarea
          value={body}
          onChange={(e) => setBody(e.currentTarget.value)}
          placeholder={disabled ? "Add a GitHub token to comment" : "Leave a comment (Markdown)…"}
          autosize
          minRows={2}
          maxRows={10}
          disabled={disabled}
          size="sm"
        />
        <Group justify="flex-end">
          <Button size="xs" h={40} variant="light" disabled={disabled || !body.trim()} loading={busy} onClick={send}>
            Comment
          </Button>
        </Group>
      </Stack>
    </Paper>
  );
}

function ConversationTab({
  pr,
  onComment,
  canWrite,
  onLoadEarlier,
  loadingEarlier,
}: {
  pr: PullDetail;
  onComment: (body: string) => Promise<void>;
  canWrite: boolean;
  onLoadEarlier: () => void;
  loadingEarlier: boolean;
}) {
  const notLoaded = notLoadedCount(pr.conversation);
  const navigate = useNavigate();
  const openInStrandly = () =>
    navigate(itemSessionLink(pr.repo, "pr", pr.number), {
      state: { viewing: { type: "pr", repo: pr.repo, number: pr.number, url: pr.url } },
    });
  return (
    <Stack gap="sm">
      <Group gap={8} wrap="wrap">
        <Button leftSection={<IconMessageCircle size={16} />} onClick={openInStrandly} h={40} radius="xl">
          Open in Strandly
        </Button>
        <Button
          component="a"
          href={pr.url}
          target="_blank"
          rel="noopener noreferrer"
          variant="light"
          color="gray"
          leftSection={<IconBrandGithub size={16} />}
          h={40}
          radius="xl"
        >
          Open on GitHub
        </Button>
      </Group>
      <StrandlyReviewCard repo={pr.repo} number={pr.number} />
      <Paper p="md">
        {pr.body.trim() ? (
          <Markdown text={pr.body} />
        ) : (
          <Text size="sm" c="dimmed">
            No description.
          </Text>
        )}
      </Paper>
      {hasEarlier(pr.conversation) ? (
        // The newest PULL_CONVERSATION_PAGE comments + reviews are loaded; this walks `before:`
        // cursors one page at a time (README.md "Pagination & limits").
        <Button
          variant="light"
          color="gray"
          radius="xl"
          h={40}
          loading={loadingEarlier}
          onClick={onLoadEarlier}
          style={{ alignSelf: "center" }}
        >
          Load earlier{notLoaded > 0 ? ` · ${notLoaded} not shown` : ""}
        </Button>
      ) : null}
      {pr.timeline.length ? (
        <Stack gap={8}>
          {pr.timeline.map((t) => (
            <Entry key={t.id} item={t} />
          ))}
        </Stack>
      ) : (
        <Text size="sm" c="dimmed" ta="center" py="sm">
          No comments yet.
        </Text>
      )}
      <Composer onSubmit={onComment} disabled={!canWrite} />
    </Stack>
  );
}

// --- Checks ---------------------------------------------------------------------------------------

function ChecksTab({ pr }: { pr: PullDetail }) {
  return (
    <Stack gap="sm">
      <Group gap={8}>
        <Text fw={650} size="sm">
          Overall
        </Text>
        {pr.ciRaw ? (
          <Badge color={checkColor(pr.ciState ?? "neutral")} variant="light" radius="sm" size="sm">
            {pr.ciRaw.toLowerCase()}
          </Badge>
        ) : (
          <Text size="sm" c="dimmed">
            no checks reported
          </Text>
        )}
        <MergeableBadge mergeable={pr.mergeable} showUnknown />
      </Group>
      {pr.checks.length ? (
        <Stack gap={6}>
          {pr.checks.map((c, i) => (
            <Paper key={`${c.name}-${i}`} p="sm">
              <Group justify="space-between" wrap="nowrap" gap="xs">
                <Text size="sm" className="truncate" style={{ minWidth: 0 }}>
                  {c.name}
                </Text>
                <Group gap={6} wrap="nowrap">
                  <CheckBadge status={c.status} />
                  {c.url ? (
                    <ActionIcon component="a" href={c.url} target="_blank" rel="noopener noreferrer" variant="subtle" color="gray" size={40} aria-label="Open check">
                      <IconExternalLink size={16} />
                    </ActionIcon>
                  ) : null}
                </Group>
              </Group>
            </Paper>
          ))}
        </Stack>
      ) : null}
    </Stack>
  );
}

// --- Review drawer --------------------------------------------------------------------------------

function ReviewDrawer({
  opened,
  onClose,
  onSubmit,
}: {
  opened: boolean;
  onClose: () => void;
  onSubmit: (event: ReviewEvent, body: string) => Promise<void>;
}) {
  const narrow = useMediaQuery("(max-width: 48em)", true, { getInitialValueInEffect: false });
  const [event, setEvent] = useState<ReviewEvent>("COMMENT");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const needsBody = event !== "APPROVE";
  const submit = async () => {
    setBusy(true);
    try {
      await onSubmit(event, body.trim());
      setBody("");
      setEvent("COMMENT");
      onClose();
    } finally {
      setBusy(false);
    }
  };
  return (
    <Drawer
      opened={opened}
      onClose={onClose}
      position={narrow ? "bottom" : "right"}
      size={narrow ? "auto" : "md"}
      title="Your review"
      radius={narrow ? "lg" : 0}
      withCloseButton
      styles={{ content: narrow ? { paddingBottom: "var(--safe-bottom)" } : undefined }}
    >
      <Stack gap="sm">
        <SegmentedControl
          fullWidth
          value={event}
          onChange={(v) => setEvent(v as ReviewEvent)}
          data={[
            { value: "APPROVE", label: "Approve" },
            { value: "REQUEST_CHANGES", label: "Request changes" },
            { value: "COMMENT", label: "Comment" },
          ]}
          styles={{ label: { minHeight: 36, display: "flex", alignItems: "center", justifyContent: "center" } }}
        />
        <Textarea
          value={body}
          onChange={(e) => setBody(e.currentTarget.value)}
          placeholder={needsBody ? "Say what should change / your comment (required)" : "Optional note"}
          autosize
          minRows={4}
          maxRows={12}
          size="sm"
        />
        <Button
          h={44}
          color={event === "APPROVE" ? "frog" : event === "REQUEST_CHANGES" ? "red" : "gray"}
          disabled={needsBody && !body.trim()}
          loading={busy}
          onClick={submit}
        >
          {event === "APPROVE" ? "Approve" : event === "REQUEST_CHANGES" ? "Request changes" : "Submit comment"}
        </Button>
      </Stack>
    </Drawer>
  );
}

// --- Page -----------------------------------------------------------------------------------------

export function PullPage() {
  const { owner = "", repo: name = "", number: numStr = "" } = useParams();
  const repo = `${owner}/${name}`;
  const number = Number(numStr);
  const { client, isMock, hasToken } = useGitHub();
  const [tab, setTab] = useTab();
  const goBack = useBack(ROUTES.pulls);

  const [loaded, setLoaded] = useState<PullDetail | null>(null);
  // Only a detail for THIS route is ever rendered or written against. `loaded` may still hold the
  // previous PR for a frame after navigation (its reset below runs post-paint); without this gate
  // that frame shows PR 1's body with live controls whose requests target PR 2.
  const pr = loaded && loaded.repo === repo && loaded.number === number ? loaded : null;
  // The routed identity as of the latest render — what an async continuation checks it still owns.
  const routeKey = `${repo}#${number}`;
  const routeRef = useRef(routeKey);
  routeRef.current = routeKey;
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  // Files stays mounted once visited: no refetch + collapse/wrap/composer state survives tab switches.
  const [filesVisited, setFilesVisited] = useState(tab === "files");
  useEffect(() => {
    if (tab === "files") setFilesVisited(true);
  }, [tab]);

  // Route identity changed: drop the old PR and every bit of per-PR UI state.
  useEffect(() => {
    setLoaded((prev) => (prev && prev.repo === repo && prev.number === number ? prev : null));
    setReviewOpen(false);
    setLoadingEarlier(false);
    setError(null);
  }, [repo, number]);

  // Latest request wins: a result for a superseded load (route change, or a reload that overtook
  // it) is dropped. `fresh` bypasses the GitHub read cache (the Refresh button); a reload after a
  // write doesn't need it — the write already cleared the cache.
  const reqId = useRef(0);
  const load = useCallback(
    (fresh: boolean) => {
      if (!client || isMock) return;
      const id = ++reqId.current;
      setLoading(true);
      setError(null);
      fetchPullDetail(client, repo, number, { fresh })
        // A reload after a write re-reads only the newest comments/reviews page: keep the earlier
        // pages the user already loaded (same PR only — a route change starts clean).
        .then(
          (d) =>
            id === reqId.current &&
            setLoaded((prev) =>
              prev && prev.repo === d.repo && prev.number === d.number
                ? withConversation(d, mergeRefreshedConversation(prev.conversation, d.conversation))
                : d,
            ),
        )
        .catch((e) => id === reqId.current && setError(e instanceof Error ? e.message : String(e)))
        .finally(() => id === reqId.current && setLoading(false));
    },
    [client, isMock, repo, number],
  );
  useEffect(() => {
    load(false);
    return () => {
      reqId.current++;
    };
  }, [load]);
  const reload = useCallback(() => load(false), [load]);

  // Writes need a token AND the rendered PR to be the routed one (see `pr` above).
  const canWrite = hasToken && !isMock && pr !== null;

  // "Load earlier": the page(s) before the oldest loaded comment / review, merged by id INTO THE
  // CURRENT conversation (a reload after a write may have landed meanwhile — merging into the one
  // captured at click time would drop what it brought). A result for a PR the user has navigated
  // away from is dropped.
  const onLoadEarlier = useCallback(async () => {
    if (!client || !pr || !hasEarlier(pr.conversation) || loadingEarlier) return;
    const key = `${pr.repo}#${pr.number}`;
    setLoadingEarlier(true);
    try {
      const page = await fetchEarlierPages(client, pr.repo, pr.number, pr.conversation);
      // Stitch into the CURRENT detail of the same PR (a quiet reload may have replaced it meanwhile).
      setLoaded((cur) =>
        cur && `${cur.repo}#${cur.number}` === key ? withConversation(cur, withEarlierConversation(cur.conversation, page)) : cur,
      );
    } catch (e) {
      if (routeRef.current === key)
        notifications.show({ color: "red", message: "Couldn't load earlier comments: " + (e instanceof Error ? e.message : String(e)) });
    } finally {
      // Owned by this PR only: after navigating away the identity effect already reset the flag.
      if (routeRef.current === key) setLoadingEarlier(false);
    }
  }, [client, pr, loadingEarlier]);

  const onComment = useCallback(
    async (body: string) => {
      if (!client || !pr) return;
      try {
        await client.postComment(pr.repo, pr.number, body);
        notifications.show({ color: "green", message: "Comment posted" });
        reload();
      } catch (e) {
        notifications.show({ color: "red", message: e instanceof Error ? e.message : String(e) });
        throw e;
      }
    },
    [client, pr, reload],
  );

  const onReview = useCallback(
    async (event: ReviewEvent, body: string) => {
      if (!client || !pr) return;
      try {
        await submitReview(client, pr.repo, pr.number, event, body);
        notifications.show({
          color: event === "REQUEST_CHANGES" ? "red" : "green",
          message: event === "APPROVE" ? "Approved" : event === "REQUEST_CHANGES" ? "Changes requested" : "Review comment posted",
        });
        reload();
      } catch (e) {
        notifications.show({ color: "red", message: e instanceof Error ? e.message : String(e) });
        throw e;
      }
    },
    [client, pr, reload],
  );

  const header = (
    <div className={classes.sticky}>
      <ActionIcon variant="subtle" color="gray" size={40} aria-label="Back" onClick={goBack}>
        <IconArrowLeft size={20} />
      </ActionIcon>
      <span className={classes.stickyRef}>
        {repo}#{number}
      </span>
      {pr ? <StateBadge state={pr.state} /> : null}
      <Tooltip label="Refresh">
        <ActionIcon variant="subtle" color="gray" size={40} aria-label="Refresh" onClick={() => load(true)} loading={loading && !!pr}>
          <IconRefresh size={18} />
        </ActionIcon>
      </Tooltip>
    </div>
  );

  const segmented = useMemo(
    () => (
      <SegmentedControl
        fullWidth
        value={tab}
        onChange={(v) => setTab(v as Tab)}
        data={[
          { value: "conversation", label: "Conversation" },
          { value: "files", label: pr ? `Files · ${pr.changedFiles}` : "Files" },
          { value: "checks", label: pr && pr.checks.length ? `Checks · ${pr.checks.length}` : "Checks" },
        ]}
        styles={{ label: { minHeight: 36, display: "flex", alignItems: "center", justifyContent: "center" } }}
      />
    ),
    [tab, setTab, pr],
  );

  if (!client || isMock) {
    return (
      <div className={`${classes.root} ${classes.page}`}>
        {header}
        <NoTokenState message="Add a GitHub token to read and review pull requests." />
      </div>
    );
  }

  return (
    <div className={`${classes.root} ${classes.page}`}>
      {header}
      <Stack gap="sm" mt="sm">
        {loading && !pr ? <LoadingBlock label="Loading pull request…" /> : null}
        {error ? <ErrorBlock error={error} /> : null}
        {pr ? (
          <>
            <div>
              <div className={classes.title}>{pr.title}</div>
              <div className={classes.meta} style={{ marginTop: 6 }}>
                {pr.author ? <Who login={pr.author.login} avatarUrl={pr.author.avatarUrl} /> : null}
                <span>opened {relTimeAgo(pr.createdAt)}</span>
                <span>· updated {relTimeAgo(pr.updatedAt)}</span>
                <span className={classes.ref}>
                  {pr.headRefName} → {pr.baseRefName}
                </span>
                <DiffStat additions={pr.additions} deletions={pr.deletions} />
                <span className="tabular">
                  {pr.changedFiles} file{pr.changedFiles === 1 ? "" : "s"}
                </span>
                <CiDot state={pr.ciState} title={pr.ciRaw ? `CI ${pr.ciRaw.toLowerCase()}` : undefined} />
                <DecisionBadge decision={pr.reviewDecision} />
                <MergeableBadge mergeable={pr.mergeable} />
              </div>
              {pr.labels.length ? (
                <div style={{ marginTop: 6 }}>
                  <LabelPills labels={pr.labels} max={8} variant="filled" />
                </div>
              ) : null}
              {pr.reviewers.length || pr.teamRequests.length ? (
                <Group gap={8} mt={6} wrap="wrap">
                  {pr.reviewers.map((r) => (
                    <Group key={r.login} gap={4} wrap="nowrap">
                      <Who login={r.login} avatarUrl={r.avatarUrl} />
                      <ReviewStateBadge state={r.state} />
                    </Group>
                  ))}
                  {pr.teamRequests.map((t) => (
                    <Group key={t} gap={4} wrap="nowrap">
                      <Text size="xs" c="dimmed">
                        team {t}
                      </Text>
                      <ReviewStateBadge state="REQUESTED" />
                    </Group>
                  ))}
                </Group>
              ) : null}
            </div>

            {segmented}

            {tab === "conversation" ? (
              <ConversationTab
                pr={pr}
                onComment={onComment}
                canWrite={canWrite}
                onLoadEarlier={() => void onLoadEarlier()}
                loadingEarlier={loadingEarlier}
              />
            ) : null}
            {filesVisited ? (
              <div style={tab === "files" ? undefined : { display: "none" }} aria-hidden={tab !== "files"}>
                <FilesTab client={client} repo={repo} number={number} headOid={pr.headOid} canComment={canWrite} />
              </div>
            ) : null}
            {tab === "checks" ? <ChecksTab pr={pr} /> : null}
          </>
        ) : null}
      </Stack>

      {pr && pr.state !== "merged" && pr.state !== "closed" ? (
        <div className={classes.actionBar}>
          <Button fullWidth h={44} radius="xl" disabled={!canWrite} onClick={() => setReviewOpen(true)}>
            Review
          </Button>
          <Button variant="light" color="gray" h={44} radius="xl" onClick={() => setTab(tab === "files" ? "conversation" : "files")}>
            {tab === "files" ? "Conversation" : "Files"}
          </Button>
        </div>
      ) : null}

      <ReviewDrawer opened={reviewOpen} onClose={() => setReviewOpen(false)} onSubmit={onReview} />
    </div>
  );
}
