import { describe, expect, it } from "vitest";
import { flattenHunks, parseHunkHeader, parsePatch, splitPath } from "./diff";

const SIMPLE = [
  "@@ -1,3 +1,4 @@",
  " a",
  "-b",
  "+B",
  "+b2",
  " c",
].join("\n");

describe("parseHunkHeader", () => {
  it("parses ranges with counts and a heading", () => {
    expect(parseHunkHeader("@@ -10,7 +12,9 @@ def foo():")).toEqual({
      oldStart: 10,
      oldLines: 7,
      newStart: 12,
      newLines: 9,
      heading: "def foo():",
    });
  });
  it("defaults a missing count to 1 (single-line files)", () => {
    expect(parseHunkHeader("@@ -1 +1 @@")).toEqual({ oldStart: 1, oldLines: 1, newStart: 1, newLines: 1, heading: "" });
  });
  it("returns null for non-headers", () => {
    expect(parseHunkHeader(" context")).toBeNull();
    expect(parseHunkHeader("@@ garbage @@")).toBeNull();
  });
});

describe("parsePatch", () => {
  it("returns [] for files without a patch (binary / too large)", () => {
    expect(parsePatch(undefined)).toEqual([]);
    expect(parsePatch(null)).toEqual([]);
    expect(parsePatch("")).toEqual([]);
  });

  it("numbers ctx/add/del lines on the right sides", () => {
    const [h] = parsePatch(SIMPLE);
    expect(h.oldStart).toBe(1);
    expect(h.newStart).toBe(1);
    expect(h.lines).toEqual([
      { type: "hunk", text: "@@ -1,3 +1,4 @@" },
      { type: "ctx", oldNo: 1, newNo: 1, text: "a" },
      { type: "del", oldNo: 2, text: "b" },
      { type: "add", newNo: 2, text: "B" },
      { type: "add", newNo: 3, text: "b2" },
      { type: "ctx", oldNo: 3, newNo: 4, text: "c" },
    ]);
  });

  it("handles multiple hunks, restarting numbering at each header", () => {
    const patch = ["@@ -1,2 +1,2 @@", " x", "-y", "+Y", "@@ -40,3 +40,3 @@ fn", " p", "-q", "+Q", " r"].join("\n");
    const hunks = parsePatch(patch);
    expect(hunks).toHaveLength(2);
    expect(hunks[1].heading).toBe("fn");
    expect(hunks[1].lines[1]).toEqual({ type: "ctx", oldNo: 40, newNo: 40, text: "p" });
    expect(hunks[1].lines[2]).toEqual({ type: "del", oldNo: 41, text: "q" });
    expect(hunks[1].lines[3]).toEqual({ type: "add", newNo: 41, text: "Q" });
    expect(hunks[1].lines[4]).toEqual({ type: "ctx", oldNo: 42, newNo: 42, text: "r" });
  });

  it("drops '\\ No newline at end of file' markers without disturbing numbering", () => {
    const patch = ["@@ -1,2 +1,2 @@", " a", "-b", "\\ No newline at end of file", "+b", "\\ No newline at end of file"].join("\n");
    const lines = flattenHunks(parsePatch(patch));
    expect(lines.map((l) => l.type)).toEqual(["hunk", "ctx", "del", "add"]);
    expect(lines[3]).toEqual({ type: "add", newNo: 2, text: "b" });
  });

  it("ignores a leading file header and tolerates CRLF + trailing newline", () => {
    const patch = "diff --git a/f b/f\r\n--- a/f\r\n+++ b/f\r\n@@ -1 +1 @@\r\n-old\r\n+new\r\n";
    const lines = flattenHunks(parsePatch(patch));
    expect(lines).toEqual([
      { type: "hunk", text: "@@ -1 +1 @@" },
      { type: "del", oldNo: 1, text: "old" },
      { type: "add", newNo: 1, text: "new" },
    ]);
  });

  it("keeps leading whitespace and empty content lines intact", () => {
    const patch = ["@@ -1,3 +1,3 @@", "   indented", "-", "+\tnew tab", " "].join("\n");
    const lines = flattenHunks(parsePatch(patch));
    expect(lines[1].text).toBe("  indented");
    expect(lines[2]).toEqual({ type: "del", oldNo: 2, text: "" });
    expect(lines[3].text).toBe("\tnew tab");
    expect(lines[4]).toEqual({ type: "ctx", oldNo: 3, newNo: 3, text: "" });
  });

  it("treats a new file (old range 0,0) as all additions from line 1", () => {
    const patch = ["@@ -0,0 +1,2 @@", "+one", "+two"].join("\n");
    const lines = flattenHunks(parsePatch(patch));
    expect(lines.slice(1)).toEqual([
      { type: "add", newNo: 1, text: "one" },
      { type: "add", newNo: 2, text: "two" },
    ]);
  });

  it("treats a line with no marker defensively as context", () => {
    const patch = ["@@ -1,1 +1,1 @@", "weird"].join("\n");
    const lines = flattenHunks(parsePatch(patch));
    expect(lines[1]).toEqual({ type: "ctx", oldNo: 1, newNo: 1, text: "weird" });
  });
});

describe("splitPath", () => {
  it("splits directory and basename", () => {
    expect(splitPath("src/features/pulls/diff.ts")).toEqual({ dir: "src/features/pulls/", base: "diff.ts" });
    expect(splitPath("README.md")).toEqual({ dir: "", base: "README.md" });
  });
});
