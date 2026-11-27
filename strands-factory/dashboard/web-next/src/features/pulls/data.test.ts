import { configurePortalIdentity } from "../../lib/team";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  appendRows,
  buildSearchQuery,
  buildTimeline,
  checkStatus,
  ciState,
  effectiveFilter,
  fetchPullFiles,
  filesPath,
  filterClause,
  mapChecks,
  mapFile,
  mapPullDetail,
  mapSearchPage,
  mergeReviewers,
  pullState,
  repoChips,
  earlierVariables,
  fetchEarlierPages,
  hasEarlier,
  mapConversation,
  mergeRefreshedConversation,
  notLoadedCount,
  PULL_CONVERSATION_PAGE,
  PULL_DETAIL_QUERY,
  PULL_EARLIER_QUERY,
  withConversation,
  withEarlierConversation,
  type Conversation,
  type PullDetailData,
  type PullRow,
  type RawComment,
  type RawReview,
  type SearchData,
} from "./data";
import type { GitHubClient } from "../../github/types";

describe("buildSearchQuery", () => {
  const orgs = ["strands-agents", "strands-labs"];
  it("scopes every search by org, open PRs, newest-updated first", () => {
    expect(buildSearchQuery(orgs, "all", "me")).toBe(
      "is:pr is:open org:strands-agents org:strands-labs sort:updated-desc",
    );
  });
  it("adds the per-filter qualifier", () => {
    expect(buildSearchQuery(orgs, "mine", "me")).toContain(" author:me ");
    expect(buildSearchQuery(orgs, "review-requested", "me")).toContain(" review-requested:me ");
    expect(buildSearchQuery(orgs, "involved", "me")).toContain(" involves:me ");
    expect(buildSearchQuery(orgs, "mentioned", "me")).toContain(" mentions:me ");
    expect(buildSearchQuery(orgs, "strandly", null)).toContain(" author:example-agent ");
  });
  it("drops viewer filters without a login instead of emitting `author:`", () => {
    expect(filterClause("mine", null)).toBe("");
    expect(buildSearchQuery(orgs, "review-requested", undefined)).toBe(buildSearchQuery(orgs, "all", undefined));
  });
  it("appends trimmed free text before the sort", () => {
    expect(buildSearchQuery(["o"], "all", "me", "  retry   label:bug ")).toBe(
      "is:pr is:open org:o retry label:bug sort:updated-desc",
    );
  });
});

describe("effectiveFilter", () => {
  it("keeps a usable pick", () => {
    expect(effectiveFilter("strandly", false)).toBe("strandly");
    expect(effectiveFilter("mine", true)).toBe("mine");
  });
  it("falls back when a viewer-only pick has no viewer (e.g. persisted, then token removed)", () => {
    expect(effectiveFilter("mine", false)).toBe("all");
    expect(effectiveFilter("review-requested", false)).toBe("all");
  });
  it("defaults to Review requested with a viewer, All without", () => {
    expect(effectiveFilter(null, true)).toBe("review-requested");
    expect(effectiveFilter(null, false)).toBe("all");
  });
});

describe("ciState", () => {
  it("maps GitHub StatusState to a dot", () => {
    expect(ciState("SUCCESS")).toBe("success");
    expect(ciState("FAILURE")).toBe("failure");
    expect(ciState("ERROR")).toBe("failure");
    expect(ciState("PENDING")).toBe("pending");
    expect(ciState("EXPECTED")).toBe("pending");
    expect(ciState(null)).toBeNull();
    expect(ciState(undefined)).toBeNull();
  });
});

const node = (over: Partial<Record<string, unknown>> = {}) => ({
  number: 7,
  title: "Fix",
  url: "https://github.com/o/r/pull/7",
  updatedAt: "2026-01-02T00:00:00Z",
  isDraft: false,
  reviewDecision: "APPROVED",
  author: { login: "a", avatarUrl: "" },
  repository: { nameWithOwner: "o/r" },
  labels: { nodes: [{ name: "bug", color: "ff0000" }] },
  comments: { totalCount: 3 },
  additions: 10,
  deletions: 2,
  commits: { nodes: [{ commit: { statusCheckRollup: { state: "FAILURE" } } }] },
  ...over,
});

describe("mapSearchPage", () => {
  it("maps nodes and skips empty/null ones", () => {
    const data = {
      search: {
        issueCount: 42,
        pageInfo: { hasNextPage: true, endCursor: "c1" },
        nodes: [node(), null, {}, node({ number: 8, commits: { nodes: [] } })],
      },
    } as unknown as SearchData;
    const page = mapSearchPage(data);
    expect(page.total).toBe(42);
    expect(page.hasNextPage).toBe(true);
    expect(page.endCursor).toBe("c1");
    expect(page.rows.map((r) => r.number)).toEqual([7, 8]);
    expect(page.rows[0]).toMatchObject({ repo: "o/r", ci: "failure", labels: [{ name: "bug", color: "ff0000" }], comments: 3 });
    expect(page.rows[1].ci).toBeNull();
  });
});

const row = (repo: string, number: number): PullRow =>
  ({ repo, number, title: "", url: "", updatedAt: "", isDraft: false, reviewDecision: null, author: null, labels: [], comments: 0, additions: 0, deletions: 0, ci: null });

describe("appendRows / repoChips", () => {
  it("dedupes by repo#number when paging", () => {
    const merged = appendRows([row("o/a", 1), row("o/b", 2)], [row("o/b", 2), row("o/c", 3)]);
    expect(merged.map((r) => `${r.repo}#${r.number}`)).toEqual(["o/a#1", "o/b#2", "o/c#3"]);
  });
  it("counts repos, most-first then alphabetical", () => {
    expect(repoChips([row("o/b", 1), row("o/a", 2), row("o/b", 3), row("o/c", 4)])).toEqual([
      { repo: "o/b", count: 2 },
      { repo: "o/a", count: 1 },
      { repo: "o/c", count: 1 },
    ]);
  });
});

describe("buildTimeline", () => {
  const u = { login: "u", avatarUrl: "" };
  const comments: RawComment[] = [
    { id: "c2", author: u, body: "second", createdAt: "2026-01-03T00:00:00Z", url: "" },
    { id: "c1", author: u, body: "first", createdAt: "2026-01-01T00:00:00Z", url: "" },
  ];
  const reviews: RawReview[] = [
    { id: "r1", author: u, state: "APPROVED", body: "", submittedAt: "2026-01-02T00:00:00Z", url: "" },
    { id: "r2", author: u, state: "COMMENTED", body: "  ", submittedAt: "2026-01-02T12:00:00Z", url: "" }, // empty shell
    { id: "r3", author: u, state: "COMMENTED", body: "nit", submittedAt: "2026-01-04T00:00:00Z", url: "" },
    { id: "r4", author: u, state: "PENDING", body: "draft", submittedAt: null, url: "" },
  ];
  it("merges comments + meaningful reviews sorted by time", () => {
    const tl = buildTimeline(comments, reviews);
    expect(tl.map((t) => t.id)).toEqual(["c1", "r1", "c2", "r3"]);
    expect(tl[1]).toMatchObject({ kind: "review", state: "APPROVED" });
    expect(tl[0]).toMatchObject({ kind: "comment", body: "first" });
  });
  it("keeps an empty-body review when its state is opinionated", () => {
    expect(buildTimeline([], [{ id: "x", author: u, state: "CHANGES_REQUESTED", body: "", submittedAt: "2026-01-01T00:00:00Z", url: "" }])).toHaveLength(1);
  });
});

describe("mergeReviewers / pullState / checks", () => {
  it("unions latest reviews with outstanding requests; teams listed separately", () => {
    const { reviewers, teamRequests } = mergeReviewers(
      [
        { author: { login: "a", avatarUrl: "" }, state: "APPROVED" },
        { author: { login: "b", avatarUrl: "" }, state: "CHANGES_REQUESTED" },
        { author: null, state: "APPROVED" },
      ],
      [{ login: "c", avatarUrl: "" }, { login: "b", avatarUrl: "" }, { name: "maintainers" }, null],
    );
    expect(reviewers).toEqual([
      { login: "a", avatarUrl: "", state: "APPROVED" },
      { login: "b", avatarUrl: "", state: "REQUESTED" },
      { login: "c", avatarUrl: "", state: "REQUESTED" },
    ]);
    expect(teamRequests).toEqual(["maintainers"]);
  });
  it("derives the state badge", () => {
    expect(pullState({ state: "OPEN", isDraft: true, merged: false })).toBe("draft");
    expect(pullState({ state: "OPEN", isDraft: false, merged: false })).toBe("open");
    expect(pullState({ state: "MERGED", isDraft: false, merged: true })).toBe("merged");
    expect(pullState({ state: "CLOSED", isDraft: false, merged: false })).toBe("closed");
  });
  it("normalizes check runs and status contexts", () => {
    expect(checkStatus({ __typename: "CheckRun", status: "IN_PROGRESS", conclusion: null })).toBe("pending");
    expect(checkStatus({ __typename: "CheckRun", status: "COMPLETED", conclusion: "SUCCESS" })).toBe("success");
    expect(checkStatus({ __typename: "StatusContext", state: "FAILURE" })).toBe("failure");
    expect(
      mapChecks([
        { __typename: "CheckRun", name: "build", status: "COMPLETED", conclusion: "FAILURE", detailsUrl: "u1" },
        { __typename: "StatusContext", context: "ci/lint", state: "SUCCESS", targetUrl: null },
      ]),
    ).toEqual([
      { name: "build", status: "failure", url: "u1" },
      { name: "ci/lint", status: "success", url: null },
    ]);
  });
});

describe("mapPullDetail", () => {
  const data: PullDetailData = {
    repository: {
      pullRequest: {
        title: "T",
        body: null,
        state: "OPEN",
        isDraft: false,
        merged: false,
        mergeable: "MERGEABLE",
        reviewDecision: "REVIEW_REQUIRED",
        url: "https://github.com/o/r/pull/1",
        createdAt: "2026-01-01T00:00:00Z",
        updatedAt: "2026-01-02T00:00:00Z",
        author: { login: "a", avatarUrl: "" },
        headRefName: "h",
        baseRefName: "main",
        additions: 1,
        deletions: 2,
        changedFiles: 3,
        labels: null,
        assignees: null,
        reviewRequests: null,
        latestReviews: null,
        comments: { nodes: [{ id: "c", author: null, body: "hi", createdAt: "2026-01-01T01:00:00Z", url: "" }] },
        reviews: null,
        commits: {
          nodes: [
            {
              commit: {
                oid: "abc",
                statusCheckRollup: {
                  state: "PENDING",
                  contexts: { nodes: [{ __typename: "CheckRun", name: "t", status: "QUEUED", conclusion: null, detailsUrl: null }] },
                },
              },
            },
          ],
        },
      },
    },
  };
  it("shapes the detail with defaults for null connections", () => {
    const d = mapPullDetail(data, "o/r", 1);
    expect(d).toMatchObject({
      repo: "o/r",
      number: 1,
      body: "",
      state: "open",
      labels: [],
      assignees: [],
      reviewers: [],
      teamRequests: [],
      headOid: "abc",
      ciState: "pending",
      ciRaw: "PENDING",
    });
    expect(d.timeline).toHaveLength(1);
    expect(d.checks).toEqual([{ name: "t", status: "pending", url: null }]);
  });
  it("throws when the PR is missing", () => {
    expect(() => mapPullDetail({ repository: { pullRequest: null } }, "o/r", 9)).toThrow(/not found/);
  });
});

describe("files", () => {
  it("builds the REST path", () => {
    expect(filesPath("o/r", 5, 2)).toBe("/repos/o/r/pulls/5/files?per_page=100&page=2");
  });
  it("maps a REST file (snake_case → camel, missing patch → null)", () => {
    expect(mapFile({ sha: "s", filename: "b.png", status: "added", additions: 0, deletions: 0, changes: 0, blob_url: "u" })).toEqual({
      sha: "s",
      filename: "b.png",
      previousFilename: null,
      status: "added",
      additions: 0,
      deletions: 0,
      changes: 0,
      patch: null,
      blobUrl: "u",
    });
  });
  const file = (i: number) => ({ sha: `${i}`, filename: `f${i}`, status: "modified", additions: 1, deletions: 1, changes: 2, patch: "@@ -1 +1 @@\n-a\n+b", blob_url: "" });
  it("pages until a short page", async () => {
    const rest = vi.fn().mockResolvedValueOnce(Array.from({ length: 100 }, (_, i) => file(i))).mockResolvedValueOnce([file(100)]);
    const client = { rest } as unknown as GitHubClient;
    const r = await fetchPullFiles(client, "o/r", 1);
    expect(r.files).toHaveLength(101);
    expect(r.truncated).toBe(false);
    expect(rest).toHaveBeenCalledTimes(2);
    expect(rest.mock.calls[1][1]).toContain("page=2");
  });
  it("caps at 300 files and flags truncation", async () => {
    const rest = vi.fn().mockResolvedValue(Array.from({ length: 100 }, (_, i) => file(i)));
    const r = await fetchPullFiles({ rest } as unknown as GitHubClient, "o/r", 1);
    expect(r.files).toHaveLength(300);
    expect(r.truncated).toBe(true);
    expect(rest).toHaveBeenCalledTimes(3);
  });
});

describe("conversation paging", () => {
  const c = (id: string, createdAt: string): RawComment => ({ id, author: null, body: id, createdAt, url: "" });
  const r = (id: string, submittedAt: string): RawReview => ({ id, author: null, state: "APPROVED", body: id, submittedAt, url: "" });

  it("the detail fetches the NEWEST page of both connections with backwards cursors", () => {
    expect(PULL_CONVERSATION_PAGE).toBe(50);
    expect(PULL_DETAIL_QUERY).toMatch(/comments\(last: \$last\)/);
    expect(PULL_DETAIL_QUERY).toMatch(/reviews\(last: \$last\)/);
    expect(PULL_DETAIL_QUERY).not.toMatch(/comments\(first/);
    expect(PULL_DETAIL_QUERY).toMatch(/contexts\(first: 100\)/); // checks stay as they were
    expect(PULL_EARLIER_QUERY).toMatch(/comments\(last: \$last, before: \$commentsBefore\) @include\(if: \$withComments\)/);
    expect(PULL_EARLIER_QUERY).toMatch(/reviews\(last: \$last, before: \$reviewsBefore\) @include\(if: \$withReviews\)/);
  });

  it("mapConversation reads totals + earlier cursors (only when a previous page exists)", () => {
    const conv = mapConversation(
      { totalCount: 120, pageInfo: { hasPreviousPage: true, startCursor: "C" }, nodes: [c("c1", "2026-01-01"), null] },
      { totalCount: 1, pageInfo: { hasPreviousPage: false, startCursor: "R" }, nodes: [r("r1", "2026-01-02")] },
    );
    expect(conv).toEqual({
      comments: [c("c1", "2026-01-01")],
      reviews: [r("r1", "2026-01-02")],
      commentsTotal: 120,
      reviewsTotal: 1,
      commentsBefore: "C",
      reviewsBefore: null,
    });
    expect(hasEarlier(conv)).toBe(true);
    expect(notLoadedCount(conv)).toBe(119);
    expect(mapConversation(null, null)).toMatchObject({ commentsBefore: null, reviewsBefore: null, commentsTotal: 0 });
  });

  it("earlierVariables only asks for connections that still have a cursor", () => {
    const conv: Conversation = { comments: [], reviews: [], commentsTotal: 0, reviewsTotal: 0, commentsBefore: "C", reviewsBefore: null };
    expect(earlierVariables("o/r", 7, conv)).toEqual({
      owner: "o",
      name: "r",
      number: 7,
      last: 50,
      commentsBefore: "C",
      withComments: true,
      reviewsBefore: null,
      withReviews: false,
    });
  });

  it("fetchEarlierPages sends PULL_EARLIER_QUERY with the cursor variables, and skips the request with nothing to walk", async () => {
    const conv: Conversation = { comments: [], reviews: [], commentsTotal: 0, reviewsTotal: 0, commentsBefore: "C", reviewsBefore: null };
    const page = { repository: { pullRequest: { comments: { totalCount: 1, pageInfo: { hasPreviousPage: false, startCursor: null }, nodes: [] } } } };
    const graphql = vi.fn().mockResolvedValue(page);
    const client = { graphql } as unknown as GitHubClient;
    await expect(fetchEarlierPages(client, "o/r", 7, conv)).resolves.toBe(page);
    expect(graphql).toHaveBeenCalledWith(PULL_EARLIER_QUERY, earlierVariables("o/r", 7, conv));
    const done = { ...conv, commentsBefore: null };
    const none = await fetchEarlierPages(client, "o/r", 7, done);
    expect(graphql).toHaveBeenCalledTimes(1);
    // An empty result leaves the conversation untouched when stitched on.
    expect(withEarlierConversation(done, none)).toEqual(done);
  });

  it("withEarlierConversation merges a page by id and moves only the returned connections' cursors", () => {
    const prev: Conversation = {
      comments: [c("c5", "2026-01-05"), c("c6", "2026-01-06")],
      reviews: [r("r2", "2026-01-02")],
      commentsTotal: 200,
      reviewsTotal: 2,
      commentsBefore: "C5",
      reviewsBefore: "R2",
    };
    const next = withEarlierConversation(prev, {
      repository: {
        pullRequest: {
          comments: { totalCount: 200, pageInfo: { hasPreviousPage: true, startCursor: "C3" }, nodes: [c("c3", "2026-01-03"), c("c4", "2026-01-04"), c("c5", "2026-01-05")] },
          // reviews not requested this time → untouched
        },
      },
    });
    expect(next.comments.map((x) => x.id)).toEqual(["c3", "c4", "c5", "c6"]);
    expect(next.commentsBefore).toBe("C3");
    expect(next.reviews).toBe(prev.reviews);
    expect(next.reviewsBefore).toBe("R2");
    const done = withEarlierConversation(next, {
      repository: { pullRequest: { reviews: { totalCount: 2, pageInfo: { hasPreviousPage: false, startCursor: null }, nodes: [r("r1", "2026-01-01")] } } },
    });
    expect(done.reviews.map((x) => x.id)).toEqual(["r1", "r2"]);
    expect(done.reviewsBefore).toBeNull();
    expect(hasEarlier(done)).toBe(true); // comments still have C3
  });

  it("mergeRefreshedConversation keeps loaded earlier pages + the older cursor after a reload", () => {
    const prev: Conversation = {
      comments: [c("c3", "2026-01-03"), c("c4", "2026-01-04"), c("c5", "2026-01-05")],
      reviews: [r("r1", "2026-01-01")],
      commentsTotal: 200,
      reviewsTotal: 1,
      commentsBefore: "C3",
      reviewsBefore: null,
    };
    const fresh: Conversation = {
      comments: [c("c5", "2026-01-05"), c("c6", "2026-01-06")],
      reviews: [r("r1", "2026-01-01"), r("r2", "2026-01-07")],
      commentsTotal: 201,
      reviewsTotal: 2,
      commentsBefore: "C5",
      reviewsBefore: null,
    };
    const out = mergeRefreshedConversation(prev, fresh);
    expect(out.comments.map((x) => x.id)).toEqual(["c3", "c4", "c5", "c6"]);
    expect(out.reviews.map((x) => x.id)).toEqual(["r1", "r2"]);
    expect(out.commentsBefore).toBe("C3");
    expect(out.commentsTotal).toBe(201);
    expect(mergeRefreshedConversation(null, fresh)).toBe(fresh);
  });

  it("withConversation rebuilds the timeline from the merged raw items", () => {
    const base = mapPullDetail(
      {
        repository: {
          pullRequest: {
            title: "t", body: null, state: "OPEN", isDraft: false, merged: false, mergeable: "MERGEABLE", reviewDecision: null,
            url: "", createdAt: "", updatedAt: "", author: null, headRefName: "h", baseRefName: "b", additions: 0, deletions: 0, changedFiles: 0,
            labels: null, assignees: null, reviewRequests: null, latestReviews: null,
            comments: { totalCount: 2, pageInfo: { hasPreviousPage: true, startCursor: "C" }, nodes: [c("c2", "2026-01-02")] },
            reviews: null, commits: null,
          },
        },
      },
      "o/r",
      1,
    );
    expect(base.timeline.map((t) => t.id)).toEqual(["c2"]);
    const merged = withConversation(base, { ...base.conversation, comments: [c("c1", "2026-01-01"), c("c2", "2026-01-02")], commentsBefore: null });
    expect(merged.timeline.map((t) => t.id)).toEqual(["c1", "c2"]);
    expect(hasEarlier(merged.conversation)).toBe(false);
  });
});

beforeEach(() => configurePortalIdentity({ agentLogin: "example-agent" }));
