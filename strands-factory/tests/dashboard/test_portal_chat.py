"""The Maintainer Portal's chat model (dashboard/web-next/src/lib/chat.ts), driven for real in node.

The portal has no JS test runner; node >= 22.6 runs TypeScript directly (type stripping), so these
tests import the module as-is and script it. Skipped when node is missing or too old (CI's
ubuntu-latest has node 22+).

What these guard is the one rule of the chat transcript: content renders IN THE ORDER IT ARRIVED.
Text and tool activity interleave exactly as the stream (or the stored SDK message) had them; the
end-of-turn repair and the failure caption only ever edit the open text block, put a missed head
FIRST, or APPEND — never move text after the tools that followed it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

WEB_NEXT = Path(__file__).resolve().parents[2] / "dashboard" / "web-next"
CHAT_TS = WEB_NEXT / "src" / "lib" / "chat.ts"
MARKDOWN_TSX = WEB_NEXT / "src" / "components" / "Markdown.tsx"


def _node_ok() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30).stdout
    m = re.match(r"v(\d+)\.(\d+)", out.strip())
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= (22, 6)


pytestmark = pytest.mark.skipif(not _node_ok(), reason="node >= 22.6 (TypeScript type stripping) not installed")

# Helpers every script gets: a compact block signature ("T(text) ⚙name ✓") so assertions read as
# the transcript would, and a stream replayer.
_PRELUDE = """
const shape = (blocks) => blocks.map((b) =>
  b.kind === "text" ? `T(${b.text})${b.failed ? "!" : ""}` : b.kind === "tool" ? `⚙${b.name}` : "✓").join(" ");
const play = (live, events) => { for (const ev of events) chat.applyEvent(live, ev); return live; };
const text = (t) => ({ kind: "text", text: t });
const tool = (name, id) => ({ kind: "tool_start", tool: name, toolUseId: id });
const result = (id, t = "ok") => ({ kind: "tool_result", toolUseId: id, data: { status: "success", content: [{ text: t }] } });
"""


def _run(body: str) -> object:
    """Run ``body`` (which must ``return`` a JSON-able value) against the real chat.ts."""
    src = (
        f"const chat = await import({json.dumps(CHAT_TS.as_uri())});\n"
        + _PRELUDE
        + f"const out = await (async () => {{ {body} }})();\nconsole.log(JSON.stringify(out));\n"
    )
    proc = subprocess.run(
        ["node", "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", src],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip())


def test_the_stream_interleaves_text_and_tools_in_arrival_order() -> None:
    """The sequence text1 · tool1 · tool2 · text2 · tool3 · tool4 · text3 renders as exactly
    that — not every tool first and the text merged after."""
    out = _run(
        """
        const live = play(chat.emptyTurn(), [
          text("text1 "), tool("a", "1"), tool("b", "2"), result("1"), result("2"),
          text("text2 "), tool("c", "3"), tool("d", "4"), text("text3"),
        ]);
        return { shape: shape(live.blocks), text: live.text };
        """
    )
    assert out == {
        "shape": "T(text1 ) ⚙a ⚙b ✓ ✓ T(text2 ) ⚙c ⚙d T(text3)",
        "text": "text1 text2 text3",
    }


def test_finish_turn_strips_the_work_product_from_the_open_block_only() -> None:
    """The runtime's copy is the streamed text minus the work-product block. Only the text block
    still open changes; the blocks before it keep their place."""
    out = _run(
        """
        const live = play(chat.emptyTurn(), [text("Looking. "), tool("shell", "1"), text("Done.\\n\\n<wp>{}</wp>")]);
        chat.finishTurn(live, "Looking. Done.");
        return { shape: shape(live.blocks), text: live.text };
        """
    )
    assert out == {"shape": "T(Looking. ) ⚙shell T(Done.)", "text": "Looking. Done."}


def test_finish_turn_on_a_joined_turn_puts_the_missed_head_first_and_keeps_the_order() -> None:
    """Attached after `text1` streamed; saw text2 · tool3 · tool4 · text3. The runtime's copy adds
    the head we missed — it goes IN FRONT (it streamed before everything we saw). This used to
    collapse the turn into `tool3 · tool4 · text1text2text3`, moving text2 after the tools."""
    out = _run(
        """
        const live = play(chat.emptyTurn(), [text("text2 "), tool("c", "3"), tool("d", "4"), text("text3")]);
        chat.finishTurn(live, "text1 text2 text3");
        return { shape: shape(live.blocks), text: live.text };
        """
    )
    assert out == {"shape": "T(text1 text2 ) ⚙c ⚙d T(text3)", "text": "text1 text2 text3"}


def test_finish_turn_on_a_joined_turn_that_opened_with_a_tool_prepends_a_text_block() -> None:
    out = _run(
        """
        const live = play(chat.emptyTurn(), [tool("c", "3"), text("text2 "), tool("d", "4"), text("text3")]);
        chat.finishTurn(live, "text1 text2 text3");
        return shape(live.blocks);
        """
    )
    assert out == "T(text1 ) ⚙c T(text2 ) ⚙d T(text3)"


def test_finish_turn_tolerates_the_runtime_stripping_leading_whitespace() -> None:
    """A first delta that opens with whitespace + a work-product block makes the runtime `.strip()`
    its copy; the prefix still matches and the layout stands (was: collapsed after the tools)."""
    out = _run(
        """
        const live = play(chat.emptyTurn(), [text("\\n\\nLooking. "), tool("shell", "1"), text("Done.\\n\\n<wp>{}</wp>")]);
        chat.finishTurn(live, "Looking. Done.");
        return shape(live.blocks);
        """
    )
    assert out == "T(\n\nLooking. ) ⚙shell T(Done.)"


def test_finish_turn_leaves_a_turn_alone_when_the_copy_matches_nothing_that_streamed() -> None:
    """What arrived, in the order it arrived, is the truth on screen; a copy we can't place changes
    nothing (the durable transcript on reload has the runtime's version)."""
    out = _run(
        """
        const live = play(chat.emptyTurn(), [text("alpha "), tool("shell", "1"), text("beta")]);
        chat.finishTurn(live, "something else entirely");
        return { shape: shape(live.blocks), text: live.text };
        """
    )
    assert out == {"shape": "T(alpha ) ⚙shell T(beta)", "text": "alpha beta"}


def test_finish_turn_fills_a_turn_that_streamed_nothing_and_drops_an_open_block_the_copy_ends_before() -> None:
    out = _run(
        """
        const a = chat.finishTurn(chat.emptyTurn(), "whole answer");
        // Open block was only a work-product block: the copy ends where it started -> block goes.
        const b = play(chat.emptyTurn(), [text("Looking. "), tool("shell", "1"), text("<wp>{}</wp>")]);
        chat.finishTurn(b, "Looking. ");
        return [shape(a.blocks), shape(b.blocks)];
        """
    )
    assert out == ["T(whole answer)", "T(Looking. ) ⚙shell"]


def test_a_failure_caption_is_appended_after_what_streamed_and_is_the_only_block_marked_failed() -> None:
    """`Run failed: …` used to REPLACE the streamed text and sit after every tool. The partial
    answer and the tools stay as they arrived; the caption is one more block, alone in red."""
    out = _run(
        """
        const live = play(chat.emptyTurn(), [text("text1 "), tool("shell", "1"), text("text2")]);
        const failed = chat.turnToMessage(live, { text: "Run failed: boom", failed: true });
        const still = chat.turnToMessage(live, { text: "Still working." });
        const plain = chat.turnToMessage(live, { streaming: true });
        return { failed: shape(failed.blocks), failedText: failed.text, failedFlag: failed.failed,
                 still: shape(still.blocks), plain: shape(plain.blocks) };
        """
    )
    assert out == {
        "failed": "T(text1 ) ⚙shell T(text2) T(Run failed: boom)!",
        "failedText": "text1 text2Run failed: boom",
        "failedFlag": True,
        "still": "T(text1 ) ⚙shell T(text2) T(Still working.)",
        "plain": "T(text1 ) ⚙shell T(text2)",
    }


def test_history_uses_the_api_blocks_in_their_stored_order() -> None:
    """A Memory message whose SDK content was [toolUse, text] renders tool-first — the API's
    `blocks` are used as given. Without `blocks` (an older API) the flat split is re-assembled."""
    out = _run(
        """
        const detail = { session_id: "s", runs: 1, messages: [
          { role: "assistant", text: "after", timestamp: "t",
            blocks: [{ kind: "tool", name: "shell", input: "ls" }, { kind: "text", text: "after" },
                     { kind: "mystery" }, { kind: "text", text: "" }],
            tools: [{ name: "shell", input: "ls" }], tool_results: [] },
          { role: "user", text: "", blocks: [{ kind: "result", status: "success", text: "ok" }],
            tools: [], tool_results: [{ status: "success", text: "ok" }] },
          { role: "assistant", text: "legacy", tools: [{ name: "shell", input: "ls" }], tool_results: [] },
        ]};
        const msgs = chat.sessionDetailToMessages(detail);
        return msgs.map((m) => [m.role, shape(m.blocks)]);
        """
    )
    assert out == [
        ["agent", "⚙shell T(after)"],
        ["user", "✓"],
        ["agent", "T(legacy) ⚙shell"],
    ]


# --- Markdown.tsx: the real component, server-rendered ------------------------------------------
# TSX needs a compiler, so this uses the portal's own esbuild (a vite dependency) and react-dom —
# both only present after `npm ci` in dashboard/web-next (the readiness command runs it; CI's pytest
# job does not, so these skip there).

_ESBUILD = WEB_NEXT / "node_modules" / ".bin" / "esbuild"
_RENDER_JS = """
import { readFileSync } from "node:fs";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MantineProvider } from "@mantine/core";
import { Markdown } from "./Markdown.mjs";
const texts = JSON.parse(readFileSync(0, "utf8"));
const html = (t) => renderToStaticMarkup(React.createElement(MantineProvider, null, React.createElement(Markdown, { text: t })))
  .replace(/<style[\\s\\S]*?<\\/style>/g, "").replace(/ (?:class|style|data-[\\w-]+)="[^"]*"/g, "");
console.log(JSON.stringify(texts.map(html)));
"""

needs_portal_deps = pytest.mark.skipif(
    not _ESBUILD.exists() or not (WEB_NEXT / "node_modules" / "react-dom").exists(),
    reason="dashboard/web-next deps not installed (run `npm ci` there)",
)


def _render_markdown(*texts: str) -> list[str]:
    """Server-render each text through the portal's Markdown component; HTML minus styling attrs."""
    cache = WEB_NEXT / "node_modules" / ".cache"  # inside node_modules so the bundle resolves its imports
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache, prefix="portal-test-") as tmp:
        subprocess.run(
            [str(_ESBUILD), str(MARKDOWN_TSX), "--bundle", "--packages=external", "--platform=node",
             "--format=esm", "--jsx=automatic", "--loader:.css=empty", "--log-level=warning",
             f"--outfile={tmp}/Markdown.mjs"],
            check=True, capture_output=True, text=True, timeout=60,
        )
        Path(tmp, "render.mjs").write_text(_RENDER_JS)
        proc = subprocess.run(
            ["node", f"{tmp}/render.mjs"], input=json.dumps(texts), capture_output=True, text=True, timeout=60,
        )
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip())


@needs_portal_deps
def test_markdown_renders_gfm_and_escapes_raw_html() -> None:
    table, script = _render_markdown("| a | b |\n|---|---|\n| 1 | 2 |", "<script>alert(1)</script><b>x</b>")
    assert "<table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>" in table
    assert "<script>" not in script and "&lt;script&gt;alert(1)&lt;/script&gt;&lt;b&gt;x&lt;/b&gt;" in script


@needs_portal_deps
def test_markdown_links_open_in_a_new_tab_and_unsafe_targets_become_plain_text() -> None:
    ok, js, foot = _render_markdown("[x](https://ok.example)", "[y](javascript:alert(1))", "hi[^1]\n\n[^1]: n")
    assert '<a href="https://ok.example" target="_blank" rel="noopener noreferrer">x</a>' in ok
    assert "<a" not in js and ">y<" in js  # dropped href: no link to "" that reopens the portal
    # A `#fragment` href is a ROUTE under the portal's HashRouter: following one leaves the current
    # view for the `*` fallback (/today). Footnote marks and back-references render as text.
    assert "<a" not in foot and "<sup>1</sup>" in foot and "↩" in foot


@needs_portal_deps
def test_markdown_never_fetches_images() -> None:
    """An <img> would make the maintainer's browser call whatever host the (untrusted) text names.
    Offer it as a link instead, labelled by its alt text or the URL."""
    pixel, bare, js, badge = _render_markdown(
        "![pixel](https://evil.example/t.png)", "![](https://e.example/t.png)", "![x](javascript:1)",
        "[![ci](https://img.example/b.svg)](https://link.example)",
    )
    assert "<img" not in pixel + bare + js + badge
    assert '<a href="https://evil.example/t.png" target="_blank" rel="noopener noreferrer">pixel</a>' in pixel
    assert ">https://e.example/t.png</a>" in bare
    assert "<a" not in js and ">x<" in js
    # A linked image is ONE link — to the link's target, labelled by the image (no <a> inside <a>).
    assert badge.count("<a") == 1 and 'href="https://link.example"' in badge and ">ci</a>" in badge
