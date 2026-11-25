/**
 * GitHub label colors — shared by the issue and PR label pills (components/LabelPill.tsx). Pure.
 */

/** A GitHub label as both list queries select it: `name` + hex `color` (without `#`). */
export interface GhLabel {
  name: string;
  color: string;
}

/** Parse a GitHub label hex (`"d73a4a"`, `"#d73a4a"`, 3-digit) → [r,g,b]; null when malformed. */
export function parseHex(hex: string): [number, number, number] | null {
  const h = (hex ?? "").trim().replace(/^#/, "");
  if (/^[0-9a-f]{3}$/i.test(h)) {
    return [h[0] + h[0], h[1] + h[1], h[2] + h[2]].map((p) => parseInt(p, 16)) as [number, number, number];
  }
  if (!/^[0-9a-f]{6}$/i.test(h)) return null;
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

/** Perceived brightness (YIQ, 0–255) — the rule GitHub's own label pills follow. */
export function brightness(rgb: [number, number, number]): number {
  const [r, g, b] = rgb;
  return (r * 299 + g * 587 + b * 114) / 1000;
}

/** Black or white text for a label pill on `hex`, picking whichever reads better. */
export function labelTextColor(hex: string): "#000000" | "#ffffff" {
  const rgb = parseHex(hex);
  if (!rgb) return "#ffffff";
  return brightness(rgb) >= 128 ? "#000000" : "#ffffff";
}

/** `#rrggbb` for a label color, falling back to a neutral gray when the hex is malformed. */
export function labelBg(hex: string): string {
  return parseHex(hex) ? `#${hex.trim().replace(/^#/, "").toLowerCase()}` : "#6e7681";
}
