import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import { describe, expect, it } from "vitest";
import { PLUGINS, REHYPE_PLUGINS } from "./Markdown";
import classes from "./Markdown.module.css";

/** The chat's exact plugin chain, minus Mantine (which needs a provider and adds nothing here). */
const render = (md: string) =>
  renderToStaticMarkup(createElement(ReactMarkdown, { remarkPlugins: PLUGINS, rehypePlugins: REHYPE_PLUGINS }, md));

describe("code highlighting", () => {
  it("colors a fenced block with a language tag", () => {
    expect(render("```ts\nconst n: number = 1; // one\n```")).toBe(
      '<pre><code class="hljs language-ts"><span class="hljs-keyword">const</span> <span class="hljs-attr">n</span>: <span class="hljs-built_in">number</span> = <span class="hljs-number">1</span>; <span class="hljs-comment">// one</span>\n</code></pre>',
    );
  });

  it("leaves an unlabeled block as plain text instead of guessing", () => {
    expect(render("```\nconst n = 1;\n$ ls -la\n```")).toBe("<pre><code>const n = 1;\n$ ls -la\n</code></pre>");
  });

  it("leaves an unknown language as plain text, without failing the render", () => {
    const html = render("```mermaid\ngraph TD; A-->B\n```");
    expect(html).toContain('class="hljs language-mermaid"');
    expect(html).toContain(">graph TD; A--&gt;B\n</code>");
    expect(html).not.toContain("hljs-");
  });

  it("does not touch inline code", () => {
    expect(render("call `const x` here")).toBe("<p>call <code>const x</code> here</p>");
  });

  it("keeps code content as text — markup in a highlighted block is escaped, not rendered", () => {
    const html = render('```html\n<script>alert("x")</script>\n```');
    expect(html).not.toContain("<script");
    expect(html).toContain('&lt;<span class="hljs-name">script</span>&gt;');
    expect(html).toContain('<span class="hljs-string">&quot;x&quot;</span>');
  });

  it("highlights code inside a details dropdown and a list item", () => {
    expect(render("<details>\n<summary>Diff</summary>\n\n```diff\n-old\n+new\n```\n\n</details>")).toBe(
      '<details><summary>Diff</summary><pre><code class="hljs language-diff"><span class="hljs-deletion">-old</span>\n<span class="hljs-addition">+new</span>\n</code></pre></details>',
    );
    expect(render("- step\n\n  ```bash\n  echo hi\n  ```")).toContain('<span class="hljs-built_in">echo</span> hi');
  });

  it("keeps the hljs-* token classes out of the CSS Module — they must never be hashed", () => {
    // The palette lives in styles.css (global, shared with the PR diff). A rule added here without
    // `:global()` would export e.g. `hljs-keyword` as a hashed class the highlighter never emits and
    // silently miss (vite.config.ts compiles CSS Modules in tests so this sees the real class map).
    expect(Object.keys(classes)).toEqual(["body"]);
  });
});
