/**
 * Issues data layer — PURE query builders + mappers on top of the generic `GitHubClient.graphql` /
 * `rest` seams. No fetching here, so every piece is unit-testable (see data.test.ts).
 */
import type { GhLabel } from "../../lib/labelColor";
import type { GitHubClient, ReadOptions } from "../../github/types";

// --- List -------------------------------------------------------------------------------------

export type IssueFilter = "all" | "involved" | "assigned" | "mine" | "mentioned" | "untriaged";

export const ISSUE_FILTERS: { value: IssueFilter; label: string; needsViewer: boolean; hint: string }[] = [
  { value: "all", label: "All", needsViewer: false, hint: "Every open issue in scope" },
  { value: "involved", label: "Involved", needsViewer: true, hint: "Authored, assigned, mentioned or commented by you" },
  { value: "assigned", label: "Assigned", needsViewer: true, hint: "Assigned to you" },
  { value: "mine", label: "Mine", needsViewer: true, hint: "Opened by you" },
  { value: "mentioned", label: "Mentioned", needsViewer: true, hint: "You are @mentioned" },
  { value: "untriaged", label: "Untriaged", needsViewer: false, hint: "No labels yet (no:label)" },
];

export const ISSUE_PAGE_SIZE = 30;

/** Qualifier a filter contributes to the search string (empty for All). */
export function filterQualifier(filter: IssueFilter, login: string | null | undefined): string {
  if (filter === "untriaged") return "no:label";
  if (filter === "all" || !login) return "";
  switch (filter) {
    case "involved":
      return `involves:${login}`;
    case "assigned":
      return `assignee:${login}`;
    case "mine":
      return `author:${login}`;
    case "mentioned":
      return `mentions:${login}`;
  }
}

/**
 * The GitHub search string for the list: open issues, org-scoped, newest-updated first, plus the
 * filter's qualifier and any free text (whitespace-collapsed) the user typed.
 */
export function buildIssueSearch(opts: {
  orgs: string[];
  filter: IssueFilter;
  login?: string | null;
  text?: string;
}): string {
  const parts = ["is:issue", "is:open", "sort:updated-desc"];
  for (const org of opts.orgs) parts.push(`org:${org}`);
  const q = filterQualifier(opts.filter, opts.login);
  if (q) parts.push(q);
  const text = (opts.text ?? "").trim().replace(/\s+/g, " ");
  if (text) parts.push(text);
  return parts.join(" ");
}

export const ISSUE_LIST_QUERY = /* GraphQL */ `
query IssuesList($q: String!, $first: Int!, $cursor: String) {
  search(query: $q, type: ISSUE, first: $first, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on Issue {
        number title url updatedAt createdAt state
        author { login avatarUrl }
        repository { nameWithOwner }
        labels(first: 10) { nodes { name color } }
        comments { totalCount }
        assignees(first: 5) { nodes { login avatarUrl } }
      }
    }
  }
}`;

export interface GhActor {
  login: string;
  avatarUrl: string;
}
export type { GhLabel };

/** Raw node shape returned by {@link ISSUE_LIST_QUERY}. Non-Issue nodes come back as `{}`. */
export interface IssueListNode {
  number?: number;
  title?: string;
  url?: string;
  updatedAt?: string;
  createdAt?: string;
  state?: string;
  author?: GhActor | null;
  repository?: { nameWithOwner: string } | null;
  labels?: { nodes: (GhLabel | null)[] } | null;
  comments?: { totalCount: number } | null;
  assignees?: { nodes: (GhActor | null)[] } | null;
}

export interface IssueListData {
  search: {
    issueCount: number;
    pageInfo: { hasNextPage: boolean; endCursor: string | null };
    nodes: (IssueListNode | null)[];
  };
}

export interface IssueRow {
  repo: string; // owner/repo
  number: number;
  title: string;
  url: string;
  state: "OPEN" | "CLOSED";
  createdAt: string;
  updatedAt: string;
  author: GhActor | null;
  labels: GhLabel[];
  commentCount: number;
  assignees: GhActor[];
}

export interface IssuePage {
  rows: IssueRow[];
  total: number;
  hasNextPage: boolean;
  endCursor: string | null;
}

function actor(a: GhActor | null | undefined): GhActor | null {
  return a && a.login ? { login: a.login, avatarUrl: a.avatarUrl ?? "" } : null;
}

function labels(nodes: (GhLabel | null)[] | undefined | null): GhLabel[] {
  return (nodes ?? [])
    .filter((l): l is GhLabel => !!l && !!l.name)
    .map((l) => ({ name: l.name, color: (l.color ?? "").replace(/^#/, "") }));
}

/** Map one search node → row; null for non-issue / malformed nodes (PRs sneaking in, ghosts). */
export function mapIssueNode(n: IssueListNode | null | undefined): IssueRow | null {
  if (!n || typeof n.number !== "number" || !n.repository?.nameWithOwner) return null;
  return {
    repo: n.repository.nameWithOwner,
    number: n.number,
    title: n.title ?? "",
    url: n.url ?? `https://github.com/${n.repository.nameWithOwner}/issues/${n.number}`,
    state: n.state === "CLOSED" ? "CLOSED" : "OPEN",
    createdAt: n.createdAt ?? "",
    updatedAt: n.updatedAt ?? "",
    author: actor(n.author),
    labels: labels(n.labels?.nodes),
    commentCount: n.comments?.totalCount ?? 0,
    assignees: (n.assignees?.nodes ?? []).map(actor).filter((a): a is GhActor => a !== null),
  };
}

export function mapIssuePage(d: IssueListData): IssuePage {
  const rows = (d.search?.nodes ?? []).map(mapIssueNode).filter((r): r is IssueRow => r !== null);
  return {
    rows,
    total: d.search?.issueCount ?? rows.length,
    hasNextPage: !!d.search?.pageInfo?.hasNextPage,
    endCursor: d.search?.pageInfo?.endCursor ?? null,
  };
}

/** Append a page, dropping duplicates by `repo#number` (GitHub search can shift between pages). */
export function mergeRows(existing: IssueRow[], incoming: IssueRow[]): IssueRow[] {
  const seen = new Set(existing.map(rowKey));
  const out = [...existing];
  for (const r of incoming) {
    const k = rowKey(r);
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(r);
  }
  return out;
}

export function rowKey(r: { repo: string; number: number }): string {
  return `${r.repo}#${r.number}`;
}

/** Distinct repos across rows with counts, most-populated first then alphabetical. */
export function repoFacets(rows: IssueRow[]): { repo: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const r of rows) counts.set(r.repo, (counts.get(r.repo) ?? 0) + 1);
  return [...counts.entries()]
    .map(([repo, count]) => ({ repo, count }))
    .sort((a, b) => b.count - a.count || a.repo.localeCompare(b.repo));
}

/**
 * Short chip label for a repo: the bare repo name when no two facets share one, else `owner/repo`
 * (two orgs can both have a `docs`).
 */
export function repoChipLabel(repo: string, facets: { repo: string }[]): string {
  const short = repo.split("/")[1] ?? repo;
  const clash = facets.some((f) => f.repo !== repo && (f.repo.split("/")[1] ?? f.repo) === short);
  return clash ? repo : short;
}

export async function fetchIssuePage(
  client: GitHubClient,
  q: string,
  cursor: string | null,
  opts?: ReadOptions,
): Promise<IssuePage> {
  const d = await client.graphql<IssueListData>(ISSUE_LIST_QUERY, { q, first: ISSUE_PAGE_SIZE, cursor }, opts);
  return mapIssuePage(d);
}

// --- Detail -----------------------------------------------------------------------------------

export const ISSUE_DETAIL_QUERY = /* GraphQL */ `
query IssueDetail($owner: String!, $name: String!, $number: Int!, $commentsLast: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      title body state createdAt updatedAt url
      author { login avatarUrl }
      labels(first: 20) { nodes { name color } }
      assignees(first: 10) { nodes { login avatarUrl } }
      milestone { title }
      comments(last: $commentsLast) {
        totalCount
        pageInfo { hasPreviousPage startCursor }
        nodes { id author { login avatarUrl } body createdAt url }
      }
    }
    labels(first: 100) { nodes { name color } }
  }
}`;

/** Comments fetched with the detail (the newest ones) and per "Load earlier" page. */
export const ISSUE_COMMENTS_PAGE = 50;

/**
 * The page of comments BEFORE a cursor (`pageInfo.startCursor` of the page already on screen) —
 * what "Load N earlier comments" walks. Same node shape as the detail's `comments`.
 */
export const ISSUE_COMMENTS_BEFORE_QUERY = /* GraphQL */ `
query IssueCommentsBefore($owner: String!, $name: String!, $number: Int!, $last: Int!, $before: String!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      comments(last: $last, before: $before) {
        totalCount
        pageInfo { hasPreviousPage startCursor }
        nodes { id author { login avatarUrl } body createdAt url }
      }
    }
  }
}`;

/** The `comments` connection as both detail + earlier-page queries return it. */
export interface IssueCommentsConnection {
  totalCount?: number;
  pageInfo?: { hasPreviousPage?: boolean; startCursor?: string | null } | null;
  nodes: ({ id: string; author: GhActor | null; body: string; createdAt: string; url: string } | null)[];
}

export interface IssueDetailData {
  repository: {
    issue: {
      title: string;
      body: string | null;
      state: string;
      createdAt: string;
      updatedAt: string;
      url: string;
      author: GhActor | null;
      labels: { nodes: (GhLabel | null)[] } | null;
      assignees: { nodes: (GhActor | null)[] } | null;
      milestone: { title: string } | null;
      comments: IssueCommentsConnection | null;
    } | null;
    labels: { nodes: (GhLabel | null)[] } | null;
  } | null;
}

export interface IssueComment {
  id: string;
  author: GhActor | null;
  body: string;
  createdAt: string;
  url: string;
}

export interface IssueDetail {
  repo: string;
  number: number;
  title: string;
  body: string;
  state: "OPEN" | "CLOSED";
  createdAt: string;
  updatedAt: string;
  url: string;
  author: GhActor | null;
  labels: GhLabel[];
  assignees: GhActor[];
  milestone: string | null;
  /** The loaded comments, oldest first: the newest ISSUE_COMMENTS_PAGE plus any earlier pages fetched. */
  comments: IssueComment[];
  /** Total comments on GitHub — more than `comments.length` when the thread is long. */
  commentsTotal: number;
  /** Cursor for the page before the oldest loaded comment (`before:`), or null when all are loaded. */
  earlierCursor: string | null;
  /** Every label defined on the repo (for the picker). */
  repoLabels: GhLabel[];
}

/** Map the detail payload; null when the issue doesn't exist (GitHub returns `issue: null`). */
export function mapIssueDetail(d: IssueDetailData, repo: string, number: number): IssueDetail | null {
  const i = d.repository?.issue;
  if (!i) return null;
  const page = mapCommentsPage(i.comments);
  const comments = page.comments;
  return {
    repo,
    number,
    title: i.title ?? "",
    body: i.body ?? "",
    state: i.state === "CLOSED" ? "CLOSED" : "OPEN",
    createdAt: i.createdAt ?? "",
    updatedAt: i.updatedAt ?? "",
    url: i.url ?? `https://github.com/${repo}/issues/${number}`,
    author: actor(i.author),
    labels: labels(i.labels?.nodes),
    assignees: (i.assignees?.nodes ?? []).map(actor).filter((a): a is GhActor => a !== null),
    milestone: i.milestone?.title ?? null,
    comments,
    commentsTotal: Math.max(page.totalCount, comments.length),
    earlierCursor: page.earlierCursor,
    repoLabels: labels(d.repository?.labels?.nodes),
  };
}

export interface IssueCommentsPage {
  comments: IssueComment[];
  totalCount: number;
  /** `startCursor` when GitHub says there is a page before this one; null at the first comment. */
  earlierCursor: string | null;
}

/** Map one `comments` connection (detail or earlier page) → comments oldest-first + paging state. */
export function mapCommentsPage(conn: IssueCommentsConnection | null | undefined): IssueCommentsPage {
  const comments = (conn?.nodes ?? [])
    .filter((c): c is NonNullable<typeof c> => !!c)
    .map((c) => ({ id: c.id, author: actor(c.author), body: c.body ?? "", createdAt: c.createdAt, url: c.url }));
  const info = conn?.pageInfo;
  return {
    comments,
    totalCount: conn?.totalCount ?? comments.length,
    earlierCursor: info?.hasPreviousPage && info.startCursor ? info.startCursor : null,
  };
}

/**
 * Union two comment sets by id, oldest first. Incoming wins on a clash (a refetch carries edits);
 * optimistic `local-*` placeholders are dropped once real data arrives.
 */
export function mergeComments(existing: IssueComment[], incoming: IssueComment[]): IssueComment[] {
  const byId = new Map<string, IssueComment>();
  for (const c of existing) if (!c.id.startsWith("local-")) byId.set(c.id, c);
  for (const c of incoming) byId.set(c.id, c);
  return [...byId.values()].sort((a, b) => a.createdAt.localeCompare(b.createdAt) || a.id.localeCompare(b.id));
}

/** The detail with an EARLIER page stitched on: comments merged, cursor moved back. */
export function withEarlierComments(issue: IssueDetail, page: IssueCommentsPage): IssueDetail {
  const comments = mergeComments(issue.comments, page.comments);
  return {
    ...issue,
    comments,
    commentsTotal: Math.max(page.totalCount, issue.commentsTotal, comments.length),
    earlierCursor: page.earlierCursor,
  };
}

/**
 * A quiet REFRESH (after a write) re-reads only the newest page. Keep the earlier pages the user
 * already loaded: merge, and keep the OLDER of the two "earlier" cursors — the refreshed page's
 * cursor points before its own first comment, which we may well already have.
 */
export function mergeRefreshedDetail(prev: IssueDetail | null, next: IssueDetail): IssueDetail {
  if (!prev || prev.repo !== next.repo || prev.number !== next.number) return next;
  const prevOldest = prev.comments.find((c) => !c.id.startsWith("local-"))?.createdAt;
  const nextOldest = next.comments[0]?.createdAt;
  const keepPrevCursor = !!prevOldest && !!nextOldest && prevOldest < nextOldest;
  return {
    ...next,
    comments: mergeComments(prev.comments, next.comments),
    earlierCursor: keepPrevCursor ? prev.earlierCursor : next.earlierCursor,
  };
}

export async function fetchEarlierComments(
  client: GitHubClient,
  owner: string,
  name: string,
  number: number,
  before: string,
): Promise<IssueCommentsPage> {
  const d = await client.graphql<{ repository: { issue: { comments: IssueCommentsConnection | null } | null } | null }>(
    ISSUE_COMMENTS_BEFORE_QUERY,
    { owner, name, number, last: ISSUE_COMMENTS_PAGE, before },
  );
  return mapCommentsPage(d.repository?.issue?.comments);
}

/** True when a GraphQL error means "no such issue" (GitHub pairs `issue: null` with this message). */
export function isNotFoundError(message: string): boolean {
  return /could not resolve to an issue/i.test(message) || /could not resolve to a repository/i.test(message);
}

export async function fetchIssueDetail(
  client: GitHubClient,
  owner: string,
  name: string,
  number: number,
  opts?: ReadOptions,
): Promise<IssueDetail | null> {
  const d = await client.graphql<IssueDetailData>(
    ISSUE_DETAIL_QUERY,
    { owner, name, number, commentsLast: ISSUE_COMMENTS_PAGE },
    opts,
  );
  return mapIssueDetail(d, `${owner}/${name}`, number);
}

/** REST path for the close/reopen PATCH (the user's own decision). */
export function issueRestPath(repo: string, number: number): string {
  const [owner, name] = repo.split("/");
  return `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/issues/${number}`;
}

export async function setIssueState(
  client: GitHubClient,
  repo: string,
  number: number,
  state: "open" | "closed",
): Promise<void> {
  await client.rest<unknown>("PATCH", issueRestPath(repo, number), { state });
}
