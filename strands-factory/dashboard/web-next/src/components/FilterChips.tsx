/**
 * FilterChips — the ONE quick-filter row used by Today / Issues / PRs (and their repo facets), so
 * every list filters with the same affordance: Mantine `Chip`s sized to a ≥ 40px tap target, an
 * optional dimmed count per chip, either wrapping (few options) or as a horizontally scrolling rail
 * (many options at 360px). Single-select; the active chip is `value`.
 *
 * Option values MUST be non-empty: Mantine reports "" when the active chip is clicked again, so an
 * option whose value is "" could never be selected. Use `ALL` for the "no filter" option instead.
 */
import { Chip } from "@mantine/core";
import classes from "./FilterChips.module.css";

/** Sentinel value for the "everything" option (e.g. "All repos") — map it back to `null` in `onChange`. */
export const ALL = "*";

export interface FilterChipOption<V extends string> {
  value: V;
  label: string;
  /** Dimmed tabular count after the label (omitted when undefined). */
  count?: number;
  /** Mantine color for this chip (defaults to the theme primary). */
  color?: string;
  /** Not selectable right now (e.g. a viewer-only filter with no token); shows `title` on hover. */
  disabled?: boolean;
  title?: string;
}

export function FilterChips<V extends string>({
  value,
  onChange,
  options,
  variant = "light",
  layout = "wrap",
  ariaLabel,
}: {
  value: V;
  /** Fires with the clicked chip's value. Clicking the active chip fires it again (never "none"). */
  onChange: (value: V) => void;
  options: readonly FilterChipOption<V>[];
  /** `light` for the primary filter row; `outline` for secondary facets (repo chips). */
  variant?: "light" | "outline";
  /** `wrap` lets chips flow onto new lines; `scroll` keeps one row that scrolls sideways. */
  layout?: "wrap" | "scroll";
  ariaLabel?: string;
}) {
  return (
    <Chip.Group
      multiple={false}
      value={value}
      // Mantine reports "" when the active chip is clicked again — keep the current selection.
      onChange={(v) => (typeof v === "string" && v !== "" ? onChange(v as V) : undefined)}
    >
      <div className={layout === "scroll" ? classes.rail : classes.wrap} role="group" aria-label={ariaLabel}>
        {options.map((o) => (
          <Chip
            key={o.value}
            value={o.value}
            size="md"
            radius="xl"
            variant={variant}
            color={o.color}
            disabled={o.disabled}
            title={o.title}
            classNames={{ label: classes.chipLabel }}
          >
            {o.label}
            {o.count !== undefined ? <span className={classes.count}>{o.count}</span> : null}
          </Chip>
        ))}
      </div>
    </Chip.Group>
  );
}
