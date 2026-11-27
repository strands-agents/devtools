import { describe, expect, it } from "vitest";
import {
  isPortalSession,
  itemToSessionId,
  newPortalSessionId,
  parseItemInput,
  parseSessionId,
  portalSessionOwner,
  portalSessionPrefix,
  repoDigest,
  sessionLabel,
} from "./sessionId";

describe("portal session ids", () => {
  it("mints portal-<login>-<yyyymmdd>-<rand8>", () => {
    const id = newPortalSessionId("example-maintainer", new Date("2026-10-21T21:00:00Z"));
    expect(id).toMatch(/^portal-example-maintainer-20261021-[0-9a-f]{8}$/);
    expect(isPortalSession(id)).toBe(true);
    expect(portalSessionOwner(id)).toBe("example-maintainer");
    expect(id.startsWith(portalSessionPrefix("example-maintainer"))).toBe(true);
  });

  it("falls back to anon without a login and sanitizes odd characters", () => {
    expect(newPortalSessionId(null)).toMatch(/^portal-anon-\d{8}-[0-9a-f]{8}$/);
    expect(portalSessionPrefix("we!rd/user")).toBe("portal-werduser-");
  });

  it("does not mistake gh-… ids for portal sessions", () => {
    const gh = itemToSessionId("strands-agents/sdk-python", "pr", 12);
    expect(isPortalSession(gh)).toBe(false);
    expect(portalSessionOwner(gh)).toBeNull();
    expect(portalSessionOwner("portal-20260101-abcdef12")).toBeNull(); // legacy login-less id
  });

  it("still parses item inputs", () => {
    expect(parseItemInput("https://github.com/o/r/issues/5")?.sessionId).toBe("gh-o-r-96458419-issue-5");
  });
});

describe("GitHub item session ids", () => {
  it("match the harness format gh-<owner>-<repo>-<h8>-<kind>-<number>", () => {
    // Values pinned against strandly.core.session_ids.canonical_session_id.
    expect(itemToSessionId("strands-agents/sdk-python", "issue", 42)).toBe("gh-strands-agents-sdk-python-b00b0499-issue-42");
    expect(repoDigest("Strands-Agents/SDK-Python")).toBe("b00b0499");
    expect(itemToSessionId("acme/labs-tool", "issue", 42)).toBe("gh-acme-labs-tool-7d6b2707-issue-42");
    expect(itemToSessionId("acme-labs/tool", "issue", 42)).toBe("gh-acme-labs-tool-65ef6050-issue-42");
  });

  it.each([
    ["acme/labs-tool", "acme-labs/tool"],
    ["strands-agents/sdk-python", "strands-agents-sdk/python"],
  ])("keeps %s and %s apart though their names read the same", (ours, theirs) => {
    expect(itemToSessionId(ours, "pr", 7)).not.toBe(itemToSessionId(theirs, "pr", 7));
    expect(parseItemInput(`https://github.com/${ours}/pull/7`)?.sessionId).toBe(itemToSessionId(ours, "pr", 7));
    expect(parseItemInput(`https://github.com/${theirs}/pull/7`)?.sessionId).toBe(itemToSessionId(theirs, "pr", 7));
  });

  it.each(["Acme/Labs-Tool", "ACME/labs-tool", "/acme/labs-tool/"])("gives %s the one lowercase id", (cased) => {
    expect(itemToSessionId(cased, "issue", 42)).toBe("gh-acme-labs-tool-7d6b2707-issue-42");
    expect(parseItemInput(`https://github.com/${cased.replace(/^\/|\/$/g, "")}/issues/42`)?.sessionId).toBe(
      "gh-acme-labs-tool-7d6b2707-issue-42",
    );
    expect(parseItemInput(`${cased.replace(/^\/|\/$/g, "")}#42`, "issue")?.sessionId).toBe(
      "gh-acme-labs-tool-7d6b2707-issue-42",
    );
  });

  it("keeps issue-1 and issue-12 distinct", () => {
    expect(itemToSessionId("o/r", "issue", 1)).not.toBe(itemToSessionId("o/r", "issue", 12));
    expect(parseSessionId(itemToSessionId("o/r", "issue", 12))).toEqual({ repoSlug: "o-r", digest: "96458419", kind: "issue", number: 12 });
  });

  it("labels new and retired ids by their readable slug", () => {
    expect(sessionLabel("gh-acme-labs-tool-7d6b2707-issue-42")).toBe("acme-labs-tool#42");
    expect(parseSessionId("gh-acme-labs-tool-issue-42")).toEqual({ repoSlug: "acme-labs-tool", digest: null, kind: "issue", number: 42 });
    expect(parseSessionId("gh-a-deadbeef-x-12345678-pr-3")).toEqual({ repoSlug: "a-deadbeef-x", digest: "12345678", kind: "pr", number: 3 });
  });
});
