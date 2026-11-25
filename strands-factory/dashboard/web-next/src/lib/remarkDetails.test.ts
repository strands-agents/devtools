import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import { describe, expect, it } from "vitest";
import { PLUGINS } from "../components/Markdown";

/** The chat's exact plugin chain, minus Mantine (which needs a provider and adds nothing here). */
const render = (md: string) => renderToStaticMarkup(createElement(ReactMarkdown, { remarkPlugins: PLUGINS }, md));

describe("remarkDetails", () => {
  it("turns a details/summary block into a dropdown with Markdown content", () => {
    const html = render(["Intro", "", "<details>", "<summary>Logs</summary>", "", "- item **one**", "", "</details>", "", "Outro"].join("\n"));
    expect(html).toBe(
      "<p>Intro</p>\n<details><summary>Logs</summary><ul>\n<li>item <strong>one</strong></li>\n</ul></details>\n<p>Outro</p>",
    );
  });

  it("accepts the one-line opener and the open attribute", () => {
    expect(render("<details open><summary>Shown</summary>\n\ntext\n\n</details>")).toBe(
      "<details open=\"\"><summary>Shown</summary><p>text</p></details>",
    );
    expect(render('<details open="open">\n\ntext\n\n</details>')).toContain('<details open="">');
  });

  it("nests, and works inside a list item", () => {
    const html = render(["<details>", "<summary>Outer</summary>", "", "<details>", "<summary>Inner</summary>", "", "deep", "", "</details>", "", "</details>"].join("\n"));
    expect(html).toBe("<details><summary>Outer</summary><details><summary>Inner</summary><p>deep</p></details></details>");
    expect(render("- a\n\n  <details>\n  <summary>S</summary>\n\n  b\n\n  </details>")).toBe(
      "<ul>\n<li>\n<p>a</p>\n<details><summary>S</summary><p>b</p></details>\n</li>\n</ul>",
    );
  });

  it("closes an unclosed block at the end and ignores a stray close tag", () => {
    expect(render("<details>\n<summary>S</summary>\n\nrest")).toBe("<details><summary>S</summary><p>rest</p></details>");
    expect(render("</details>\n\nafter")).toBe("<p>after</p>");
  });

  it("keeps lines glued to the tags as literal text, like GitHub", () => {
    // No blank line after <summary>: `- item` is part of the raw block, not a list.
    expect(render("<details>\n<summary>S</summary>\n- item\n\n</details>")).toBe(
      "<details><summary>S</summary>- item</details>",
    );
    // A second summary is text; the summary's own inline Markdown is literal.
    expect(render("<details>\n<summary>**A**</summary>\n<summary>B</summary>\n\n</details>")).toBe(
      "<details><summary>**A**</summary>&lt;summary&gt;B&lt;/summary&gt;</details>",
    );
  });

  it("decodes character references in the summary, as HTML would", () => {
    expect(render("<details>\n<summary>Tests &amp; lint &#8212; &#x1F50D; &lt;ok&gt; &bogus; &#0;</summary>\n\nt\n\n</details>")).toBe(
      "<details><summary>Tests &amp; lint — 🔍 &lt;ok&gt; &amp;bogus; &amp;#0;</summary><p>t</p></details>",
    );
  });

  it("does not let an unhonored opener's close tag end the enclosing block", () => {
    const md = ["<details>", "<summary>Outer</summary>", "", '<details class="x">', "", "inner", "", "</details>", "", "still outer", "", "</details>", "", "after"];
    expect(render(md.join("\n"))).toBe(
      '<details><summary>Outer</summary>&lt;details class=&quot;x&quot;&gt;<p>inner</p>&lt;/details&gt;<p>still outer</p></details>\n<p>after</p>',
    );
  });

  it("resolves only known entity names, never inherited object keys", () => {
    expect(render("<details>\n<summary>&constructor; &toString; &amp;</summary>\n\nt\n\n</details>")).toBe(
      "<details><summary>&amp;constructor; &amp;toString; &amp;</summary><p>t</p></details>",
    );
  });

  it("leaves every other raw HTML as escaped text", () => {
    // An opener with attributes is not ours: it stays text, and so does its close tag.
    expect(render('<script>alert(1)</script>\n\n<details onclick="x()">\n\nbody\n\n</details>')).toBe(
      "&lt;script&gt;alert(1)&lt;/script&gt;\n&lt;details onclick=&quot;x()&quot;&gt;\n<p>body</p>\n&lt;/details&gt;",
    );
    expect(render("inline <details> in a paragraph")).toBe("<p>inline &lt;details&gt; in a paragraph</p>");
    expect(render("<summary>orphan</summary>")).toBe("&lt;summary&gt;orphan&lt;/summary&gt;");
    expect(render("<details>\n\n<img src=x onerror=alert(1)>\n\n</details>")).toBe(
      "<details>&lt;img src=x onerror=alert(1)&gt;</details>",
    );
  });
});
