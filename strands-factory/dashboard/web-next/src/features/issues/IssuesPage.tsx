/**
 * /issues — open issues across the scoped orgs, newest-updated first.
 *
 * One GraphQL `search(type: ISSUE)` per page (30 rows, cursor "Load more"). Filter (All / Involved /
 * Assigned / Mine / Mentioned / Untriaged) + free text go INTO the search string; repo chips derived
 * from the loaded rows narrow client-side. The chosen filter persists in sessionStorage. First pages
 * come from the GitHub read cache (10 min) so coming back to the tab is instant; Refresh bypasses it.
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type KeyboardEvent,
} from "react";
import { ActionIcon, Avatar, Button, Group, Stack, Text, TextInput, Title, Tooltip, UnstyledButton } from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { IconMessageCircle, IconRefresh, IconSearch, IconX } from "@tabler/icons-react";
import { useNavigate } from "react-router-dom";
import { ALL, FilterChips } from "../../components/FilterChips";
import { NoTokenState } from "../../components/NoTokenState";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlock";
import { useGitHub } from "../../github/GitHubContext";
import { relTime } from "../../lib/format";
import { issueLink } from "../../lib/links";
import { getScope, subscribeScope } from "../../lib/scope";
import {
  buildIssueSearch,
  fetchIssuePage,
  ISSUE_FILTERS,
  mergeRows,
  repoChipLabel,
  repoFacets,
  rowKey,
  type IssueFilter,
  type IssueRow,
} from "./data";
import { LabelPill } from "../../components/LabelPill";
import classes from "./issues.module.css";

const FILTER_KEY = "strandly.issues.filter";

function readSavedFilter(): IssueFilter | null {
  try {
    const v = sessionStorage.getItem(FILTER_KEY);
    return ISSUE_FILTERS.some((f) => f.value === v) ? (v as IssueFilter) : null;
  } catch {
    return null;
  }
}
function saveFilter(f: IssueFilter): void {
  try {
    sessionStorage.setItem(FILTER_KEY, f);
  } catch {
    // storage unavailable — the choice just doesn't persist
  }
}

export function IssuesPage() {
  const navigate = useNavigate();
  const { client, viewer, isMock, validating } = useGitHub();
  const scope = useSyncExternalStore(subscribeScope, getScope, getScope);

  const [savedFilter, setSavedFilter] = useState<IssueFilter | null>(readSavedFilter);
  // Default: Involved once we know who the viewer is, else All.
  const wanted: IssueFilter = savedFilter ?? (viewer ? "involved" : "all");
  // A viewer-dependent choice (saved earlier) means All while there's no viewer to resolve `<me>`.
  const filter: IssueFilter =
    !viewer && ISSUE_FILTERS.find((f) => f.value === wanted)?.needsViewer ? "all" : wanted;
  const [text, setText] = useState("");
  const [debounced] = useDebouncedValue(text, 300);
  const [applied, setApplied] = useState("");
  useEffect(() => setApplied(debounced.trim()), [debounced]);
  const [repo, setRepo] = useState<string | null>(null);

  const q = useMemo(
    () => buildIssueSearch({ orgs: scope.orgs, filter, login: viewer?.login, text: applied }),
    [scope.orgs, filter, viewer?.login, applied],
  );

  const [rows, setRows] = useState<IssueRow[]>([]);
  const [total, setTotal] = useState(0);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [more, setMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reqId = useRef(0);
  // Whether the first page on screen was fetched fresh: its cursor pages must be too, or a cached
  // page 2 from minutes ago would be stitched under a just-refreshed page 1.
  const freshPages = useRef(false);

  const unavailable = !client || isMock;

  // First page. `fresh` (the Refresh button) bypasses the read cache and keeps the current rows on
  // screen while it loads; a query change drops them and shows the loading block.
  const load = useCallback(
    (fresh: boolean) => {
      if (!client) return;
      const id = ++reqId.current;
      freshPages.current = fresh;
      setError(null);
      if (fresh) setRefreshing(true);
      else {
        setLoading(true);
        setRows([]);
        setRepo(null);
      }
      // A page request from the previous query may still be in flight; its generation-guarded
      // cleanup will never run for us, so reset paging state here.
      setMore(false);
      setCursor(null);
      setHasMore(false);
      fetchIssuePage(client, q, null, { fresh })
        .then((p) => {
          if (id !== reqId.current) return;
          setRows(p.rows);
          setTotal(p.total);
          setCursor(p.endCursor);
          setHasMore(p.hasNextPage);
        })
        .catch((e: unknown) => id === reqId.current && setError(e instanceof Error ? e.message : String(e)))
        .finally(() => {
          if (id !== reqId.current) return;
          setLoading(false);
          setRefreshing(false);
        });
    },
    [client, q],
  );

  // Whenever the query (or client) changes. Wait for token validation so a viewer-based filter
  // doesn't fire once as All and once as Involved.
  useEffect(() => {
    if (unavailable || validating) return;
    load(false);
  }, [load, unavailable, validating]);

  const loadMore = useCallback(() => {
    if (!client || !cursor || more) return;
    const id = reqId.current;
    setMore(true);
    fetchIssuePage(client, q, cursor, { fresh: freshPages.current })
      .then((p) => {
        if (id !== reqId.current) return;
        setRows((r) => mergeRows(r, p.rows));
        setCursor(p.endCursor);
        setHasMore(p.hasNextPage);
      })
      .catch((e: unknown) => id === reqId.current && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => id === reqId.current && setMore(false));
  }, [client, cursor, more, q]);

  const facets = useMemo(() => repoFacets(rows), [rows]);
  const visible = useMemo(() => (repo ? rows.filter((r) => r.repo === repo) : rows), [rows, repo]);
  const activeHint = ISSUE_FILTERS.find((f) => f.value === filter)?.hint;

  function pickFilter(f: IssueFilter) {
    setSavedFilter(f);
    saveFilter(f);
  }
  function onSearchKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") setApplied(text.trim());
  }

  return (
    <Stack gap="md">
      <Group justify="space-between" align="center" wrap="nowrap">
        <Title order={2}>Issues</Title>
        {!unavailable ? (
          <Group gap={4} wrap="nowrap">
            {!loading && rows.length > 0 ? (
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
        ) : null}
      </Group>

      {unavailable ? (
        <NoTokenState message="Add a GitHub token to search open issues across your orgs." />
      ) : (
        <>
          <TextInput
            placeholder="Search open issues…"
            value={text}
            onChange={(e) => setText(e.currentTarget.value)}
            onKeyDown={onSearchKey}
            leftSection={<IconSearch size={16} />}
            rightSection={
              text ? (
                <ActionIcon variant="subtle" color="gray" size="lg" aria-label="Clear search" onClick={() => setText("")}>
                  <IconX size={16} />
                </ActionIcon>
              ) : null
            }
            size="md"
            aria-label="Search open issues"
          />

          <Stack gap={4}>
            <FilterChips<IssueFilter>
              value={filter}
              onChange={pickFilter}
              layout="scroll"
              ariaLabel="Filter"
              options={ISSUE_FILTERS.filter((f) => !f.needsViewer || viewer).map((f) => ({ value: f.value, label: f.label }))}
            />
            {activeHint ? (
              <Text size="sm" c="dimmed">
                {activeHint}
              </Text>
            ) : null}
          </Stack>

          {facets.length > 1 ? (
            <FilterChips
              value={repo ?? ALL}
              onChange={(v) => setRepo(v === ALL ? null : v)}
              variant="outline"
              layout="scroll"
              ariaLabel="Repository"
              options={[
                { value: ALL, label: "All repos" },
                ...facets.map((f) => ({ value: f.repo, label: repoChipLabel(f.repo, facets), count: f.count })),
              ]}
            />
          ) : null}

          {error ? <ErrorBlock error={error} /> : null}
          {loading ? (
            <LoadingBlock label="Searching…" />
          ) : visible.length === 0 && !error ? (
            <EmptyBlock label={applied ? "No open issues match." : "Nothing here — inbox zero."} />
          ) : (
            <Stack gap={8}>
              {visible.map((r) => (
                <IssueRowCard key={rowKey(r)} row={r} onOpen={() => navigate(issueLink(r.repo, r.number))} />
              ))}
            </Stack>
          )}

          {!loading && hasMore && !repo ? (
            <Button variant="light" color="gray" radius="xl" onClick={loadMore} loading={more} h={40} fullWidth>
              Load more
            </Button>
          ) : null}
          {!loading && hasMore && repo ? (
            <Text size="sm" c="dimmed" ta="center">
              Repo filter applies to loaded results only — clear it to load more.
            </Text>
          ) : null}
        </>
      )}
    </Stack>
  );
}

function IssueRowCard({ row, onOpen }: { row: IssueRow; onOpen: () => void }) {
  return (
    <UnstyledButton className={classes.row} onClick={onOpen} aria-label={`${row.repo}#${row.number} ${row.title}`}>
      <Stack gap={6}>
        <Text fw={600} lineClamp={2} style={{ lineHeight: 1.3 }}>
          {row.title}
        </Text>
        <Group gap={8} wrap="wrap" align="center">
          <Text size="sm" c="dimmed" className="tabular" style={{ whiteSpace: "nowrap" }}>
            {row.repo}#{row.number}
          </Text>
          {row.labels.map((l) => (
            <LabelPill key={l.name} label={l} />
          ))}
        </Group>
        <Group gap={10} wrap="nowrap" align="center" style={{ minWidth: 0 }}>
          {row.author ? (
            <Group gap={6} wrap="nowrap" style={{ minWidth: 0 }}>
              <Avatar src={row.author.avatarUrl} alt="" size={18} radius="xl" />
              <Text size="sm" c="dimmed" className="truncate">
                {row.author.login}
              </Text>
            </Group>
          ) : null}
          {row.commentCount > 0 ? (
            <Group gap={3} wrap="nowrap">
              <IconMessageCircle size={14} stroke={1.75} style={{ opacity: 0.7 }} />
              <Text size="sm" c="dimmed" className="tabular">
                {row.commentCount}
              </Text>
            </Group>
          ) : null}
          <Text
            size="sm"
            c="dimmed"
            ml="auto"
            style={{ flex: "0 0 auto" }}
            title={new Date(row.updatedAt).toLocaleString()}
          >
            {relTime(row.updatedAt)}
          </Text>
        </Group>
      </Stack>
    </UnstyledButton>
  );
}
