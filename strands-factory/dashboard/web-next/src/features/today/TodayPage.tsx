/**
 * Today — everything that needs the signed-in maintainer, newest first: mentions, their PRs,
 * Strandly's PRs, assignments, review requests. One aliased GraphQL search (see data.ts) through the
 * user's browser token; refetches when the viewer or scope changes and polls every 2 minutes while
 * mounted. Mounting reads the GitHub read cache (10 min) so returning to the tab is instant — the
 * "Updated x ago" caption is the real fetch time; Refresh, polls and tab-return fetch fresh. Mock /
 * no-token → an empty state pointing at Settings.
 */
import { type ReactElement, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ActionIcon, Button, Stack, Text, Title, Tooltip } from "@mantine/core";
import { IconRefresh } from "@tabler/icons-react";
import { useNavigate } from "react-router-dom";
import { FilterChips } from "../../components/FilterChips";
import { NoTokenState } from "../../components/NoTokenState";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlock";
import { useGitHub } from "../../github/GitHubContext";
import { itemLink } from "../../lib/links";
import { relTimeAgo } from "../../lib/format";
import { getScope, subscribeScope } from "../../lib/scope";
import {
  buildTodayQuery,
  buildTodayVariables,
  cappedReasons,
  filterRows,
  mergeBuckets,
  reasonCounts,
  REASON_META,
  REASONS,
  type Filter,
  type Reason,
  type TodayResponse,
  type TodayRow as Row,
} from "./data";
import { TodayRow } from "./TodayRow";
import classes from "./Today.module.css";

interface FeedState {
  rows: Row[] | null;
  /** Buckets that hit the `first: 40` page cap — older items in those buckets are not loaded. */
  capped: Reason[];
  /** First load (no rows yet) — shows the LoadingBlock. */
  loading: boolean;
  /** Any in-flight fetch (manual refresh / poll) — spins the header icon only. */
  refreshing: boolean;
  error: string | null;
  refreshedAt: string | null;
}

const POLL_MS = 2 * 60_000;
/** Returning to the tab refetches only if the feed is at least this stale (tab-flipping shouldn't spam). */
const VISIBLE_REFRESH_MIN_MS = 30_000;
const QUERY = buildTodayQuery();
const IDLE: FeedState = {
  rows: null,
  capped: [],
  loading: false,
  refreshing: false,
  error: null,
  refreshedAt: null,
};

export function TodayPage() {
  const { client, viewer, hasToken, isMock, validating, tokenError } = useGitHub();
  const scope = useSyncExternalStore(subscribeScope, getScope, getScope);
  const navigate = useNavigate();
  const [filter, setFilter] = useState<Filter>("all");
  const [state, setState] = useState<FeedState>(IDLE);
  // Monotonic request id: a late response from a superseded fetch (scope/viewer changed) is dropped.
  const reqId = useRef(0);
  // Wall-clock of the last successful fetch, for the visibility-return throttle (ref: no re-render).
  const lastFetchMs = useRef(0);
  // Re-render on a timer so "refreshed x ago" ticks without a refetch.
  const [, setTick] = useState(0);

  const login = viewer?.login ?? null;
  const canFetch = !!client && !!login && !isMock;

  /** @param silent keep the current rows + error on screen while fetching (poll / tab return). */
  const load = useCallback(
    async (silent: boolean, fresh: boolean) => {
      if (!client || !login) return;
      const id = ++reqId.current;
      setState((s) => ({ ...s, loading: s.rows === null, refreshing: true, error: silent ? s.error : null }));
      try {
        let fetchedAt = Date.now();
        const res = await client.graphql<TodayResponse>(QUERY, buildTodayVariables(login, scope.orgs), {
          fresh,
          onMeta: (m) => (fetchedAt = m.fetchedAt),
        });
        if (id !== reqId.current) return;
        lastFetchMs.current = fetchedAt;
        setState({
          rows: mergeBuckets(res),
          capped: cappedReasons(res),
          loading: false,
          refreshing: false,
          error: null,
          refreshedAt: new Date(fetchedAt).toISOString(),
        });
      } catch (e) {
        if (id !== reqId.current) return;
        const message = e instanceof Error ? e.message : String(e);
        setState((s) => ({ ...s, loading: false, refreshing: false, error: message }));
      }
    },
    [client, login, scope],
  );

  // Initial fetch + refetch on client/viewer/scope change (all captured by `load`'s identity).
  useEffect(() => {
    if (!canFetch) {
      setState(IDLE);
      return;
    }
    setState({ ...IDLE, loading: true });
    void load(false, false);
  }, [canFetch, load]);

  // Poll while mounted; paused when the tab is hidden, refreshed on return.
  useEffect(() => {
    if (!canFetch) return;
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") void load(true, true);
    }, POLL_MS);
    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      if (Date.now() - lastFetchMs.current < VISIBLE_REFRESH_MIN_MS) return;
      void load(true, true);
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [canFetch, load]);

  // Tick the "refreshed x ago" caption every 30s.
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 30_000);
    return () => clearInterval(t);
  }, []);

  const rows = state.rows ?? [];
  const counts = useMemo(() => reasonCounts(rows), [rows]);
  const visible = useMemo(() => filterRows(rows, filter), [rows, filter]);

  const open = useCallback(
    (row: Row) => navigate(itemLink(row.repo, row.kind, row.number, row.url)),
    [navigate],
  );

  // Spec header: "Updated x ago · N items".
  const subtitle =
    state.rows === null
      ? null
      : `${state.refreshedAt ? `Updated ${relTimeAgo(state.refreshedAt)} · ` : ""}${rows.length} ${
          rows.length === 1 ? "item" : "items"
        }`;

  let body: ReactElement;
  if (!hasToken || isMock) {
    body = (
      <NoTokenState
        title="Your day, from GitHub"
        message="Add a GitHub token to see mentions, your PRs, Strandly's PRs, and what's assigned to or awaiting review from you — across every org in scope."
      />
    );
  } else if (tokenError) {
    body = <ErrorBlock error={tokenError} />;
  } else if (!login) {
    body = <LoadingBlock label={validating ? "Checking your token…" : "Loading…"} />;
  } else if (state.loading && state.rows === null) {
    body = <LoadingBlock label="Loading your day…" />;
  } else if (state.error && state.rows === null) {
    body = (
      <Stack gap="xs">
        <ErrorBlock error={state.error} />
        <Button variant="light" onClick={() => void load(false, true)} style={{ alignSelf: "flex-start" }}>
          Retry
        </Button>
      </Stack>
    );
  } else {
    body = (
      <Stack gap="md">
        {state.error ? (
          <Text size="sm" c="red">
            Refresh failed: {state.error}
          </Text>
        ) : null}
        <FilterChips<Filter>
          value={filter}
          onChange={setFilter}
          ariaLabel="Filter"
          options={[
            { value: "all", label: "All", count: rows.length },
            ...REASONS.map((r) => ({ value: r, label: REASON_META[r].filter, count: counts[r], color: REASON_META[r].color })),
          ]}
        />

        {state.capped.length > 0 ? (
          <div className={classes.capped}>
            Showing the newest 40 for {state.capped.map((r) => REASON_META[r].filter).join(", ")} — older items
            are on GitHub.
          </div>
        ) : null}

        {visible.length === 0 ? (
          <EmptyBlock
            label={
              rows.length === 0 ? "Nothing needs you right now." : `Nothing under “${filterLabel(filter)}”.`
            }
          />
        ) : (
          <Stack gap={6} component="ul" style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {visible.map((row) => (
              <li key={row.key}>
                <TodayRow row={row} onOpen={open} />
              </li>
            ))}
          </Stack>
        )}
      </Stack>
    );
  }

  return (
    <Stack gap="md">
      <div className={classes.header}>
        <div>
          <Title order={2}>Today</Title>
          {subtitle ? <div className={classes.subtitle}>{subtitle}</div> : null}
        </div>
        {canFetch ? (
          <Tooltip label="Refresh" withArrow>
            <ActionIcon
              variant="subtle"
              color="gray"
              size={40}
              radius="xl"
              aria-label="Refresh"
              loading={state.refreshing}
              onClick={() => void load(false, true)}
            >
              <IconRefresh size={18} />
            </ActionIcon>
          </Tooltip>
        ) : null}
      </div>
      {body}
    </Stack>
  );
}

function filterLabel(f: Filter): string {
  return f === "all" ? "All" : REASON_META[f].filter;
}
