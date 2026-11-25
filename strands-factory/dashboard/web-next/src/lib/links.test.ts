import { describe, expect, it } from "vitest";
import { issueLink, itemLink, itemSessionLink, pullLink, scheduleLink, sessionLink } from "./links";

describe("links", () => {
  it("builds encoded detail routes", () => {
    expect(issueLink("strands-agents/sdk-python", 7)).toBe("/issues/strands-agents/sdk-python/7");
    expect(pullLink("o/r", 9)).toBe("/pulls/o/r/9");
    expect(sessionLink("gh-o-r-96458419-pr-9")).toBe("/strandly/gh-o-r-96458419-pr-9");
    expect(itemSessionLink("o/r", "issue", 3)).toBe("/strandly/gh-o-r-96458419-issue-3");
    expect(scheduleLink("daily-review")).toBe("/schedules/daily-review");
  });
  it("sends discussions to GitHub (no in-app page)", () => {
    expect(itemLink("o/r", "disc", 2)).toBe("https://github.com/o/r/discussions/2");
    expect(itemLink("o/r", "pr", 2)).toBe("/pulls/o/r/2");
  });
});
