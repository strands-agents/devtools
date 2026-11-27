import { renderToString } from "react-dom/server";
import { MantineProvider } from "@mantine/core";
import { describe, expect, it } from "vitest";
import { SessionTitle } from "./SessionTitle";

function render(title: string | null): string {
  return renderToString(
    <MantineProvider>
      <SessionTitle title={title} fallback="Conversation" hint="portal-alice-20300101-abcdef" onRename={() => {}} />
    </MantineProvider>,
  );
}

describe("SessionTitle", () => {
  it("shows the title as a rename button", () => {
    const html = render("Release prep");
    expect(html).toContain("Release prep");
    expect(html).toContain('aria-label="Rename conversation: Release prep"');
    expect(html).toContain('title="portal-alice-20300101-abcdef"');
    expect(html).not.toContain("Conversation<");
  });

  it("falls back to today's label when untitled", () => {
    const html = render(null);
    expect(html).toContain("Conversation");
    expect(html).toContain('aria-label="Rename conversation: Conversation"');
  });

  it("escapes a title instead of rendering it as markup", () => {
    expect(render("<img src=x onerror=alert(1)>")).not.toContain("<img");
  });
});
