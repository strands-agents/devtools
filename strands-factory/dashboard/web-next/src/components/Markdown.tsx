/**
 * Markdown — the agent's text rendered as Markdown (GFM: tables, strikethrough, task lists).
 *
 * Safety model: transcript text is UNTRUSTED (the agent echoes arbitrary tool output). react-markdown
 * renders to React elements — never `innerHTML` — with raw HTML in the source rendered as escaped
 * text (no `rehype-raw`), and its default URL transform drops `javascript:`/`data:` link targets. So
 * no separate sanitizer pass is needed, unlike the SPA's marked + DOMPurify pair. Links open in a
 * new tab with `rel="noopener noreferrer"`; images are offered as links, never fetched — an `<img>`
 * would make the maintainer's browser call whatever host the text names (the SPA's allowlist has no
 * `img` for the same reason).
 *
 * `remark-breaks` turns a single newline into a line break, matching the SPA (`breaks: true`) and
 * what the agent means when it lays out a reply line by line.
 *
 * `<details>`/`<summary>` are the ONE piece of HTML honored — as dropdowns, like GitHub — via
 * `remarkDetails`, which rewrites only those tag lines into elements and leaves all other raw HTML
 * as text, so the no-`rehype-raw` safety model above still holds.
 *
 * Fenced code blocks with a language tag are syntax-highlighted by `rehype-highlight` (lowlight —
 * highlight.js grammars, `common` set). It rewrites the `<code>` element's children into `span`s with
 * `hljs-*` classes on the hast tree — still React elements, no HTML string — and leaves unlabeled or
 * unknown-language blocks as plain text (`detect: false`: guessing a language for tool output and
 * logs colors them wrong more often than right). Colors live in Markdown.module.css.
 */
import { createContext, useContext, type ReactNode } from "react";
import { Anchor, Typography } from "@mantine/core";
import ReactMarkdown, { type Options as MarkdownOptions } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import remarkDetails from "../lib/remarkDetails";
import classes from "./Markdown.module.css";

export const PLUGINS = [remarkGfm, remarkBreaks, remarkDetails];
export const REHYPE_PLUGINS: NonNullable<MarkdownOptions["rehypePlugins"]> = [[rehypeHighlight, { detect: false }]];

/** True inside a rendered link — an image there is the link's label, not a second `<a>`. */
const InLink = createContext(false);

function Link({ href, children }: { href?: string; children?: ReactNode }) {
  // No href: the URL transform dropped an unsafe target — show the label as text, not a link to "".
  // A `#fragment` (a footnote's ref) is a ROUTE under the portal's HashRouter — following it would
  // leave the current view — so it is text too.
  if (!href || href.startsWith("#")) return <>{children}</>;
  return (
    <InLink.Provider value={true}>
      <Anchor href={href} size="sm" target="_blank" rel="noopener noreferrer">
        {children}
      </Anchor>
    </InLink.Provider>
  );
}

function Image({ src, alt }: { src?: string; alt?: string }) {
  const label = alt || src;
  return useContext(InLink) ? <>{label}</> : <Link href={src}>{label}</Link>;
}

const COMPONENTS = { a: Link, img: Image };

export function Markdown({ text }: { text: string }) {
  return (
    <Typography className={classes.body}>
      <ReactMarkdown remarkPlugins={PLUGINS} rehypePlugins={REHYPE_PLUGINS} components={COMPONENTS}>
        {text}
      </ReactMarkdown>
    </Typography>
  );
}
