import { describe, expect, it } from "vitest";
import type { SessionSummary } from "../../api/types";
import {
  defaultScope,
  filterSessions,
  inScope,
  itemKindLabel,
  matchesText,
  primaryLabel,
  secondaryText,
  sortByActivity,
  pageWindow,
  SESSIONS_PAGE_SIZE,
  isOwnedBy,
  itemTargets,
  splitItemDraft,
  viewingFor,
} from "./filters";

const s = (session_id: string, extra: Partial<SessionSummary> = {}): SessionSummary => ({
  session_id,
  runs: 1,
  tokens_total: 0,
  ...extra,
});

const mine = s("portal-alice-20260101-0123abcd", { description: "Plan the release\nmore detail", last_activity: "2026-01-03T00:00:00Z" });
const bobs = s("portal-bob-20260102-deadbeef", { description: "", last_activity: "2026-01-02T00:00:00Z" });
const bobSmiths = s("portal-bob-smith-20260102-0badf00d", { last_activity: "2026-01-02T01:00:00Z" });
const issue = s("gh-strands-agents-sdk-python-b00b0499-issue-42", {
  description: "triage: flaky test in model providers",
  target: "https://github.com/strands-agents/sdk-python/issues/42",
  last_activity: "2026-01-04T00:00:00Z",
});
const pr = s("gh-o-r-96458419-pr-7", { last_activity: "2025-12-31T00:00:00Z" });
const foreign = s("something-else");
const all = [mine, bobs, bobSmiths, issue, pr, foreign];

describe("scopes", () => {
  it("defaults to Mine when a viewer exists", () => {
    expect(defaultScope("alice")).toBe("mine");
    expect(defaultScope(null)).toBe("all");
    expect(defaultScope("")).toBe("all");
  });
  it("mine = the viewer's sessions (case-insensitive login)", () => {
    expect(all.filter((x) => inScope(x, "mine", "Alice")).map((x) => x.session_id)).toEqual([mine.session_id]);
    expect(all.filter((x) => inScope(x, "mine", undefined))).toEqual([]);
  });
  it("mine matches the parsed owner exactly — `bob` does not see `bob-smith`'s sessions (or vice versa)", () => {
    expect(all.filter((x) => inScope(x, "mine", "bob"))).toEqual([bobs]);
    expect(all.filter((x) => inScope(x, "mine", "bob-smith"))).toEqual([bobSmiths]);
  });
  it("portal = every portal-* id; items = gh-*; all = everything", () => {
    expect(all.filter((x) => inScope(x, "portal", "alice"))).toEqual([mine, bobs, bobSmiths]);
    expect(all.filter((x) => inScope(x, "items", "alice"))).toEqual([issue, pr]);
    expect(all.filter((x) => inScope(x, "all", null))).toEqual(all);
  });
});

describe("labels", () => {
  it("items get the owner-repo#n label, portal sessions the description's first line", () => {
    expect(primaryLabel(issue)).toBe("strands-agents-sdk-python#42");
    expect(primaryLabel(mine)).toBe("Plan the release");
    expect(primaryLabel(bobs)).toBe("New conversation");
    expect(primaryLabel(foreign)).toBe("something-else");
  });
  it("a title wins over every derived label; a titled item keeps its item label below", () => {
    expect(primaryLabel({ ...issue, title: "Flaky provider test" })).toBe("Flaky provider test");
    expect(primaryLabel({ ...mine, title: "Release prep" })).toBe("Release prep");
    expect(primaryLabel({ ...bobs, title: "  " })).toBe("New conversation");
    expect(secondaryText({ ...issue, title: "Flaky provider test" })).toBe("strands-agents-sdk-python#42");
    expect(secondaryText({ ...mine, title: "Release prep" })).toBe("Plan the release");
    expect(matchesText({ ...mine, title: "Release prep" }, "release prep")).toBe(true);
  });
  it("secondary text never repeats the label", () => {
    expect(secondaryText(issue)).toBe("triage: flaky test in model providers");
    expect(secondaryText(mine)).toBe("");
    expect(secondaryText(s("portal-x-20260101-00000000", { description: "A", latest_prompt: "B" }))).toBe("B");
  });
  it("kind chip", () => {
    expect(itemKindLabel(issue.session_id)).toBe("Issue");
    expect(itemKindLabel(pr.session_id)).toBe("PR");
    expect(itemKindLabel("gh-o-r-96458419-disc-1")).toBe("Discussion");
    expect(itemKindLabel(mine.session_id)).toBeNull();
  });
});

describe("text filter", () => {
  it("empty query matches everything", () => {
    expect(all.filter((x) => matchesText(x, "   "))).toEqual(all);
  });
  it("matches a login (the owner baked into portal ids), case-insensitively, with or without @", () => {
    expect(all.filter((x) => matchesText(x, "BOB"))).toEqual([bobs, bobSmiths]);
    expect(all.filter((x) => matchesText(x, "@bob"))).toEqual([bobs, bobSmiths]);
    expect(all.filter((x) => matchesText(x, "bob-smith"))).toEqual([bobSmiths]);
    expect(all.filter((x) => matchesText(x, "@"))).toEqual(all);
  });
  it("matches id, description, label and target", () => {
    expect(all.filter((x) => matchesText(x, "flaky"))).toEqual([issue]);
    expect(all.filter((x) => matchesText(x, "sdk-python#42"))).toEqual([issue]);
    expect(all.filter((x) => matchesText(x, "github.com/strands-agents"))).toEqual([issue]);
    expect(all.filter((x) => matchesText(x, "release"))).toEqual([mine]);
  });
  it("every term must match", () => {
    expect(all.filter((x) => matchesText(x, "alice release"))).toEqual([mine]);
    expect(all.filter((x) => matchesText(x, "alice flaky"))).toEqual([]);
  });
});

describe("filterSessions", () => {
  it("applies scope + text and sorts newest first (missing timestamps last)", () => {
    expect(filterSessions(all, "all", "", "alice").map((x) => x.session_id)).toEqual([
      issue.session_id,
      mine.session_id,
      bobSmiths.session_id,
      bobs.session_id,
      pr.session_id,
      foreign.session_id,
    ]);
    expect(filterSessions(all, "portal", "bob", "alice")).toEqual([bobSmiths, bobs]);
    expect(filterSessions(all, "mine", "bob", "alice")).toEqual([]);
  });
  it("sortByActivity does not mutate its input", () => {
    const input = [pr, issue];
    sortByActivity(input);
    expect(input).toEqual([pr, issue]);
  });
});

describe("pageWindow", () => {
  const rows = Array.from({ length: 120 }, (_, i) => i);
  it("shows the first window and reports the rest", () => {
    const w = pageWindow(rows, SESSIONS_PAGE_SIZE);
    expect(w.rows).toHaveLength(50);
    expect(w.rows[0]).toBe(0);
    expect(w.remaining).toBe(70);
    expect(w.nextStep).toBe(50);
  });
  it("the last step is the remainder, and a fully shown list is returned as-is", () => {
    expect(pageWindow(rows, 100)).toMatchObject({ remaining: 20, nextStep: 20 });
    const all = pageWindow(rows, 500);
    expect(all.rows).toBe(rows);
    expect(all).toMatchObject({ remaining: 0, nextStep: 0 });
    expect(pageWindow([], 50)).toEqual({ rows: [], remaining: 0, nextStep: 0 });
  });
});

describe("isOwnedBy", () => {
  it("matches a portal id on its exact owner only", () => {
    expect(isOwnedBy("portal-bob-20260102-deadbeef", "Bob")).toBe(true);
    expect(isOwnedBy("portal-bob-smith-20260102-0badf00d", "bob")).toBe(false);
    expect(isOwnedBy("portal-bob-20260102-deadbeef", "bob-smith")).toBe(false);
  });
  it("matches other surfaces' ids that carry the login as a whole token", () => {
    expect(isOwnedBy("slack-bob-C0123-1700000000", "bob")).toBe(true);
    expect(isOwnedBy("cli_bob_2026", "bob")).toBe(true);
    expect(isOwnedBy("bob", "bob")).toBe(true);
    expect(isOwnedBy("BOB-desk", "bob")).toBe(true);
    expect(isOwnedBy("slack-bob-smith-C0123", "bob-smith")).toBe(true);
  });
  it("does not match a login embedded in a longer token", () => {
    expect(isOwnedBy("slack-bobby-C0123", "bob")).toBe(false);
    expect(isOwnedBy("slack-abob-C0123", "bob")).toBe(false);
    expect(isOwnedBy("slack-bob2-C0123", "bob")).toBe(false);
  });
  it("never claims gh- item sessions, even for the repo owner", () => {
    expect(isOwnedBy("gh-bob-repo-62d5be96-pr-1", "bob")).toBe(false);
  });
  it("mine scope uses it", () => {
    const slack = s("slack-alice-C0123-1700000000");
    expect([...all, slack].filter((x) => inScope(x, "mine", "alice"))).toEqual([mine, slack]);
  });
});

describe("itemTargets", () => {
  it("a URL yields exactly the item it names", () => {
    expect(itemTargets(" https://github.com/strands-agents/sdk-python/pull/42 ").map((t) => t.sessionId)).toEqual([
      "gh-strands-agents-sdk-python-b00b0499-pr-42",
    ]);
    expect(itemTargets("github.com/o/r/issues/7").map((t) => t.sessionId)).toEqual(["gh-o-r-96458419-issue-7"]);
    expect(itemTargets("https://github.com/o/r/discussions/3#discussioncomment-1").map((t) => t.kind)).toEqual(["disc"]);
  });
  it("a differently cased URL finds the same session in any scope", () => {
    expect(itemTargets("https://github.com/Strands-Agents/SDK-Python/issues/42").map((t) => t.sessionId)).toEqual([
      issue.session_id,
    ]);
    expect(filterSessions(all, "mine", "https://github.com/Strands-Agents/SDK-Python/issues/42", "alice")).toEqual([issue]);
  });
  it("a bare owner/repo#n offers the PR and the issue", () => {
    expect(itemTargets("o/r#5").map((t) => t.sessionId)).toEqual(["gh-o-r-96458419-pr-5", "gh-o-r-96458419-issue-5"]);
  });
  it("plain text and logins are not targets", () => {
    expect(itemTargets("")).toEqual([]);
    expect(itemTargets("release plan")).toEqual([]);
    expect(itemTargets("@bob")).toEqual([]);
    expect(itemTargets("https://github.com/o/r")).toEqual([]);
  });
});

describe("splitItemDraft", () => {
  it("pulls the URL out and keeps the rest as the message", () => {
    expect(splitItemDraft("what do you think about https://github.com/o/r/pull/9 ? looks risky")).toEqual({
      item: { repo: "o/r", kind: "pr", number: 9, sessionId: "gh-o-r-96458419-pr-9" },
      message: "what do you think about ? looks risky",
    });
  });
  it("a URL alone has an empty message", () => {
    expect(splitItemDraft("https://github.com/o/r/issues/2")?.message).toBe("");
  });
  it("ignores prose refs and non-item URLs", () => {
    expect(splitItemDraft("see o/r#9 please")).toBeNull();
    expect(splitItemDraft("see https://github.com/o/r/blob/main/x.ts")).toBeNull();
    expect(splitItemDraft("nothing here")).toBeNull();
  });
});

describe("viewingFor", () => {
  it("maps the session kind to the invocation-context type with the canonical URL", () => {
    expect(viewingFor({ repo: "o/r", kind: "pr", number: 1, sessionId: "gh-o-r-96458419-pr-1" })).toEqual({
      type: "pr",
      repo: "o/r",
      number: 1,
      url: "https://github.com/o/r/pull/1",
    });
    expect(viewingFor({ repo: "o/r", kind: "disc", number: 2, sessionId: "gh-o-r-96458419-disc-2" }).type).toBe("discussion");
    expect(viewingFor({ repo: "o/r", kind: "issue", number: 3, sessionId: "gh-o-r-96458419-issue-3" }).type).toBe("issue");
  });
});

describe("filterSessions with an item target", () => {
  it("shows the named item's session whatever the scope — Mine is the default and would hide it", () => {
    const url = "https://github.com/o/r/pull/7";
    expect(filterSessions(all, "mine", url, "alice")).toEqual([pr]);
    expect(filterSessions(all, "portal", "o/r#7", "alice")).toEqual([pr]);
    expect(filterSessions(all, "all", "github.com/o/r/issues/7", "alice")).toEqual([]);
  });
  it("still text-filters when the query is not an item", () => {
    expect(filterSessions(all, "all", "o/r", "alice")).toEqual([]);
  });
  it("never jumps to a repository whose name only reads the same", () => {
    const ours = s("gh-acme-labs-tool-7d6b2707-issue-12");
    const theirs = s("gh-acme-labs-tool-65ef6050-issue-12");
    const one = s("gh-acme-labs-tool-7d6b2707-issue-1");
    const rows = [ours, theirs, one];
    expect(filterSessions(rows, "mine", "https://github.com/acme/labs-tool/issues/12", "alice")).toEqual([ours]);
    expect(filterSessions(rows, "mine", "https://github.com/acme-labs/tool/issues/12", "alice")).toEqual([theirs]);
    expect(filterSessions(rows, "mine", "https://github.com/acme/labs-tool/issues/1", "alice")).toEqual([one]);
    expect(rows.some((r) => isOwnedBy(r.session_id, "acme"))).toBe(false);
  });
});
