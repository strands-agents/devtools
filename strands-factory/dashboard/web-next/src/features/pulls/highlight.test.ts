import { describe, expect, it } from "vitest";
import { flattenHunks, parsePatch } from "./diff";
import { highlightLines, languageForPath, MAX_LINE_CHARS, MAX_SIDE_CHARS, tokenizeDiff, type Token } from "./highlight";

const text = (toks: Token[] | null | undefined) => (toks ?? []).map((t) => t.text).join("");

describe("languageForPath", () => {
  it("maps common extensions and well-known names", () => {
    expect(languageForPath("src/features/pulls/DiffView.tsx")).toBe("typescript");
    expect(languageForPath("handler.py")).toBe("python");
    expect(languageForPath("a/b/Makefile")).toBe("makefile");
    expect(languageForPath(".github/workflows/build.yml")).toBe("yaml");
    expect(languageForPath("index.html")).toBe("xml");
    expect(languageForPath("Cargo.toml")).toBe("ini");
  });
  it("is null for unknown or extension-less files", () => {
    expect(languageForPath("LICENSE")).toBeNull();
    expect(languageForPath("image.png")).toBeNull();
    expect(languageForPath("weird.mermaid")).toBeNull();
    expect(languageForPath("")).toBeNull();
  });
});

describe("highlightLines", () => {
  it("returns one token run per input line and round-trips the text", () => {
    const code = "const n: number = 1; // one\n\nfunction f() {\n  return `x${n}`;\n}";
    const lines = highlightLines(code, "typescript")!;
    expect(lines.map(text)).toEqual(code.split("\n"));
    expect(lines[0].some((t) => t.cls === "hljs-keyword" && t.text === "const")).toBe(true);
    expect(lines[0].some((t) => t.cls === "hljs-comment")).toBe(true);
    expect(lines[1]).toEqual([]);
  });

  it("keeps a block comment's scope across lines (highlighted as one document)", () => {
    const lines = highlightLines("/* a\n   b\n   c */\nlet x = 1;", "javascript")!;
    expect(lines[1].every((t) => t.cls === "hljs-comment")).toBe(true);
    expect(lines[3].some((t) => t.cls === "hljs-keyword")).toBe(true);
  });

  it("uses the innermost scope for nested spans", () => {
    const lines = highlightLines("def f(x):\n    return x", "python")!;
    const fn = lines[0].find((t) => t.text === "f");
    expect(fn?.cls).toBe("hljs-title function_");
  });

  it("is null for unknown languages and over-budget input", () => {
    expect(highlightLines("x", "mermaid")).toBeNull();
    expect(highlightLines("x".repeat(MAX_SIDE_CHARS + 1), "typescript")).toBeNull();
  });
});

const PATCH = [
  "@@ -1,4 +1,4 @@",
  " import os",
  "-x = 'old'  # was",
  "+x = 'new'  # now",
  " ",
  " print(x)",
].join("\n");

describe("tokenizeDiff", () => {
  it("aligns tokens with the flattened lines; hunk headers stay null; text round-trips", () => {
    const lines = flattenHunks(parsePatch(PATCH));
    const toks = tokenizeDiff(lines, "app.py")!;
    expect(toks).toHaveLength(lines.length);
    expect(toks[0]).toBeNull();
    lines.forEach((l, i) => {
      if (l.type !== "hunk") expect(text(toks[i])).toBe(l.text);
    });
    const del = toks[2]!;
    const add = toks[3]!;
    expect(del.some((t) => t.cls === "hljs-string" && t.text === "'old'")).toBe(true);
    expect(add.some((t) => t.cls === "hljs-string" && t.text === "'new'")).toBe(true);
    expect(del.some((t) => t.cls === "hljs-comment")).toBe(true);
  });

  it("is null when the file kind is unknown or a line is too long", () => {
    const lines = flattenHunks(parsePatch(PATCH));
    expect(tokenizeDiff(lines, "notes.txt")).toBeNull();
    expect(tokenizeDiff(lines, undefined)).toBeNull();
    const long = flattenHunks(parsePatch(`@@ -1 +1 @@\n-a\n+${"b".repeat(MAX_LINE_CHARS + 1)}`));
    expect(tokenizeDiff(long, "a.js")).toBeNull();
  });

  it("handles a patch with only additions (no old side)", () => {
    const lines = flattenHunks(parsePatch("@@ -0,0 +1,2 @@\n+const a = 1;\n+const b = 2;"));
    const toks = tokenizeDiff(lines, "a.ts")!;
    expect(toks[1]!.some((t) => t.cls === "hljs-keyword")).toBe(true);
    expect(text(toks[2])).toBe("const b = 2;");
  });
});
