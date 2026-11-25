/**
 * Today feed — pure data layer. ONE aliased GraphQL `search(type: ISSUE)` query (five buckets,
 * each org-scoped + `is:open` + `first: 40`), then a merge that dedups by `repo#number`, unions the
 * reasons into chips, and sorts `updatedAt` desc. No React, no fetch — everything here is testable.
 */
import { agentQueryLogin } from "../../lib/team";

/** Why an item is in the feed. One chip per reason; an item may carry several. */
export type Reason = "mentioned" | "my-pr" | "strandly-pr" | "assigned" | "review-requested";

export const REASONS: readonly Reason[] = [
  "mentioned",
  "my-pr",
  "strandly-pr",
  "assigned",
  "review-requested",
] as const;

/** Chip label + Mantine color per reason (one color per reason, stable). */
export const REASON_META: Record<Reason, { label: string; filter: string; color: string }> = {
  mentioned: { label: "Mentioned", filter: "Mentioned", color: "blue" },
  "my-pr": { label: "My PR", filter: "My PRs", color: "frog" },
  "strandly-pr": { label: "Strandly PR", filter: "Strandly", color: "violet" },
  assigned: { label: "Assigned", filter: "Assigned", color: "orange" },
  "review-requested": { label: "Review requested", filter: "Review requested", color: "pink" },
};

/** GraphQL alias per reason — fixed, so the response shape is known. */
const ALIAS: Record<Reason, string> = {
  mentioned: "mentioned",
  "my-pr": "myPrs",
  "strandly-pr": "strandlyPrs",
  assigned: "assigned",
  "review-requested": "reviewRequested",
};

export const PAGE_SIZE = 40;

/**
 * The raw node shape the query returns (issue + PR union). Only what the row renders is selected —
 * five buckets × 40 nodes makes every extra nested connection (labels, …) a real search-cost line.
 */
export interface SearchNode {
  __typename: "Issue" | "PullRequest";
  number: number;
  title: string;
  url: string;
  updatedAt: string;
  author: { login: string; avatarUrl: string } | null;
  repository: { nameWithOwner: string };
  comments?: { totalCount: number } | null;
  isDraft?: boolean;
}

export interface SearchBucket {
  issueCount?: number;
  nodes: (SearchNode | null)[] | null;
}

/** Response of {@link buildTodayQuery}: one bucket per alias. */
export type TodayResponse = Partial<Record<string, SearchBucket | null>>;

/** A merged feed row. */
export interface TodayRow {
  key: string; // "owner/repo#number"
  kind: "issue" | "pr";
  repo: string;
  number: number;
  title: string;
  url: string;
  updatedAt: string;
  author: { login: string; avatarUrl: string } | null;
  comments: number;
  isDraft: boolean;
  reasons: Set<Reason>;
}

const orgClause = (orgs: string[]): string => (orgs.length ? orgs : ["__unconfigured__"]).map((o) => `org:${o}`).join(" ");

/** The search string for one reason bucket (exported for tests + tooltips). */
export function bucketQuery(reason: Reason, login: string, orgs: string[]): string {
  const base = `${orgClause(orgs)} is:open sort:updated-desc`;
  switch (reason) {
    case "mentioned":
      return `${base} mentions:${login}`;
    case "my-pr":
      return `${base} is:pr author:${login}`;
    case "strandly-pr":
      return `${base} is:pr author:${agentQueryLogin()}`;
    case "assigned":
      return `${base} assignee:${login}`;
    case "review-requested":
      return `${base} is:pr review-requested:${login}`;
  }
}

const NODE_FIELDS = `
  __typename
  ... on Issue {
    number title url updatedAt
    author { login avatarUrl }
    repository { nameWithOwner }
    comments { totalCount }
  }
  ... on PullRequest {
    number title url updatedAt isDraft
    author { login avatarUrl }
    repository { nameWithOwner }
    comments { totalCount }
  }`;

/**
 * Build the single aliased query. Search strings are passed as variables (`$mentioned`, …) so the
 * document itself is constant; {@link buildTodayVariables} produces the matching values.
 */
export function buildTodayQuery(): string {
  const vars = REASONS.map((r) => `$${ALIAS[r]}: String!`).join(", ");
  const fields = REASONS.map(
    (r) =>
      `  ${ALIAS[r]}: search(type: ISSUE, first: ${PAGE_SIZE}, query: $${ALIAS[r]}) { issueCount nodes {${NODE_FIELDS}\n  } }`,
  ).join("\n");
  return `query Today(${vars}) {\n${fields}\n}`;
}

/** Variables for {@link buildTodayQuery}: alias → search string. */
export function buildTodayVariables(login: string, orgs: string[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const r of REASONS) out[ALIAS[r]] = bucketQuery(r, login, orgs);
  return out;
}

function toRow(n: SearchNode, reason: Reason): TodayRow {
  const repo = n.repository.nameWithOwner;
  return {
    key: `${repo}#${n.number}`,
    kind: n.__typename === "PullRequest" ? "pr" : "issue",
    repo,
    number: n.number,
    title: n.title,
    url: n.url,
    updatedAt: n.updatedAt,
    author: n.author,
    comments: n.comments?.totalCount ?? 0,
    isDraft: n.isDraft ?? false,
    reasons: new Set([reason]),
  };
}

/** Sort newest-updated first; ties by repo#number for stability. */
export function sortRows(rows: TodayRow[]): TodayRow[] {
  return [...rows].sort((a, b) => {
    const d = Date.parse(b.updatedAt) - Date.parse(a.updatedAt);
    if (d !== 0 && Number.isFinite(d)) return d;
    return a.key.localeCompare(b.key);
  });
}

/**
 * Merge the five buckets into one deduped, sorted list. The same `repo#number` from several buckets
 * becomes one row whose `reasons` is the union; the freshest `updatedAt` wins for the row data.
 */
export function mergeBuckets(res: TodayResponse): TodayRow[] {
  const byKey = new Map<string, TodayRow>();
  for (const reason of REASONS) {
    const nodes = res[ALIAS[reason]]?.nodes ?? [];
    for (const n of nodes) {
      if (!n || !n.repository?.nameWithOwner || typeof n.number !== "number") continue;
      const row = toRow(n, reason);
      const prev = byKey.get(row.key);
      if (!prev) {
        byKey.set(row.key, row);
        continue;
      }
      prev.reasons.add(reason);
      if (Date.parse(row.updatedAt) > Date.parse(prev.updatedAt)) {
        byKey.set(row.key, { ...row, reasons: prev.reasons });
      }
    }
  }
  return sortRows([...byKey.values()]);
}

/** Quick-filter value: `"all"` or one reason. */
export type Filter = "all" | Reason;

export function filterRows(rows: TodayRow[], filter: Filter): TodayRow[] {
  if (filter === "all") return rows;
  return rows.filter((r) => r.reasons.has(filter));
}

/** Per-reason counts (for chip badges). */
export function reasonCounts(rows: TodayRow[]): Record<Reason, number> {
  const out = Object.fromEntries(REASONS.map((r) => [r, 0])) as Record<Reason, number>;
  for (const row of rows) for (const r of row.reasons) out[r] += 1;
  return out;
}

/**
 * Reasons whose bucket returned fewer nodes than GitHub says match (`issueCount > nodes.length`) —
 * i.e. the `first: 40` page was full and older items are NOT in the feed. The page surfaces this so
 * a long tail is never silently dropped.
 */
export function cappedReasons(res: TodayResponse): Reason[] {
  const out: Reason[] = [];
  for (const reason of REASONS) {
    const bucket = res[ALIAS[reason]];
    if (!bucket) continue;
    const got = (bucket.nodes ?? []).filter(Boolean).length;
    if (typeof bucket.issueCount === "number" && bucket.issueCount > got) out.push(reason);
  }
  return out;
}
