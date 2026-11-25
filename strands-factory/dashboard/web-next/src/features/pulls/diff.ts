/**
 * Unified-diff parser — turns GitHub's per-file `patch` (REST `/pulls/:n/files`) into hunks of
 * typed lines a renderer can lay out as a grid. Pure: no DOM, no React, no I/O.
 *
 * GitHub's `patch` is a unified diff WITHOUT the `---`/`+++` file header: it starts directly at the
 * first `@@` hunk header. We tolerate a leading file header anyway (skipped). `\ No newline at end
 * of file` markers are dropped (they're not a line in either side). A line that doesn't begin with
 * ` `, `+`, `-`, `@@` or `\` (shouldn't happen) is treated as context so the numbering stays sane.
 */

export type DiffLineType = "add" | "del" | "ctx" | "hunk";

export interface DiffLine {
  type: DiffLineType;
  /** Line number in the old file (ctx + del). */
  oldNo?: number;
  /** Line number in the new file (ctx + add). */
  newNo?: number;
  /** Content without the leading marker (hunk headers keep their full `@@ … @@` text). */
  text: string;
}

export interface DiffHunk {
  oldStart: number;
  oldLines: number;
  newStart: number;
  newLines: number;
  /** Trailing section heading after the second `@@` (e.g. a function name), "" if none. */
  heading: string;
  lines: DiffLine[];
}

const HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$/;

/** Parse a hunk header line, or null when it isn't one. */
export function parseHunkHeader(line: string): Omit<DiffHunk, "lines"> | null {
  const m = HUNK_RE.exec(line);
  if (!m) return null;
  return {
    oldStart: Number(m[1]),
    oldLines: m[2] === undefined ? 1 : Number(m[2]),
    newStart: Number(m[3]),
    newLines: m[4] === undefined ? 1 : Number(m[4]),
    heading: m[5] ?? "",
  };
}

/** Parse a GitHub `patch` string into hunks. `undefined`/empty → `[]` (binary / too large). */
export function parsePatch(patch: string | null | undefined): DiffHunk[] {
  if (!patch) return [];
  const hunks: DiffHunk[] = [];
  let cur: DiffHunk | null = null;
  let oldNo = 0;
  let newNo = 0;
  // Split on \n; a trailing "\n" would otherwise yield a phantom empty last line.
  const raw = patch.replace(/\r\n/g, "\n").split("\n");
  if (raw.length && raw[raw.length - 1] === "") raw.pop();

  for (const line of raw) {
    if (line.startsWith("@@")) {
      const h = parseHunkHeader(line);
      if (!h) continue; // malformed header — skip rather than misnumber
      cur = { ...h, lines: [{ type: "hunk", text: line }] };
      hunks.push(cur);
      oldNo = h.oldStart;
      newNo = h.newStart;
      continue;
    }
    if (!cur) continue; // file header (`--- a/x`, `+++ b/x`, `diff --git`) or junk before the first hunk
    if (line.startsWith("\\")) continue; // "\ No newline at end of file"
    const marker = line[0];
    const text = line.slice(1);
    if (marker === "+") {
      cur.lines.push({ type: "add", newNo: newNo++, text });
    } else if (marker === "-") {
      cur.lines.push({ type: "del", oldNo: oldNo++, text });
    } else {
      // " " context — or a line with no marker at all (defensive: treat as context).
      cur.lines.push({ type: "ctx", oldNo: oldNo++, newNo: newNo++, text: marker === " " ? text : line });
    }
  }
  return hunks;
}

/** Every line across all hunks, in order (what a renderer iterates). */
export function flattenHunks(hunks: DiffHunk[]): DiffLine[] {
  const out: DiffLine[] = [];
  for (const h of hunks) out.push(...h.lines);
  return out;
}

/** Split `dir/sub/file.ts` → `{ dir: "dir/sub/", base: "file.ts" }` so the filename can be bolded. */
export function splitPath(path: string): { dir: string; base: string } {
  const i = path.lastIndexOf("/");
  if (i < 0) return { dir: "", base: path };
  return { dir: path.slice(0, i + 1), base: path.slice(i + 1) };
}
