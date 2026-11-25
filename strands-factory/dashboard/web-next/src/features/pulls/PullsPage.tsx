/**
 * /pulls — open PRs across the scope orgs, newest-updated first. Filter chips (All / Mine / Review
 * requested / Strandly / Involved / Mentioned) + free text feed ONE GitHub search; repo chips derived
 * from the loaded rows narrow client-side. Cursor-paged with "Load more". Filter persists in
 * sessionStorage. No token → "add a token in Settings" state (the mock client can't search). First
 * pages come from the GitHub read cache (10 min) so coming back to the tab is instant; Refresh
 * bypasses it.
 */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ActionIcon, Badge, Button, Group, Stack, Text, TextInput, Title, Tooltip } from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { IconMessageCircle, IconRefresh, IconSearch, IconX } from "@tabler/icons-react";
import { Link } from "react-router-dom";
import { useGitHub } from "../../github/GitHubContext";
import { relTime } from "../../lib/format";
import { pullLink } from "../../lib/links";
import { getScope, subscribeScope } from "../../lib/scope";
import { ALL, FilterChips } from "../../components/FilterChips";
import { NoTokenState } from "../../components/NoTokenState";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlock";
import {
  appendRows,
  buildSearchQuery,
  effectiveFilter,
  fetchPulls,
  isPullFilter,
  PULL_FILTERS,
  repoChips,
  type PullFilter,
  type PullRow,
} from "./data";
import { LabelPills } from "../../components/LabelPill";
import { CiDot, DecisionBadge, DiffStat, Who } from "./badges";
import classes from "./Pull.module.css";

const FILTER_KEY = "pulls.filter";

function readFilter(): PullFilter | null {
  try {
    const v = sessionStorage.getItem(FILTER_KEY);
    return isPullFilter(v) ? v : null;
  } catch {
    return null;
  }
}
function writeFilter(f: PullFilter) {
  try {
    sessionStorage.setItem(FILTER_KEY, f);
  } catch {
    // storage unavailable — fine
  }
}

function PullRowItem({ row }: { row: PullRow }) {
  return (
    <Link to={pullLink(row.repo, row.number)} className={classes.row}>
      <Text className={classes.rowTitle} lineClamp={2}>
        {row.title}
      </Text>
      <div className={classes.meta} style={{ marginTop: 4 }}>
        <span className={classes.repoRef}>
          {row.repo}#{row.number}
        </span>
        {row.isDraft ? (
          <Badge color="gray" variant="light" radius="sm" size="sm">
            Draft
          </Badge>
        ) : null}
        <DecisionBadge decision={row.reviewDecision} />
        <CiDot state={row.ci} />
        <DiffStat additions={row.additions} deletions={row.deletions} />
        {row.author ? <Who login={row.author.login} avatarUrl={row.author.avatarUrl} /> : null}
        {row.comments > 0 ? (
          <span className={classes.comments} title={`${row.comments} comments`}>
            <IconMessageCircle size={14} stroke={1.75} />
            {row.comments}
          </span>
        ) : null}
        <span>{relTime(row.updatedAt)}</span>
      </div>
      {row.labels.length ? (
        <div style={{ marginTop: 4 }}>
          <LabelPills labels={row.labels} max={4} />
        </div>
      ) : null}
    </Link>
  );
}

export function PullsPage() {
  const { client, viewer, isMock, validating } = useGitHub();
  const { orgs } = useSyncExternalStore(subscribeScope, getScope, getScope);

  const [picked, setPicked] = useState<PullFilter | null>(readFilter);
  const filter = effectiveFilter(picked, !!viewer);
  const [text, setText] = useState("");
  const [query] = useDebouncedValue(text, 350);
  const [repo, setRepo] = useState<string | null>(null);

  const [rows, setRows] = useState<PullRow[]>([]);
  const [total, setTotal] = useState(0);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [more, setMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const login = viewer?.login ?? null;
  const q = useMemo(() => buildSearchQuery(orgs, filter, login, query), [orgs, filter, login, query]);
  const gen = useRef(0);
  // Whether the first page on screen was fetched fresh: its cursor pages must be too, or a cached
  // page 2 from minutes ago would be stitched under a just-refreshed page 1.
  const freshPages = useRef(false);

  // First page. `fresh` (the Refresh button) bypasses the read cache and keeps the current rows on
  // screen while it loads; a query change shows the loading block.
  const load = useCallback(
    (fresh: boolean) => {
      if (!client) return;
      const my = ++gen.current;
      freshPages.current = fresh;
      if (fresh) setRefreshing(true);
      else setLoading(true);
      setError(null);
      // A page request from the previous query may still be in flight; its generation-guarded
      // cleanup will never run for us, so reset paging state here.
      setMore(false);
      setCursor(null);
      setHasNext(false);
      fetchPulls(client, q, null, { fresh })
        .then((p) => {
          if (my !== gen.current) return;
          setRows(p.rows);
          // A repo narrowed before the query changed may no longer be in the results — don't strand the
          // user on an empty list with the chip row hidden.
          setRepo((r) => (r && p.rows.some((x) => x.repo === r) ? r : null));
          setTotal(p.total);
          setCursor(p.endCursor);
          setHasNext(p.hasNextPage);
        })
        .catch((e) => my === gen.current && setError(e instanceof Error ? e.message : String(e)))
        .finally(() => {
          if (my !== gen.current) return;
          setLoading(false);
          setRefreshing(false);
        });
    },
    [client, q],
  );

  useEffect(() => {
    if (!client || isMock || validating) return;
    load(false);
  }, [client, isMock, validating, load]);

  const loadMore = useCallback(() => {
    if (!client || !cursor || more) return;
    const my = gen.current;
    setMore(true);
    fetchPulls(client, q, cursor, { fresh: freshPages.current })
      .then((p) => {
        if (my !== gen.current) return;
        setRows((prev) => appendRows(prev, p.rows));
        setCursor(p.endCursor);
        setHasNext(p.hasNextPage);
      })
      .catch((e) => my === gen.current && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => my === gen.current && setMore(false));
  }, [client, cursor, more, q]);

  const chips = useMemo(() => repoChips(rows), [rows]);
  const visible = useMemo(() => (repo ? rows.filter((r) => r.repo === repo) : rows), [rows, repo]);

  const pick = (f: PullFilter) => {
    setPicked(f);
    writeFilter(f);
    setRepo(null);
  };

  if (!client || isMock) {
    return (
      <Stack gap="sm" className={classes.root}>
        <Title order={2}>Pull requests</Title>
        <NoTokenState message="Add a GitHub token to review pull requests across your orgs." />
      </Stack>
    );
  }

  return (
    <Stack gap="sm" className={classes.root}>
      <Group justify="space-between" align="center" wrap="nowrap">
        <Title order={2}>Pull requests</Title>
        <Group gap={4} wrap="nowrap">
          {!loading && !error ? (
            <Text size="sm" c="dimmed" className="tabular">
              {repo ? `${visible.length} of ` : ""}
              {total} open
            </Text>
          ) : null}
          <Tooltip label="Refresh" withArrow>
            <ActionIcon
              variant="subtle"
              color="gray"
              size={40}
              radius="xl"
              aria-label="Refresh"
              loading={refreshing}
              disabled={loading || validating}
              onClick={() => load(true)}
            >
              <IconRefresh size={18} />
            </ActionIcon>
          </Tooltip>
        </Group>
      </Group>

      <TextInput
        placeholder="Search open PRs… (supports repo:, label:, author:)"
        value={text}
        onChange={(e) => setText(e.currentTarget.value)}
        leftSection={<IconSearch size={16} />}
        rightSection={
          text ? (
            <ActionIcon variant="subtle" color="gray" size={32} radius="xl" onClick={() => setText("")} aria-label="Clear search">
              <IconX size={16} />
            </ActionIcon>
          ) : null
        }
        rightSectionWidth={40}
        size="md"
        radius="xl"
        inputMode="search"
      />

      <FilterChips<PullFilter>
        value={filter}
        onChange={pick}
        layout="scroll"
        ariaLabel="Filter"
        options={PULL_FILTERS.map((f) => {
          const disabled = f.needsViewer && !viewer;
          return { value: f.key, label: f.label, disabled, title: disabled ? "Add a GitHub token to use this filter" : undefined };
        })}
      />

      {chips.length > 1 ? (
        <FilterChips
          value={repo ?? ALL}
          onChange={(v) => setRepo(v === ALL ? null : v)}
          variant="outline"
          layout="scroll"
          ariaLabel="Repository"
          options={[
            { value: ALL, label: "All repos" },
            ...chips.map((c) => ({ value: c.repo, label: c.repo.split("/")[1] ?? c.repo, count: c.count })),
          ]}
        />
      ) : null}

      {loading || validating ? <LoadingBlock label="Loading pull requests…" /> : null}
      {!loading && error ? <ErrorBlock error={error} /> : null}
      {!loading && !validating && !error && visible.length === 0 ? <EmptyBlock label="No open pull requests match." /> : null}

      {!loading && !validating && visible.length > 0 ? (
        <Stack gap={6}>
          {visible.map((r) => (
            <PullRowItem key={`${r.repo}#${r.number}`} row={r} />
          ))}
        </Stack>
      ) : null}

      {!loading && hasNext ? (
        <Button variant="light" color="gray" radius="xl" onClick={loadMore} loading={more} h={40} fullWidth>
          Load more
        </Button>
      ) : null}
    </Stack>
  );
}
