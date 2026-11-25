/**
 * Syntax colors for the diff — PURE (no React). Grammars come from lowlight's `common` set, the same
 * modules rehype-highlight already bundles for Markdown code blocks, so this costs no extra bytes;
 * the `hljs-*` palette in styles.css colors both.
 *
 * Each side of a patch is highlighted as ONE document (old = ctx + del, new = ctx + add, hunks
 * joined) so block comments and template strings keep their color across lines, then the hast tree
 * is split back into per-line token runs. Bounded: a side over MAX_SIDE_CHARS or any line over
 * MAX_LINE_CHARS (minified bundles, lockfiles) is left plain — highlighting is decoration, never a
 * reason for a slow Files tab.
 */
import { common, createLowlight } from "lowlight";
import type { DiffLine } from "./diff";

const lowlight = createLowlight(common);

export const MAX_SIDE_CHARS = 200_000;
export const MAX_LINE_CHARS = 2_000;

/** One run of text with its innermost highlight.js scope classes ("" for plain). */
export interface Token {
  cls: string;
  text: string;
}

const BY_EXT: Record<string, string> = {
  ts: "typescript", tsx: "typescript", mts: "typescript", cts: "typescript",
  js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  py: "python", pyi: "python",
  json: "json", jsonc: "json", json5: "json",
  yml: "yaml", yaml: "yaml",
  md: "markdown", mdx: "markdown",
  css: "css", scss: "scss", less: "less",
  html: "xml", htm: "xml", xml: "xml", svg: "xml", vue: "xml", svelte: "xml", xsl: "xml", plist: "xml",
  sh: "bash", bash: "bash", zsh: "bash",
  go: "go", rs: "rust", java: "java", kt: "kotlin", kts: "kotlin", swift: "swift", rb: "ruby", php: "php",
  c: "c", h: "c", cc: "cpp", cpp: "cpp", cxx: "cpp", hpp: "cpp", hh: "cpp", m: "objectivec", cs: "csharp",
  sql: "sql", toml: "ini", ini: "ini", cfg: "ini", graphql: "graphql", gql: "graphql",
  lua: "lua", pl: "perl", r: "r", diff: "diff", patch: "diff", mk: "makefile",
};

const BY_NAME: Record<string, string> = {
  makefile: "makefile", gnumakefile: "makefile",
  ".bashrc": "bash", ".zshrc": "bash", ".bash_profile": "bash", ".profile": "bash",
  ".gitignore": "ini", ".editorconfig": "ini", ".npmrc": "ini",
};

/** The lowlight grammar for a file path, or null when we don't color that kind of file. */
export function languageForPath(path: string): string | null {
  const base = path.split("/").pop()?.toLowerCase() ?? "";
  const byName = BY_NAME[base];
  if (byName) return byName;
  const dot = base.lastIndexOf(".");
  if (dot < 0) return null;
  const lang = BY_EXT[base.slice(dot + 1)];
  return lang && lowlight.registered(lang) ? lang : null;
}

interface HastNode {
  type: string;
  value?: string;
  properties?: { className?: unknown };
  children?: HastNode[];
}

/**
 * Highlight `code` and return one token run per line (`code.split("\n").length` lines). Null when
 * the language is unknown, the input is over budget, or the grammar throws.
 */
export function highlightLines(code: string, language: string): Token[][] | null {
  if (!lowlight.registered(language) || code.length > MAX_SIDE_CHARS) return null;
  let root: HastNode;
  try {
    root = lowlight.highlight(language, code) as HastNode;
  } catch {
    return null;
  }
  const lines: Token[][] = [[]];
  const walk = (nodes: HastNode[], cls: string) => {
    for (const n of nodes) {
      if (n.type === "text" && typeof n.value === "string") {
        n.value.split("\n").forEach((part, i) => {
          if (i > 0) lines.push([]);
          if (part) lines[lines.length - 1].push({ cls, text: part });
        });
      } else if (n.children) {
        const names = n.properties?.className;
        // Innermost scope wins — that is what nested <span>s would cascade to.
        walk(n.children, Array.isArray(names) && names.length ? names.join(" ") : cls);
      }
    }
  };
  walk(root.children ?? [], "");
  return lines;
}

/**
 * Token runs for every flattened diff line (hunk headers and anything unhighlightable are null), or
 * null when the whole file stays plain. Old-side tokens go to `del` lines, new-side to `ctx` + `add`.
 */
export function tokenizeDiff(lines: DiffLine[], path: string | undefined): (Token[] | null)[] | null {
  const lang = path ? languageForPath(path) : null;
  if (!lang || lines.some((l) => l.type !== "hunk" && l.text.length > MAX_LINE_CHARS)) return null;
  const oldIdx: number[] = [];
  const newIdx: number[] = [];
  lines.forEach((l, i) => {
    if (l.type === "ctx" || l.type === "del") oldIdx.push(i);
    if (l.type === "ctx" || l.type === "add") newIdx.push(i);
  });
  const side = (idx: number[]) => (idx.length ? highlightLines(idx.map((i) => lines[i].text).join("\n"), lang) : null);
  const oldT = side(oldIdx);
  const newT = side(newIdx);
  if (!oldT && !newT) return null;
  const out: (Token[] | null)[] = lines.map(() => null);
  if (newT?.length === newIdx.length) newIdx.forEach((i, k) => (out[i] = newT[k]));
  if (oldT?.length === oldIdx.length) oldIdx.forEach((i, k) => lines[i].type === "del" && (out[i] = oldT[k]));
  return out;
}
