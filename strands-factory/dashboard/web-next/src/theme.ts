import { Card, createTheme, Paper, rem, type MantineColorsTuple } from "@mantine/core";

/**
 * Portal theme — dark-first, "fewer lines": surfaces are distinguished by tone, not borders.
 *
 *   - `frog` is the accent (the strands-agents frog green, toned down for text contrast).
 *   - `dark` is a neutral near-black ramp (Mantine's default dark is blue-tinted); `dark[7]` is the
 *     page, `dark[6]` a raised surface, `dark[5]` a hover/selected tone.
 *   - Card/Paper default to NO border and a raised tone; views opt into `withBorder` explicitly
 *     (they shouldn't). Large radii + generous spacing do the separating.
 *
 * Mobile is a first-class target: tap targets ≥ 40px, safe-area insets are handled by AppShell's
 * CSS (styles.css), and the bottom tab bar replaces the sidebar under the `sm` breakpoint.
 */

const frog: MantineColorsTuple = [
  "#e6fff2",
  "#c7f9df",
  "#95f2c0",
  "#5eeb9f",
  "#2fe683",
  "#13e271",
  "#00e067",
  "#00c758",
  "#00b04d",
  "#009840",
];

const dark: MantineColorsTuple = [
  "#d4d4d8", // 0 — text on dark
  "#a1a1aa", // 1 — dimmed text
  "#71717a", // 2 — placeholder
  "#52525b", // 3 — subtle borders (rarely used)
  "#3f3f46", // 4 — dividers
  "#27272a", // 5 — hover / selected
  "#1b1b1f", // 6 — raised surface (cards)
  "#121215", // 7 — page background
  "#0c0c0e", // 8 — header / nav
  "#070708", // 9
];

export const theme = createTheme({
  primaryColor: "frog",
  primaryShade: { light: 7, dark: 5 },
  colors: { frog, dark },
  defaultRadius: "lg",
  fontFamily:
    "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
  fontFamilyMonospace:
    "ui-monospace, 'JetBrains Mono', SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace",
  headings: { fontWeight: "650", fontFamily: "inherit" },
  fontSizes: { xs: rem(12), sm: rem(13.5), md: rem(15), lg: rem(17), xl: rem(20) },
  components: {
    Card: Card.extend({
      defaultProps: { withBorder: false, shadow: "none", radius: "lg", padding: "md" },
      styles: { root: { backgroundColor: "var(--portal-surface)" } },
    }),
    Paper: Paper.extend({
      defaultProps: { withBorder: false, shadow: "none", radius: "lg" },
      styles: { root: { backgroundColor: "var(--portal-surface)" } },
    }),
  },
});
