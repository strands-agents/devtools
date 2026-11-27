/**
 * remarkDetails — honor `<details>` / `<summary>` in agent Markdown the way GitHub does, so a
 * reply can fold long output behind a dropdown.
 *
 * Markdown leaves raw HTML alone, so the agent's `<details>` lines reach the renderer as `html`
 * nodes and, with no HTML pass wired (see Markdown.tsx), print as literal text. This plugin
 * recognizes ONLY those two tags — a whole line that is exactly `<details>`, `<details open>`,
 * `<summary>…</summary>` or `</details>` — and rebuilds the blocks between them into real
 * `details`/`summary` elements. Every other line of raw HTML is left exactly as it was (still
 * rendered as escaped text), so this does not widen the safety model: no general HTML parser, no
 * sanitizer to keep right, nothing new for a hostile transcript to reach.
 *
 * Runs on the mdast tree, so the content of a block is regular Markdown (lists, code, tables,
 * nested details). Unclosed blocks close at the end of their container, stray `</details>` are
 * dropped, and a second `<summary>` in one block is text — the way a browser would read the same
 * HTML. As on GitHub, the summary's text is literal (no inline Markdown, but `&amp;`-style character
 * references are decoded), and content must be separated from the tags by a blank line to be
 * treated as Markdown.
 */
import type { Html, Parent, Root, RootContent } from "mdast";

/** `<details>` / `<details open>`, optionally followed on the same line by its `<summary>`. */
const OPEN = /^<details(?:\s+(open)(?:="[^"]*")?)?\s*>(?:\s*<summary>(.*?)<\/summary>)?\s*$/i;
/** Any other line that starts a `<details` tag: not honored, but its `</details>` must not close a real block. */
const OPEN_LIKE = /^<details\b/i;
const SUMMARY = /^<summary>(.*?)<\/summary>\s*$/i;
const CLOSE = /^<\/details>\s*$/i;

/** Containers whose children are blocks — the only place a `<details>` line is a block of its own. */
const FLOW_PARENTS = new Set(["root", "blockquote", "list", "listItem", "footnoteDefinition"]);

export interface Details extends Parent {
  type: "details";
  children: RootContent[];
}
export interface Summary extends Parent {
  type: "summary";
  children: RootContent[];
}
declare module "mdast" {
  interface RootContentMap {
    details: Details;
    summary: Summary;
  }
}

function details(open: boolean): Details {
  return {
    type: "details",
    data: { hName: "details", hProperties: open ? { open: true } : {} },
    children: [],
  };
}

const NAMED = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: "\u00a0" } as const;

/** Decode the character references a summary may carry (`&amp;`, `&#8212;`, `&#x1F50D;`) as a browser would. */
function decode(text: string): string {
  return text.replace(/&(?:#x([0-9a-f]+)|#([0-9]+)|([a-z]+));/gi, (ref, hex?: string, dec?: string, name?: string) => {
    if (name) {
      const key = name.toLowerCase();
      // Own keys only: `&constructor;` must not resolve to Object.prototype.constructor.
      return Object.prototype.hasOwnProperty.call(NAMED, key) ? NAMED[key as keyof typeof NAMED] : ref;
    }
    const cp = hex ? parseInt(hex, 16) : parseInt(dec ?? "", 10);
    return cp > 0 && cp <= 0x10ffff ? String.fromCodePoint(cp) : ref;
  });
}

function summary(text: string): Summary {
  return { type: "summary", data: { hName: "summary" }, children: [{ type: "text", value: decode(text) }] };
}

function hasSummary(node: Details): boolean {
  return node.children.some((c) => c.type === "summary");
}

function transform(parent: Parent): void {
  const out: RootContent[] = [];
  // `null` marks an opener left as raw text, so its closer is left as raw text too.
  const stack: (Details | null)[] = [];
  const top = (): Details | undefined => {
    for (let i = stack.length - 1; i >= 0; i--) {
      const node = stack[i];
      if (node) return node;
    }
    return undefined;
  };
  const target = () => top()?.children ?? out;

  for (const child of parent.children) {
    if (child.type !== "html") {
      if (FLOW_PARENTS.has(child.type)) transform(child as Parent);
      target().push(child);
      continue;
    }
    // A raw block is one or more lines; take the tag lines out and keep the rest as raw text.
    let text: string[] = [];
    const flush = () => {
      if (text.length) {
        target().push({ type: "html", value: text.join("\n") } as Html);
        text = [];
      }
    };
    for (const line of child.value.split("\n")) {
      const t = line.trim();
      const open = OPEN.exec(t);
      if (open) {
        flush();
        const node = details(open[1] !== undefined);
        if (open[2] !== undefined) node.children.push(summary(open[2]));
        target().push(node);
        stack.push(node);
        continue;
      }
      if (OPEN_LIKE.test(t)) {
        stack.push(null);
        text.push(line);
        continue;
      }
      if (CLOSE.test(t) && stack.length && stack[stack.length - 1] !== null) {
        flush();
        stack.pop();
        continue;
      }
      if (CLOSE.test(t)) {
        // Closes an opener left as text: leave it as text too. A stray close (empty stack) is dropped.
        if (stack.length) text.push(line);
        else flush();
        stack.pop();
        continue;
      }
      const cur = stack.length ? stack[stack.length - 1] : null;
      const sum = cur ? SUMMARY.exec(t) : null;
      if (cur && sum && !hasSummary(cur)) {
        flush();
        cur.children.push(summary(sum[1]));
        continue;
      }
      text.push(line);
    }
    flush();
  }
  parent.children = out;
}

export default function remarkDetails() {
  return (tree: Root) => transform(tree);
}
