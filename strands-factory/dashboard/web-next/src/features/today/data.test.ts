import { configurePortalIdentity } from "../../lib/team";
import { beforeEach, describe, expect, it } from "vitest";
import {
  bucketQuery,
  buildTodayQuery,
  buildTodayVariables,
  cappedReasons,
  filterRows,
  mergeBuckets,
  reasonCounts,
  sortRows,
  type SearchNode,
  type TodayResponse,
} from "./data";

const ORGS = ["strands-agents", "strands-labs"];

function node(over: Partial<SearchNode> & { number: number }): SearchNode {
  return {
    __typename: "Issue",
    title: `Item ${over.number}`,
    url: `https://github.com/strands-agents/sdk/issues/${over.number}`,
    updatedAt: "2026-01-01T00:00:00Z",
    author: { login: "alice", avatarUrl: "https://a/alice" },
    repository: { nameWithOwner: "strands-agents/sdk" },
    comments: { totalCount: 3 },
    ...over,
  };
}

describe("query builder", () => {
  it("scopes every bucket to every org, open, updated-desc", () => {
    for (const r of ["mentioned", "my-pr", "strandly-pr", "assigned", "review-requested"] as const) {
      const q = bucketQuery(r, "me", ORGS);
      expect(q).toContain("org:strands-agents");
      expect(q).toContain("org:strands-labs");
      expect(q).toContain("is:open");
      expect(q).toContain("sort:updated-desc");
    }
  });

  it("uses the right qualifier per bucket", () => {
    expect(bucketQuery("mentioned", "me", ORGS)).toContain("mentions:me");
    expect(bucketQuery("my-pr", "me", ORGS)).toMatch(/is:pr.*author:me/);
    expect(bucketQuery("strandly-pr", "me", ORGS)).toMatch(/is:pr.*author:example-agent/);
    expect(bucketQuery("assigned", "me", ORGS)).toContain("assignee:me");
    expect(bucketQuery("review-requested", "me", ORGS)).toContain("review-requested:me");
  });

  it("selects only what the row renders (no nested label connection — search cost)", () => {
    expect(buildTodayQuery()).not.toMatch(/labels\(/);
  });

  it("emits one document with five aliased search fields, first: 40, and the union fragments", () => {
    const q = buildTodayQuery();
    const aliases = ["mentioned", "myPrs", "strandlyPrs", "assigned", "reviewRequested"];
    for (const a of aliases) {
      expect(q).toContain(`${a}: search(type: ISSUE, first: 40, query: $${a})`);
      expect(q).toContain(`$${a}: String!`);
    }
    expect(q.match(/search\(type: ISSUE/g)).toHaveLength(5);
    expect(q).toContain("__typename");
    expect(q).toContain("... on PullRequest");
    expect(q).toContain("isDraft");
    expect(q).toContain("... on Issue");
    expect(q).toContain("comments { totalCount }");
    expect(q).toContain("issueCount");
    expect(q).toContain("author { login avatarUrl }");
    expect(q).toContain("repository { nameWithOwner }");
  });

  it("variables match the aliases", () => {
    const v = buildTodayVariables("me", ORGS);
    expect(Object.keys(v).sort()).toEqual(["assigned", "mentioned", "myPrs", "reviewRequested", "strandlyPrs"]);
    expect(v.myPrs).toBe(bucketQuery("my-pr", "me", ORGS));
    expect(v.mentioned).toContain("org:strands-labs");
  });
});

describe("mergeBuckets", () => {
  it("dedups repo#number across buckets and unions the reasons", () => {
    const res: TodayResponse = {
      mentioned: { nodes: [node({ number: 1 }), node({ number: 2 })] },
      assigned: { nodes: [node({ number: 1 })] },
      reviewRequested: { nodes: [node({ number: 1, __typename: "PullRequest", isDraft: true })] },
    };
    const rows = mergeBuckets(res);
    expect(rows).toHaveLength(2);
    const one = rows.find((r) => r.number === 1)!;
    expect([...one.reasons].sort()).toEqual(["assigned", "mentioned", "review-requested"]);
    expect(rows.find((r) => r.number === 2)!.reasons).toEqual(new Set(["mentioned"]));
  });

  it("does not collide the same number across different repos", () => {
    const res: TodayResponse = {
      mentioned: { nodes: [node({ number: 7 })] },
      assigned: { nodes: [node({ number: 7, repository: { nameWithOwner: "strands-labs/robots" } })] },
    };
    const rows = mergeBuckets(res);
    expect(rows.map((r) => r.key).sort()).toEqual(["strands-agents/sdk#7", "strands-labs/robots#7"]);
  });

  it("sorts updatedAt desc", () => {
    const res: TodayResponse = {
      mentioned: {
        nodes: [
          node({ number: 1, updatedAt: "2026-01-01T00:00:00Z" }),
          node({ number: 2, updatedAt: "2026-01-03T00:00:00Z" }),
          node({ number: 3, updatedAt: "2026-01-02T00:00:00Z" }),
        ],
      },
    };
    expect(mergeBuckets(res).map((r) => r.number)).toEqual([2, 3, 1]);
  });

  it("keeps the freshest copy of a duplicated item but preserves the reason union", () => {
    const res: TodayResponse = {
      mentioned: { nodes: [node({ number: 1, title: "old", updatedAt: "2026-01-01T00:00:00Z" })] },
      myPrs: {
        nodes: [node({ number: 1, title: "new", __typename: "PullRequest", updatedAt: "2026-01-05T00:00:00Z" })],
      },
    };
    const [row] = mergeBuckets(res);
    expect(row.title).toBe("new");
    expect(row.kind).toBe("pr");
    expect([...row.reasons].sort()).toEqual(["mentioned", "my-pr"]);
  });

  it("maps node fields onto the row and tolerates nulls / missing buckets", () => {
    const res: TodayResponse = {
      mentioned: {
        nodes: [
          null,
          node({
            number: 9,
            __typename: "PullRequest",
            isDraft: true,
            comments: null,
            author: null,
          }),
        ],
      },
      strandlyPrs: null,
    };
    const rows = mergeBuckets(res);
    expect(rows).toHaveLength(1);
    const r = rows[0];
    expect(r).toMatchObject({
      key: "strands-agents/sdk#9",
      kind: "pr",
      repo: "strands-agents/sdk",
      isDraft: true,
      comments: 0,
      author: null,
    });
  });

  it("issues are never drafts even if the field is absent", () => {
    const [r] = mergeBuckets({ assigned: { nodes: [node({ number: 2 })] } });
    expect(r.kind).toBe("issue");
    expect(r.isDraft).toBe(false);
    expect(r.comments).toBe(3);
  });

  it("returns [] for an empty response", () => {
    expect(mergeBuckets({})).toEqual([]);
  });
});

describe("filterRows / reasonCounts / sortRows", () => {
  const rows = mergeBuckets({
    mentioned: { nodes: [node({ number: 1 }), node({ number: 2 })] },
    myPrs: { nodes: [node({ number: 2, __typename: "PullRequest" })] },
    strandlyPrs: { nodes: [node({ number: 3, __typename: "PullRequest" })] },
  });

  it("all passes everything through", () => {
    expect(filterRows(rows, "all")).toHaveLength(3);
  });

  it("a reason filter keeps rows carrying that reason", () => {
    expect(filterRows(rows, "mentioned").map((r) => r.number).sort()).toEqual([1, 2]);
    expect(filterRows(rows, "my-pr").map((r) => r.number)).toEqual([2]);
    expect(filterRows(rows, "assigned")).toEqual([]);
  });

  it("counts reasons across rows", () => {
    expect(reasonCounts(rows)).toEqual({
      mentioned: 2,
      "my-pr": 1,
      "strandly-pr": 1,
      assigned: 0,
      "review-requested": 0,
    });
  });

  it("sortRows is stable on ties and does not mutate", () => {
    const tie = mergeBuckets({
      mentioned: { nodes: [node({ number: 5 }), node({ number: 4 })] },
    });
    const input = [tie[1], tie[0]];
    const sorted = sortRows(input);
    expect(sorted.map((r) => r.number)).toEqual([4, 5]);
    expect(input.map((r) => r.number)).toEqual([5, 4]);
  });
});

describe("cappedReasons", () => {
  it("flags buckets whose page was full (issueCount > returned nodes), ignoring nulls", () => {
    const res: TodayResponse = {
      mentioned: { issueCount: 41, nodes: [node({ number: 1 })] },
      myPrs: { issueCount: 1, nodes: [node({ number: 2 })] },
      assigned: { issueCount: 2, nodes: [null, node({ number: 3 })] },
      strandlyPrs: { nodes: [node({ number: 4 })] },
      reviewRequested: null,
    };
    expect(cappedReasons(res)).toEqual(["mentioned", "assigned"]);
  });

  it("is empty for an empty response", () => {
    expect(cappedReasons({})).toEqual([]);
  });
});

beforeEach(() => configurePortalIdentity({ agentLogin: "example-agent" }));
