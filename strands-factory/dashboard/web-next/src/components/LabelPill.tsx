/**
 * LabelPill / LabelPills — the ONE way a GitHub label renders across Today-adjacent lists, Issues
 * and PRs. `dot` (list rows): neutral pill with the label's color as a dot. `filled` (detail pages):
 * GitHub-style, label color as background with YIQ-picked text. `LabelPills` caps a row at `max`
 * and says "+N" for the rest.
 */
import { labelBg, labelTextColor, type GhLabel } from "../lib/labelColor";
import classes from "./LabelPill.module.css";

export function LabelPill({ label, variant = "dot" }: { label: GhLabel; variant?: "dot" | "filled" }) {
  const bg = labelBg(label.color);
  if (variant === "filled") {
    return (
      <span
        className={`${classes.pill} ${classes.pillFilled}`}
        style={{ background: bg, color: labelTextColor(label.color) }}
        title={label.name}
      >
        {label.name}
      </span>
    );
  }
  return (
    <span className={classes.pill} title={label.name}>
      <LabelDot color={label.color} />
      {label.name}
    </span>
  );
}

/** Just the colored dot (for pickers / compact rows). */
export function LabelDot({ color }: { color: string }) {
  return <span className={classes.dot} style={{ background: labelBg(color) }} />;
}

export function LabelPills({
  labels,
  max = 6,
  variant = "dot",
}: {
  labels: GhLabel[];
  max?: number;
  variant?: "dot" | "filled";
}) {
  if (!labels.length) return null;
  const shown = labels.slice(0, max);
  return (
    <div className={classes.list}>
      {shown.map((l) => (
        <LabelPill key={l.name} label={l} variant={variant} />
      ))}
      {labels.length > max ? <span className={classes.more}>+{labels.length - max}</span> : null}
    </div>
  );
}
