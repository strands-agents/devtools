import { describe, expect, it } from "vitest";
import {
  buildIssueSearch,
  filterQualifier,
  isNotFoundError,
  issueRestPath,
  mapIssueDetail,
  mapIssueNode,
  mapIssuePage,
  mergeRows,
  repoChipLabel,
  repoFacets,
  type IssueDetailData,
  type IssueListData,
  ISSUE_COMMENTS_PAGE,
  ISSUE_DETAIL_QUERY,
  ISSUE_COMMENTS_BEFORE_QUERY,
  mapCommentsPage,
  mergeComments,
  mergeRefreshedDetail,
  withEarlierComments,
  type IssueComment,
  type IssueDetail,
} from "./data";

const orgs = ["strands-agents", "strands-labs"];

describe("buildIssueSearch", () => {
  it("All: scope only", () => {
    expect(buildIssueSearch({ orgs, filter: "all", login: "me" })).toBe(
      "is:issue is:open sort:updated-desc org:strands-agents org:strands-labs",
    );
  });
  it.each([
    ["involved", "involves:me"],
    ["assigned", "assignee:me"],
    ["mine", "author:me"],
    ["mentioned", "mentions:me"],
  ] as const)("%s → %s", (filter, qualifier) => {
    expect(filterQualifier(filter, "me")).toBe(qualifier);
    expect(buildIssueSearch({ orgs, filter, login: "me" })).toContain(` ${qualifier}`);
  });
  it("untriaged is viewer-independent", () => {
    expect(filterQualifier("untriaged", null)).toBe("no:label");
    expect(buildIssueSearch({ orgs, filter: "untriaged" })).toMatch(/ no:label$/);
  });
  it("viewer-dependent filters degrade to All without a login", () => {
    expect(filterQualifier("involved", null)).toBe("");
    expect(buildIssueSearch({ orgs, filter: "mine", login: undefined })).toBe(
      buildIssueSearch({ orgs, filter: "all" }),
    );
  });
  it("appends collapsed free text last", () => {
    expect(buildIssueSearch({ orgs: ["o"], filter: "all", text: "  crash   on  start " })).toBe(
      "is:issue is:open sort:updated-desc org:o crash on start",
    );
  });
});

describe("mapping", () => {
  const node = {
    number: 7,
    title: "Boom",
    url: "https://github.com/o/r/issues/7",
    updatedAt: "2024-01-02T00:00:00Z",
    createdAt: "2024-01-01T00:00:00Z",
    state: "OPEN",
    author: { login: "ann", avatarUrl: "a.png" },
    repository: { nameWithOwner: "o/r" },
    labels: { nodes: [{ name: "bug", color: "d73a4a" }, null] },
    comments: { totalCount: 3 },
    assignees: { nodes: [{ login: "bob", avatarUrl: "b.png" }, null] },
  };

  it("maps a search node to a row", () => {
    expect(mapIssueNode(node)).toEqual({
      repo: "o/r",
      number: 7,
      title: "Boom",
      url: "https://github.com/o/r/issues/7",
      state: "OPEN",
      createdAt: "2024-01-01T00:00:00Z",
      updatedAt: "2024-01-02T00:00:00Z",
      author: { login: "ann", avatarUrl: "a.png" },
      labels: [{ name: "bug", color: "d73a4a" }],
      commentCount: 3,
      assignees: [{ login: "bob", avatarUrl: "b.png" }],
    });
  });
  it("drops empty / non-issue nodes and ghost authors become null", () => {
    expect(mapIssueNode({})).toBeNull();
    expect(mapIssueNode(null)).toBeNull();
    expect(mapIssueNode({ ...node, author: null })?.author).toBeNull();
  });
  it("maps a page with pageInfo", () => {
    const d: IssueListData = {
      search: { issueCount: 99, pageInfo: { hasNextPage: true, endCursor: "C" }, nodes: [node, {}, null] },
    };
    const p = mapIssuePage(d);
    expect(p.rows).toHaveLength(1);
    expect(p.total).toBe(99);
    expect(p.hasNextPage).toBe(true);
    expect(p.endCursor).toBe("C");
  });
  it("mergeRows dedups by repo#number", () => {
    const a = mapIssueNode(node)!;
    const b = mapIssueNode({ ...node, number: 8 })!;
    expect(mergeRows([a], [a, b]).map((r) => r.number)).toEqual([7, 8]);
  });
  it("repoFacets counts and orders", () => {
    const a = mapIssueNode(node)!;
    const b = mapIssueNode({ ...node, number: 8, repository: { nameWithOwner: "o/z" } })!;
    const c = mapIssueNode({ ...node, number: 9, repository: { nameWithOwner: "o/z" } })!;
    expect(repoFacets([a, b, c])).toEqual([
      { repo: "o/z", count: 2 },
      { repo: "o/r", count: 1 },
    ]);
  });
  it("repoChipLabel shortens unless two orgs share a repo name", () => {
    const facets = [{ repo: "a/docs" }, { repo: "b/docs" }, { repo: "a/sdk" }];
    expect(repoChipLabel("a/sdk", facets)).toBe("sdk");
    expect(repoChipLabel("a/docs", facets)).toBe("a/docs");
    expect(repoChipLabel("b/docs", facets)).toBe("b/docs");
    expect(repoChipLabel("solo", [{ repo: "solo" }])).toBe("solo");
  });
  it("maps detail incl. repo labels, milestone, comments", () => {
    const d: IssueDetailData = {
      repository: {
        issue: {
          title: "T",
          body: null,
          state: "CLOSED",
          createdAt: "c",
          updatedAt: "u",
          url: "https://github.com/o/r/issues/1",
          author: { login: "ann", avatarUrl: "" },
          labels: { nodes: [{ name: "bug", color: "#d73a4a" }] },
          assignees: { nodes: [] },
          milestone: { title: "v1" },
          comments: {
            totalCount: 250,
            nodes: [{ id: "x", author: null, body: "hi", createdAt: "t", url: "u1" }, null],
          },
        },
        labels: { nodes: [{ name: "bug", color: "d73a4a" }, { name: "docs", color: "0075ca" }] },
      },
    };
    const m = mapIssueDetail(d, "o/r", 1)!;
    expect(m.state).toBe("CLOSED");
    expect(m.body).toBe("");
    expect(m.labels).toEqual([{ name: "bug", color: "d73a4a" }]);
    expect(m.milestone).toBe("v1");
    expect(m.comments).toEqual([{ id: "x", author: null, body: "hi", createdAt: "t", url: "u1" }]);
    expect(m.commentsTotal).toBe(250);
    expect(m.repoLabels.map((l) => l.name)).toEqual(["bug", "docs"]);
  });
  it("commentsTotal never undercounts the loaded comments", () => {
    const d: IssueDetailData = {
      repository: {
        issue: {
          title: "T",
          body: "",
          state: "OPEN",
          createdAt: "c",
          updatedAt: "u",
          url: "",
          author: null,
          labels: null,
          assignees: null,
          milestone: null,
          comments: { nodes: [{ id: "x", author: null, body: "", createdAt: "t", url: "" }] },
        },
        labels: null,
      },
    };
    expect(mapIssueDetail(d, "o/r", 1)!.commentsTotal).toBe(1);
  });
  it("isNotFoundError recognises GitHub's resolve errors only", () => {
    expect(isNotFoundError("GitHub GraphQL: Could not resolve to an Issue with the number of 9.")).toBe(true);
    expect(isNotFoundError("Could not resolve to a Repository with the name 'o/r'.")).toBe(true);
    expect(isNotFoundError("GitHub GraphQL 401: Bad credentials")).toBe(false);
    expect(isNotFoundError("API rate limit exceeded")).toBe(false);
  });
  it("detail is null for a missing issue", () => {
    expect(mapIssueDetail({ repository: { issue: null, labels: null } }, "o/r", 1)).toBeNull();
    expect(mapIssueDetail({ repository: null }, "o/r", 1)).toBeNull();
  });
  it("issueRestPath", () => {
    expect(issueRestPath("o/r", 12)).toBe("/repos/o/r/issues/12");
  });
});

describe("comment paging", () => {
  const c = (id: string, createdAt: string): IssueComment => ({ id, author: null, body: id, createdAt, url: "" });
  const detail = (comments: IssueComment[], earlierCursor: string | null, commentsTotal = 300): IssueDetail => ({
    repo: "o/r",
    number: 1,
    title: "",
    body: "",
    state: "OPEN",
    createdAt: "",
    updatedAt: "",
    url: "",
    author: null,
    labels: [],
    assignees: [],
    milestone: null,
    comments,
    commentsTotal,
    earlierCursor,
    repoLabels: [],
  });

  it("the detail asks for a bounded newest page with a backwards cursor", () => {
    expect(ISSUE_COMMENTS_PAGE).toBe(50);
    expect(ISSUE_DETAIL_QUERY).toMatch(/comments\(last: \$commentsLast\)/);
    expect(ISSUE_DETAIL_QUERY).toMatch(/pageInfo \{ hasPreviousPage startCursor \}/);
    expect(ISSUE_COMMENTS_BEFORE_QUERY).toMatch(/comments\(last: \$last, before: \$before\)/);
  });

  it("mapCommentsPage exposes the earlier cursor only when GitHub has a previous page", () => {
    const nodes = [{ id: "a", author: null, body: "x", createdAt: "t1", url: "u" }];
    expect(mapCommentsPage({ totalCount: 80, pageInfo: { hasPreviousPage: true, startCursor: "S" }, nodes })).toEqual({
      comments: [{ id: "a", author: null, body: "x", createdAt: "t1", url: "u" }],
      totalCount: 80,
      earlierCursor: "S",
    });
    expect(mapCommentsPage({ pageInfo: { hasPreviousPage: false, startCursor: "S" }, nodes }).earlierCursor).toBeNull();
    expect(mapCommentsPage({ nodes: [null] })).toEqual({ comments: [], totalCount: 0, earlierCursor: null });
    expect(mapCommentsPage(null).earlierCursor).toBeNull();
  });

  it("mergeComments unions by id oldest-first, incoming wins, local placeholders dropped", () => {
    const merged = mergeComments(
      [c("local-1", "2026-01-05"), c("c3", "2026-01-03"), c("c4", "2026-01-04")],
      [c("c1", "2026-01-01"), c("c2", "2026-01-02"), { ...c("c3", "2026-01-03"), body: "edited" }],
    );
    expect(merged.map((x) => x.id)).toEqual(["c1", "c2", "c3", "c4"]);
    expect(merged[2].body).toBe("edited");
  });

  it("withEarlierComments prepends a page without duplicates and moves the cursor back", () => {
    const issue = detail([c("c5", "2026-01-05"), c("c6", "2026-01-06")], "CUR5");
    const next = withEarlierComments(issue, {
      comments: [c("c3", "2026-01-03"), c("c4", "2026-01-04"), c("c5", "2026-01-05")],
      totalCount: 300,
      earlierCursor: "CUR3",
    });
    expect(next.comments.map((x) => x.id)).toEqual(["c3", "c4", "c5", "c6"]);
    expect(next.earlierCursor).toBe("CUR3");
    const done = withEarlierComments(next, { comments: [c("c1", "2026-01-01")], totalCount: 300, earlierCursor: null });
    expect(done.earlierCursor).toBeNull();
    expect(done.comments[0].id).toBe("c1");
  });

  it("mergeRefreshedDetail keeps earlier pages + the older cursor across a quiet refresh", () => {
    const prev = detail([c("c3", "2026-01-03"), c("c4", "2026-01-04"), c("c5", "2026-01-05"), c("local-9", "2026-01-09")], "CUR3");
    const fresh = detail([c("c5", "2026-01-05"), c("c6", "2026-01-06")], "CUR5", 301);
    const out = mergeRefreshedDetail(prev, fresh);
    expect(out.comments.map((x) => x.id)).toEqual(["c3", "c4", "c5", "c6"]);
    expect(out.earlierCursor).toBe("CUR3");
    expect(out.commentsTotal).toBe(301);
    // First load / different issue: the fresh detail as-is.
    expect(mergeRefreshedDetail(null, fresh)).toBe(fresh);
    expect(mergeRefreshedDetail({ ...prev, number: 2 }, fresh)).toBe(fresh);
    // Nothing earlier loaded yet: the fresh page's cursor wins.
    expect(mergeRefreshedDetail(detail([c("c5", "2026-01-05")], "CUR5"), fresh).earlierCursor).toBe("CUR5");
  });
});
