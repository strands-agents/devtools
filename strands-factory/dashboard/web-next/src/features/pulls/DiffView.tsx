/**
 * DiffView — a GitHub `patch` rendered as a unified diff grid: old/new line numbers · marker · text.
 * Pure presentation over `parsePatch`; tints + layout live in Pull.module.css (CSS vars, dark-aware).
 * `path` picks a grammar for syntax colors (highlight.ts; the `hljs-*` palette in styles.css).
 * `wrap` toggles between a horizontally scrolling `<pre>`-like body (the only horizontal scroller in
 * the app) and `pre-wrap` for phones. `onLineClick` enables the inline-comment stretch: the parent
 * decides what to render under the active line via `afterLine`.
 */
import { memo, useMemo, type CSSProperties, type ReactNode } from "react";
import { flattenHunks, parsePatch, type DiffLine } from "./diff";
import { tokenizeDiff, type Token } from "./highlight";
import classes from "./Pull.module.css";

export interface DiffViewProps {
  patch: string;
  /** File path — picks the syntax-highlighting grammar; omit (or unknown kind) for plain text. */
  path?: string;
  wrap: boolean;
  /** Index (into the flattened lines) of the highlighted line, if any. */
  activeIndex?: number | null;
  onLineClick?: (line: DiffLine, index: number) => void;
  /** Rendered directly below the active line (an inline composer). */
  afterLine?: ReactNode;
}

function lineClass(l: DiffLine): string {
  if (l.type === "add") return classes.lineAdd;
  if (l.type === "del") return classes.lineDel;
  if (l.type === "hunk") return classes.lineHunk;
  return "";
}

function LineText({ text, tokens }: { text: string; tokens: Token[] | null | undefined }) {
  if (!tokens?.length) return <>{text || " "}</>;
  return (
    <>
      {tokens.map((t, k) =>
        t.cls ? (
          <span key={k} className={t.cls}>
            {t.text}
          </span>
        ) : (
          t.text
        ),
      )}
    </>
  );
}

export const DiffView = memo(function DiffView({ patch, path, wrap, activeIndex, onLineClick, afterLine }: DiffViewProps) {
  const lines = useMemo(() => flattenHunks(parsePatch(patch)), [patch]);
  const tokens = useMemo(() => tokenizeDiff(lines, path), [lines, path]);
  const maxNo = useMemo(() => lines.reduce((m, l) => Math.max(m, l.oldNo ?? 0, l.newNo ?? 0), 0), [lines]);
  // Width for the number gutters: digits + a little air, in `em` so it scales with the mono font.
  const lnWidth = `${Math.max(2, String(maxNo).length) + 1.2}em`;
  const clickable = !!onLineClick;
  return (
    <div className={`${classes.diff} ${wrap ? classes.wrap : classes.diffScroll}`} style={{ "--ln-w": lnWidth } as CSSProperties}>
      <div className={classes.lines} role="table" aria-label="Diff">
        {lines.map((l, i) => {
          const isHunk = l.type === "hunk";
          const active = activeIndex === i;
          const cls = [classes.line, lineClass(l), clickable && !isHunk ? classes.clickable : "", active ? classes.lineActive : ""]
            .filter(Boolean)
            .join(" ");
          return (
            <div key={i}>
              <div
                className={cls}
                role="row"
                onClick={clickable && !isHunk ? () => onLineClick?.(l, i) : undefined}
              >
                <span className={classes.ln}>{l.oldNo ?? ""}</span>
                <span className={classes.ln}>{l.newNo ?? ""}</span>
                <span className={classes.marker} aria-hidden>
                  {l.type === "add" ? "+" : l.type === "del" ? "−" : ""}
                </span>
                <span className={classes.text}>{isHunk ? l.text : <LineText text={l.text} tokens={tokens?.[i]} />}</span>
              </div>
              {active && afterLine ? afterLine : null}
            </div>
          );
        })}
      </div>
    </div>
  );
});
