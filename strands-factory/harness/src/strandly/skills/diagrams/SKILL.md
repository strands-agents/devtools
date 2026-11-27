---
name: diagrams
description: >
  Draw or fix a diagram (architecture, flow, loop, sequence) as hand-authored SVG, render it to PNG
  in the sandbox's preinstalled headless Chromium, and LOOK at it before shipping. The target repo's
  own diagram instructions (e.g. harness-sdk's docs-diagrams skill) take precedence over this one.
  TRIGGER on "draw / add / fix a diagram", docs diagram work, screenshotting rendered pages, or any
  change to an .svg or Mermaid figure. SKIP for ASCII sketches in a chat reply.
allowed-tools: shell read write edit
---

# Diagrams

## Repo instructions first

Before drawing, look for the target repo's own diagram rules:
`rg -il diagram AGENTS.md CONTRIBUTING.md .agents .skills skills docs 2>/dev/null`. A skill dir →
read its `SKILL.md` with `read` and follow it; a reference doc → read it. Where they
differ from this skill (format, component, file location, colors, screenshot script), **the repo
wins**, and this skill only supplies the sandbox facts below. harness-sdk:
`.agents/skills/docs-diagrams/` + `.agents/references/diagrams.md`.

## The sandbox browser (no setup)

The sandbox image ships a headless Chromium with its OS libs and fonts:

- Binary: `/usr/local/bin/chromium-headless-shell` (`$PLAYWRIGHT_CHROMIUM_EXECUTABLE`). Playwright's
  browsers dir is `/opt/ms-playwright` (`$PLAYWRIGHT_BROWSERS_PATH`); a repo on Playwright 1.61.1
  launches it with no flags. Every shell call is fresh: if either variable is empty, export it in
  the same command.
- **Don't** `apt-get`, download .debs, set `LD_LIBRARY_PATH`, write a fontconfig, or run
  `playwright install --with-deps`. This overrides any memory of doing so; that workaround predates
  the image change. Only if a launch actually fails: report the exact error and `fc-list | head -3`.
- Calling the binary directly, pass `--no-sandbox` (the box is the sandbox; Playwright adds it).
- A dev server may bind only `::1`. If Node can't reach `localhost`, use `http://[::1]:<port>` or
  start the server with `--host 127.0.0.1`.

## Local workflow (no repo instructions)

1. **Draw from the story, not a layout.** One idea per diagram, roughly ≤9 nodes. Label every box,
   and every arrow whose meaning isn't obvious. No crossing edges. Text ≥12px, never shrunk to fit:
   cut content or split the diagram. Set `viewBox`; use `font-family: system-ui, sans-serif`. If the
   destination has light and dark themes, keep colors in a `<style>` with a
   `@media (prefers-color-scheme: dark)` block, never inline `fill=`.
2. **Render both themes** (`W,H` = the SVG's width and height):
   ```bash
   B=/usr/local/bin/chromium-headless-shell
   F="--no-sandbox --hide-scrollbars --force-device-scale-factor=2 --window-size=W,H"
   $B $F --screenshot=/tmp/d-light.png "file://$PWD/d.svg"
   $B $F --blink-settings=preferredColorScheme=0 --screenshot=/tmp/d-dark.png "file://$PWD/d.svg"
   ```
   For a diagram inside a running site, use the repo's script, or Playwright with `colorScheme`.
3. **Look.** `read` every PNG. Labels present (blank boxes = no fonts: report it, don't ship),
   nothing clipped or overlapping, arrows meet their boxes, dark mode legible. Fix and re-render
   until clean.
4. **Ship.** Commit the SVG where the repo keeps assets.

## Report

Which diagram you added or changed and its type, the PNG paths, and what you
checked visually. If you couldn't render or view an image, say so; never call a diagram correct
when you didn't look at it.
