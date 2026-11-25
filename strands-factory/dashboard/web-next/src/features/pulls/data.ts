/**
 * PRs data layer — GraphQL/REST queries composed on the generic `GitHubClient.graphql` / `rest`
 * seams, plus the pure mappers that shape responses for the list + detail views. Everything
 * network-free here (query builders, mappers, timeline merge) is unit-tested in data.test.ts.
 */
import type { GhLabel } from "../../lib/labelColor";
import type { GitHubClient, ReadOptions } from "../../github/types";
import { agentQueryLogin } from "../../lib/team";

// --- List -----------------------------------------------------------------------------------------

export type PullFilter = "all" | "mine" | "review-requested" | "strandly" | "involved" | "mentioned";

export const PULL_FILTERS: { key: PullFilter; label: string; needsViewer: boolean }[] = [
  { key: "all", label: "All", needsViewer: false },
  { key: "mine", label: "Mine", needsViewer: true },
  { key: "review-requested", label: "Review requested", needsViewer: true },
  { key: "strandly", label: "Strandly", needsViewer: false },
  { key: "involved", label: "Involved", needsViewer: true },
  { key: "mentioned", label: "Mentioned", needsViewer: true },
];

export function isPullFilter(v: unknown): v is PullFilter {
  return PULL_FILTERS.some((f) => f.key === v);
}

/**
 * The filter to run: the user's pick when it's usable, else a sensible default. A persisted
 * viewer-only pick (Mine / Review requested / …) with no viewer would otherwise show an active-but-
 * disabled chip while silently searching "All".
 */
export function effectiveFilter(picked: PullFilter | null, hasViewer: boolean): PullFilter {
  if (picked && (hasViewer || !PULL_FILTERS.find((f) => f.key === picked)?.needsViewer)) return picked;
  return hasViewer ? "review-requested" : "all";
}

/** The filter qualifier for a viewer, or "" when the filter needs a login we don't have. */
export function filterClause(filter: PullFilter, login: string | null | undefined): string {
  switch (filter) {
    case "strandly":
      return `author:${agentQueryLogin()}`;
    case "mine":
      return login ? `author:${login}` : "";
    case "review-requested":
      return login ? `review-requested:${login}` : "";
    case "involved":
      return login ? `involves:${login}` : "";
    case "mentioned":
      return login ? `mentions:${login}` : "";
    default:
      return "";
  }
}

/**
 * The GitHub search string for the list: open PRs in the scope orgs, newest-updated first, narrowed
 * by the filter + optional free text (which may itself carry qualifiers like `repo:` or `label:`).
 */
export function buildSearchQuery(
  orgs: string[],
  filter: PullFilter,
  login: string | null | undefined,
  text = "",
): string {
  const parts = ["is:pr", "is:open", ...(orgs.length ? orgs : ["__unconfigured__"]).map((o) => `org:${o}`)];
  const clause = filterClause(filter, login);
  if (clause) parts.push(clause);
  const t = text.trim().replace(/\s+/g, " ");
  if (t) parts.push(t);
  parts.push("sort:updated-desc");
  return parts.join(" ");
}

export const PULLS_SEARCH_QUERY = /* GraphQL */ `
  query PullsList($q: String!, $cursor: String) {
    search(type: ISSUE, query: $q, first: 30, after: $cursor) {
      issueCount
      pageInfo { hasNextPage endCursor }
      nodes {
        ... on PullRequest {
          number title url updatedAt isDraft reviewDecision
          author { login avatarUrl }
          repository { nameWithOwner }
          labels(first: 10) { nodes { name color } }
          comments { totalCount }
          additions deletions
          commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
        }
      }
    }
  }
`;

export interface Actor {
  login: string;
  avatarUrl: string;
}
export type Label = GhLabel;

export type ReviewDecision = "APPROVED" | "CHANGES_REQUESTED" | "REVIEW_REQUIRED" | null;
/** GitHub's StatusState, reduced to what a dot can show. */
export type CiState = "success" | "failure" | "pending" | null;

export interface PullRow {
  repo: string;
  number: number;
  title: string;
  url: string;
  updatedAt: string;
  isDraft: boolean;
  reviewDecision: ReviewDecision;
  author: Actor | null;
  labels: Label[];
  comments: number;
  additions: number;
  deletions: number;
  ci: CiState;
}

interface SearchNode {
  number: number;
  title: string;
  url: string;
  updatedAt: string;
  isDraft: boolean;
  reviewDecision: ReviewDecision;
  author: Actor | null;
  repository: { nameWithOwner: string };
  labels: { nodes: Label[] } | null;
  comments: { totalCount: number } | null;
  additions: number;
  deletions: number;
  commits: { nodes: { commit: { statusCheckRollup: { state: string } | null } }[] };
}

export interface SearchData {
  search: {
    issueCount: number;
    pageInfo: { hasNextPage: boolean; endCursor: string | null };
    nodes: (SearchNode | Record<string, never> | null)[];
  };
}

/** GitHub `StatusState` → dot color class. `EXPECTED`/`PENDING` → pending; `ERROR` counts as failure. */
export function ciState(state: string | null | undefined): CiState {
  switch (state) {
    case "SUCCESS":
      return "success";
    case "FAILURE":
    case "ERROR":
      return "failure";
    case "PENDING":
    case "EXPECTED":
      return "pending";
    default:
      return null;
  }
}

export function mapSearchNode(n: SearchNode): PullRow {
  return {
    repo: n.repository.nameWithOwner,
    number: n.number,
    title: n.title,
    url: n.url,
    updatedAt: n.updatedAt,
    isDraft: !!n.isDraft,
    reviewDecision: n.reviewDecision ?? null,
    author: n.author ?? null,
    labels: n.labels?.nodes ?? [],
    comments: n.comments?.totalCount ?? 0,
    additions: n.additions ?? 0,
    deletions: n.deletions ?? 0,
    ci: ciState(n.commits?.nodes?.[0]?.commit?.statusCheckRollup?.state),
  };
}

export interface PullsPage {
  rows: PullRow[];
  total: number;
  endCursor: string | null;
  hasNextPage: boolean;
}

/** Map one search response page; skips non-PR nodes (search can return nulls on permissions). */
export function mapSearchPage(data: SearchData): PullsPage {
  const rows: PullRow[] = [];
  for (const n of data.search.nodes ?? []) {
    if (n && "number" in n && typeof n.number === "number") rows.push(mapSearchNode(n as SearchNode));
  }
  return {
    rows,
    total: data.search.issueCount ?? rows.length,
    endCursor: data.search.pageInfo?.endCursor ?? null,
    hasNextPage: !!data.search.pageInfo?.hasNextPage,
  };
}

export async function fetchPulls(client: GitHubClient, q: string, cursor: string | null, opts?: ReadOptions): Promise<PullsPage> {
  const data = await client.graphql<SearchData>(PULLS_SEARCH_QUERY, { q, cursor }, opts);
  return mapSearchPage(data);
}

/** Merge a next page into the list, dropping any row already present (search pages can overlap). */
export function appendRows(prev: PullRow[], next: PullRow[]): PullRow[] {
  const seen = new Set(prev.map((r) => `${r.repo}#${r.number}`));
  return [...prev, ...next.filter((r) => !seen.has(`${r.repo}#${r.number}`))];
}

/** Distinct `owner/repo` values in display order (most rows first, then name). */
export function repoChips(rows: PullRow[]): { repo: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const r of rows) counts.set(r.repo, (counts.get(r.repo) ?? 0) + 1);
  return [...counts.entries()]
    .map(([repo, count]) => ({ repo, count }))
    .sort((a, b) => b.count - a.count || a.repo.localeCompare(b.repo));
}

// --- Detail ---------------------------------------------------------------------------------------

/** Comments and reviews fetched with the detail (the NEWEST ones) and per "Load earlier" page. */
export const PULL_CONVERSATION_PAGE = 50;

export const PULL_DETAIL_QUERY = /* GraphQL */ `
  query PullDetail($owner: String!, $name: String!, $number: Int!, $last: Int!) {
    repository(owner: $owner, name: $name) {
      pullRequest(number: $number) {
        title body state isDraft merged mergeable reviewDecision url createdAt updatedAt
        author { login avatarUrl }
        headRefName baseRefName additions deletions changedFiles
        labels(first: 20) { nodes { name color } }
        assignees(first: 10) { nodes { login avatarUrl } }
        reviewRequests(first: 10) {
          nodes { requestedReviewer { ... on User { login avatarUrl } ... on Team { name } } }
        }
        latestReviews(first: 20) { nodes { author { login avatarUrl } state submittedAt body url } }
        comments(last: $last) {
          totalCount
          pageInfo { hasPreviousPage startCursor }
          nodes { id author { login avatarUrl } body createdAt url }
        }
        reviews(last: $last) {
          totalCount
          pageInfo { hasPreviousPage startCursor }
          nodes { id author { login avatarUrl } state body submittedAt url }
        }
        commits(last: 1) {
          nodes {
            commit {
              oid
              statusCheckRollup {
                state
                contexts(first: 100) {
                  nodes {
                    __typename
                    ... on CheckRun { name conclusion status detailsUrl }
                    ... on StatusContext { context state targetUrl }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
`;

/**
 * The pages of comments and/or reviews BEFORE the given cursors (`pageInfo.startCursor` of what is
 * on screen) — what "Load earlier" walks. Each connection is fetched only while it still has a
 * cursor (`@include`), so a PR with many comments but few reviews stops asking for reviews.
 */
export const PULL_EARLIER_QUERY = /* GraphQL */ `
  query PullEarlier(
    $owner: String!, $name: String!, $number: Int!, $last: Int!,
    $commentsBefore: String, $withComments: Boolean!,
    $reviewsBefore: String, $withReviews: Boolean!
  ) {
    repository(owner: $owner, name: $name) {
      pullRequest(number: $number) {
        comments(last: $last, before: $commentsBefore) @include(if: $withComments) {
          totalCount
          pageInfo { hasPreviousPage startCursor }
          nodes { id author { login avatarUrl } body createdAt url }
        }
        reviews(last: $last, before: $reviewsBefore) @include(if: $withReviews) {
          totalCount
          pageInfo { hasPreviousPage startCursor }
          nodes { id author { login avatarUrl } state body submittedAt url }
        }
      }
    }
  }
`;

export type ReviewState = "APPROVED" | "CHANGES_REQUESTED" | "COMMENTED" | "DISMISSED" | "PENDING";

/** A backwards-pageable connection as the detail + earlier queries return it. */
export interface RawConnection<T> {
  totalCount?: number;
  pageInfo?: { hasPreviousPage?: boolean; startCursor?: string | null } | null;
  nodes: (T | null)[] | null;
}

export interface RawComment {
  id: string;
  author: Actor | null;
  body: string;
  createdAt: string;
  url: string;
}
export interface RawReview {
  id: string;
  author: Actor | null;
  state: ReviewState;
  body: string;
  submittedAt: string | null;
  url: string;
}

export interface TimelineItem {
  id: string;
  kind: "comment" | "review";
  author: Actor | null;
  body: string;
  at: string;
  url: string;
  /** Review state (reviews only). */
  state?: ReviewState;
}

/**
 * One chronological timeline from issue comments + submitted reviews. Reviews that carry no body
 * AND are mere `COMMENTED` shells (GitHub emits one per inline-comment batch) are dropped — they'd
 * be empty rows. Unsubmitted `PENDING` reviews (the viewer's own draft) are dropped too.
 */
export function buildTimeline(comments: RawComment[], reviews: RawReview[]): TimelineItem[] {
  const items: TimelineItem[] = comments.map((c) => ({
    id: c.id,
    kind: "comment",
    author: c.author,
    body: c.body ?? "",
    at: c.createdAt,
    url: c.url,
  }));
  for (const r of reviews) {
    if (r.state === "PENDING" || !r.submittedAt) continue;
    const body = (r.body ?? "").trim();
    if (!body && r.state === "COMMENTED") continue;
    items.push({ id: r.id, kind: "review", author: r.author, body: r.body ?? "", at: r.submittedAt, url: r.url, state: r.state });
  }
  return items.sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
}

export type PullState = "open" | "draft" | "merged" | "closed";

export interface Reviewer {
  login: string;
  avatarUrl: string;
  /** Latest review state, or "REQUESTED" when only a request exists. */
  state: ReviewState | "REQUESTED";
}

export interface CheckContext {
  name: string;
  /** Normalized outcome: success / failure / pending / neutral / skipped / cancelled … */
  status: string;
  url: string | null;
}

export interface PullDetail {
  repo: string;
  number: number;
  title: string;
  body: string;
  state: PullState;
  isDraft: boolean;
  merged: boolean;
  mergeable: "MERGEABLE" | "CONFLICTING" | "UNKNOWN";
  reviewDecision: ReviewDecision;
  url: string;
  createdAt: string;
  updatedAt: string;
  author: Actor | null;
  headRefName: string;
  baseRefName: string;
  additions: number;
  deletions: number;
  changedFiles: number;
  labels: Label[];
  assignees: Actor[];
  /** Requested team reviewers (names). */
  teamRequests: string[];
  reviewers: Reviewer[];
  /** Derived from `conversation` (see buildTimeline) — recomputed when an earlier page lands. */
  timeline: TimelineItem[];
  /** The loaded comments + reviews (newest pages first fetched; earlier pages merged in) and their paging state. */
  conversation: Conversation;
  headOid: string | null;
  ciState: CiState;
  ciRaw: string | null;
  checks: CheckContext[];
}

interface RawRollupContext {
  __typename: "CheckRun" | "StatusContext";
  name?: string;
  conclusion?: string | null;
  status?: string;
  detailsUrl?: string | null;
  context?: string;
  state?: string;
  targetUrl?: string | null;
}

/**
 * The raw comments + reviews behind the timeline, with how many exist on GitHub and the `before:`
 * cursors for the pages preceding the oldest loaded of each (null = everything is loaded).
 */
export interface Conversation {
  comments: RawComment[];
  reviews: RawReview[];
  commentsTotal: number;
  reviewsTotal: number;
  commentsBefore: string | null;
  reviewsBefore: string | null;
}

/** Items on GitHub that are NOT loaded yet (comments + reviews). */
export function notLoadedCount(c: Conversation): number {
  return Math.max(0, c.commentsTotal - c.comments.length) + Math.max(0, c.reviewsTotal - c.reviews.length);
}

/** True while either connection still has an earlier page to walk. */
export function hasEarlier(c: Conversation): boolean {
  return c.commentsBefore !== null || c.reviewsBefore !== null;
}

function connectionPage<T extends { id: string }>(conn: RawConnection<T> | null | undefined): { nodes: T[]; total: number; before: string | null } {
  const nodes = (conn?.nodes ?? []).filter((n): n is T => !!n);
  const info = conn?.pageInfo;
  return {
    nodes,
    total: Math.max(conn?.totalCount ?? 0, nodes.length),
    before: info?.hasPreviousPage && info.startCursor ? info.startCursor : null,
  };
}

/** Map the detail's two connections into the initial conversation state. */
export function mapConversation(comments: RawConnection<RawComment> | null | undefined, reviews: RawConnection<RawReview> | null | undefined): Conversation {
  const c = connectionPage(comments);
  const r = connectionPage(reviews);
  return {
    comments: c.nodes,
    reviews: r.nodes,
    commentsTotal: c.total,
    reviewsTotal: r.total,
    commentsBefore: c.before,
    reviewsBefore: r.before,
  };
}

/** Union by id, incoming wins (edits), chronological by `at(x)`. */
function unionById<T extends { id: string }>(existing: T[], incoming: T[], at: (x: T) => string | null): T[] {
  const byId = new Map<string, T>();
  for (const x of existing) byId.set(x.id, x);
  for (const x of incoming) byId.set(x.id, x);
  return [...byId.values()].sort((a, b) => (at(a) ?? "").localeCompare(at(b) ?? "") || a.id.localeCompare(b.id));
}

/** The raw shape of a PULL_EARLIER_QUERY response: each connection present only when it was asked for. */
export interface PullEarlierData {
  repository: {
    pullRequest: {
      comments?: RawConnection<RawComment> | null;
      reviews?: RawConnection<RawReview> | null;
    } | null;
  } | null;
}

/**
 * Stitch an earlier page onto the conversation: each connection that came back is merged by id
 * and its cursor moved back (a connection NOT in the response keeps its state untouched).
 */
export function withEarlierConversation(prev: Conversation, data: PullEarlierData): Conversation {
  const pr = data.repository?.pullRequest;
  let next = prev;
  if (pr?.comments !== undefined) {
    const c = connectionPage(pr.comments);
    next = {
      ...next,
      comments: unionById(next.comments, c.nodes, (x) => x.createdAt),
      commentsTotal: Math.max(next.commentsTotal, c.total),
      commentsBefore: c.before,
    };
  }
  if (pr?.reviews !== undefined) {
    const r = connectionPage(pr.reviews);
    next = {
      ...next,
      reviews: unionById(next.reviews, r.nodes, (x) => x.submittedAt),
      reviewsTotal: Math.max(next.reviewsTotal, r.total),
      reviewsBefore: r.before,
    };
  }
  return next;
}

/**
 * A reload after a write re-reads only the NEWEST pages. Keep the earlier pages already on screen:
 * merge both connections and keep the OLDER cursor of each (the fresh page's cursor points before
 * its own first item, which we may already have).
 */
export function mergeRefreshedConversation(prev: Conversation | null, next: Conversation): Conversation {
  if (!prev) return next;
  const olderCursor = <T extends { id: string }>(
    prevItems: T[], nextItems: T[], at: (x: T) => string | null, prevCursor: string | null, nextCursor: string | null,
  ) => {
    const p = prevItems[0] ? at(prevItems[0]) : null;
    const n = nextItems[0] ? at(nextItems[0]) : null;
    return p && n && p < n ? prevCursor : nextCursor;
  };
  return {
    comments: unionById(prev.comments, next.comments, (x) => x.createdAt),
    reviews: unionById(prev.reviews, next.reviews, (x) => x.submittedAt),
    commentsTotal: Math.max(prev.commentsTotal, next.commentsTotal),
    reviewsTotal: Math.max(prev.reviewsTotal, next.reviewsTotal),
    commentsBefore: olderCursor(prev.comments, next.comments, (x) => x.createdAt, prev.commentsBefore, next.commentsBefore),
    reviewsBefore: olderCursor(prev.reviews, next.reviews, (x) => x.submittedAt, prev.reviewsBefore, next.reviewsBefore),
  };
}

/** The detail with a (merged) conversation swapped in and its timeline rebuilt. */
export function withConversation(pr: PullDetail, conversation: Conversation): PullDetail {
  return { ...pr, conversation, timeline: buildTimeline(conversation.comments, conversation.reviews) };
}

export interface PullDetailData {
  repository: {
    pullRequest: {
      title: string;
      body: string | null;
      state: "OPEN" | "CLOSED" | "MERGED";
      isDraft: boolean;
      merged: boolean;
      mergeable: "MERGEABLE" | "CONFLICTING" | "UNKNOWN";
      reviewDecision: ReviewDecision;
      url: string;
      createdAt: string;
      updatedAt: string;
      author: Actor | null;
      headRefName: string;
      baseRefName: string;
      additions: number;
      deletions: number;
      changedFiles: number;
      labels: { nodes: Label[] } | null;
      assignees: { nodes: Actor[] } | null;
      reviewRequests: { nodes: { requestedReviewer: (Actor & { name?: undefined }) | { name: string } | null }[] } | null;
      latestReviews: { nodes: Omit<RawReview, "id">[] } | null;
      comments: RawConnection<RawComment> | null;
      reviews: RawConnection<RawReview> | null;
      commits: {
        nodes: {
          commit: {
            oid: string;
            statusCheckRollup: { state: string; contexts: { nodes: RawRollupContext[] } | null } | null;
          };
        }[];
      } | null;
    } | null;
  } | null;
}

/** CheckRun conclusion/status or StatusContext state → one lowercase status word. */
export function checkStatus(c: RawRollupContext): string {
  if (c.__typename === "StatusContext") return (c.state ?? "pending").toLowerCase();
  if (c.status && c.status !== "COMPLETED") return "pending";
  return (c.conclusion ?? "pending").toLowerCase();
}

export function mapChecks(nodes: RawRollupContext[] | undefined | null): CheckContext[] {
  return (nodes ?? []).map((c) => ({
    name: (c.__typename === "StatusContext" ? c.context : c.name) ?? "check",
    status: checkStatus(c),
    url: (c.__typename === "StatusContext" ? c.targetUrl : c.detailsUrl) ?? null,
  }));
}

export function pullState(pr: { state: string; isDraft: boolean; merged: boolean }): PullState {
  if (pr.merged || pr.state === "MERGED") return "merged";
  if (pr.state === "CLOSED") return "closed";
  return pr.isDraft ? "draft" : "open";
}

/**
 * Reviewers = latest review per author ∪ outstanding requests. A requested reviewer who already
 * reviewed keeps their review state (re-request after changes resets to REQUESTED on GitHub, which
 * surfaces here because the latest review is then dismissed/stale — we show REQUESTED).
 */
export function mergeReviewers(
  latest: { author: Actor | null; state: ReviewState }[],
  requests: ({ login?: string; avatarUrl?: string; name?: string } | null)[],
): { reviewers: Reviewer[]; teamRequests: string[] } {
  const byLogin = new Map<string, Reviewer>();
  for (const r of latest) {
    if (!r.author || r.state === "PENDING") continue;
    byLogin.set(r.author.login, { login: r.author.login, avatarUrl: r.author.avatarUrl, state: r.state });
  }
  const teamRequests: string[] = [];
  for (const q of requests) {
    if (!q) continue;
    if ("login" in q && q.login) {
      byLogin.set(q.login, { login: q.login, avatarUrl: q.avatarUrl ?? "", state: "REQUESTED" });
    } else if ("name" in q && q.name) {
      teamRequests.push(q.name);
    }
  }
  return { reviewers: [...byLogin.values()], teamRequests };
}

export function mapPullDetail(data: PullDetailData, repo: string, number: number): PullDetail {
  const pr = data.repository?.pullRequest;
  if (!pr) throw new Error(`Pull request ${repo}#${number} not found`);
  const head = pr.commits?.nodes?.[0]?.commit ?? null;
  const rollup = head?.statusCheckRollup ?? null;
  const { reviewers, teamRequests } = mergeReviewers(
    pr.latestReviews?.nodes ?? [],
    (pr.reviewRequests?.nodes ?? []).map((n) => n.requestedReviewer),
  );
  const conversation = mapConversation(pr.comments, pr.reviews);
  return {
    repo,
    number,
    title: pr.title,
    body: pr.body ?? "",
    state: pullState(pr),
    isDraft: !!pr.isDraft,
    merged: !!pr.merged,
    mergeable: pr.mergeable ?? "UNKNOWN",
    reviewDecision: pr.reviewDecision ?? null,
    url: pr.url,
    createdAt: pr.createdAt,
    updatedAt: pr.updatedAt,
    author: pr.author ?? null,
    headRefName: pr.headRefName,
    baseRefName: pr.baseRefName,
    additions: pr.additions ?? 0,
    deletions: pr.deletions ?? 0,
    changedFiles: pr.changedFiles ?? 0,
    labels: pr.labels?.nodes ?? [],
    assignees: pr.assignees?.nodes ?? [],
    teamRequests,
    reviewers,
    timeline: buildTimeline(conversation.comments, conversation.reviews),
    conversation,
    headOid: head?.oid ?? null,
    ciState: ciState(rollup?.state),
    ciRaw: rollup?.state ?? null,
    checks: mapChecks(rollup?.contexts?.nodes),
  };
}

export function splitRepo(repo: string): { owner: string; name: string } {
  const [owner = "", name = ""] = repo.split("/");
  return { owner, name };
}

export async function fetchPullDetail(client: GitHubClient, repo: string, number: number, opts?: ReadOptions): Promise<PullDetail> {
  const { owner, name } = splitRepo(repo);
  const data = await client.graphql<PullDetailData>(PULL_DETAIL_QUERY, { owner, name, number, last: PULL_CONVERSATION_PAGE }, opts);
  return mapPullDetail(data, repo, number);
}

/** Variables for PULL_EARLIER_QUERY given the conversation's cursors (a null cursor skips that connection). */
export function earlierVariables(repo: string, number: number, c: Conversation): Record<string, unknown> {
  const { owner, name } = splitRepo(repo);
  return {
    owner,
    name,
    number,
    last: PULL_CONVERSATION_PAGE,
    commentsBefore: c.commentsBefore,
    withComments: c.commentsBefore !== null,
    reviewsBefore: c.reviewsBefore,
    withReviews: c.reviewsBefore !== null,
  };
}

/**
 * Fetch the raw page(s) before the oldest loaded comment / review of `c` (only the connections that
 * still have a cursor are asked for). The merge is the caller's (`withEarlierConversation`) so it can
 * stitch the result onto its CURRENT state — a quiet reload may have landed while this was in flight.
 */
export async function fetchEarlierPages(client: GitHubClient, repo: string, number: number, c: Conversation): Promise<PullEarlierData> {
  if (!hasEarlier(c)) return { repository: { pullRequest: {} } };
  return client.graphql<PullEarlierData>(PULL_EARLIER_QUERY, earlierVariables(repo, number, c));
}

// --- Files (REST) ---------------------------------------------------------------------------------

export type FileStatus = "added" | "removed" | "modified" | "renamed" | "copied" | "changed" | "unchanged";

export interface PullFile {
  sha: string;
  filename: string;
  previousFilename: string | null;
  status: FileStatus;
  additions: number;
  deletions: number;
  changes: number;
  /** Unified diff body; absent for binary / very large files. */
  patch: string | null;
  blobUrl: string;
}

interface RestFile {
  sha: string;
  filename: string;
  previous_filename?: string;
  status: FileStatus;
  additions: number;
  deletions: number;
  changes: number;
  patch?: string;
  blob_url: string;
}

export const FILES_PER_PAGE = 100;
export const FILES_CAP = 300;

export function mapFile(f: RestFile): PullFile {
  return {
    sha: f.sha,
    filename: f.filename,
    previousFilename: f.previous_filename ?? null,
    status: f.status,
    additions: f.additions ?? 0,
    deletions: f.deletions ?? 0,
    changes: f.changes ?? 0,
    patch: f.patch ?? null,
    blobUrl: f.blob_url,
  };
}

export function filesPath(repo: string, number: number, page: number): string {
  return `/repos/${repo}/pulls/${number}/files?per_page=${FILES_PER_PAGE}&page=${page}`;
}

/** All changed files, paging until a short page or {@link FILES_CAP}. `truncated` when capped. */
export async function fetchPullFiles(
  client: GitHubClient,
  repo: string,
  number: number,
  opts?: ReadOptions,
): Promise<{ files: PullFile[]; truncated: boolean }> {
  const files: PullFile[] = [];
  for (let page = 1; files.length < FILES_CAP; page++) {
    const batch = await client.rest<RestFile[]>("GET", filesPath(repo, number, page), undefined, undefined, opts);
    files.push(...batch.map(mapFile));
    if (batch.length < FILES_PER_PAGE) return { files, truncated: false };
  }
  return { files: files.slice(0, FILES_CAP), truncated: true };
}

// --- Writes (the user's own decisions, under their token) ----------------------------------------

export type ReviewEvent = "APPROVE" | "REQUEST_CHANGES" | "COMMENT";

export function submitReview(client: GitHubClient, repo: string, number: number, event: ReviewEvent, body: string): Promise<unknown> {
  return client.rest("POST", `/repos/${repo}/pulls/${number}/reviews`, { event, body });
}

export interface LineCommentInput {
  body: string;
  commitId: string;
  path: string;
  line: number;
  side: "LEFT" | "RIGHT";
}

export function postLineComment(client: GitHubClient, repo: string, number: number, c: LineCommentInput): Promise<unknown> {
  return client.rest("POST", `/repos/${repo}/pulls/${number}/comments`, {
    body: c.body,
    commit_id: c.commitId,
    path: c.path,
    line: c.line,
    side: c.side,
  });
}
