import { describe, expect, it } from "vitest";
import type { ChatBlock } from "../api/types";
import type { ChatMessage } from "../api/types";
import {
  type ActivityRow,
  anchoredScrollTop,
  clipDetail,
  contextLabel,
  foldToolTurns,
  groupBlocks,
  historyCaption,
  isNearBottom,
  isNearTop,
  isThinking,
  nextPinState,
  prependPage,
  PREVIEW_SOURCE_MAX,
  previewInput,
} from "./chatView";

describe("scroll pinning", () => {
  it("is near the bottom within the threshold", () => {
    expect(isNearBottom(900, 100, 1000)).toBe(true);
    expect(isNearBottom(860, 100, 1000)).toBe(true); // 40px away
    expect(isNearBottom(800, 100, 1000)).toBe(false); // 100px away
    expect(isNearBottom(0, 500, 300)).toBe(true); // content shorter than the viewport
  });
  it("unpins when the user scrolls up and clears the pill when they come back", () => {
    let st = { pinned: true, unseen: false };
    st = nextPinState(st, false);
    expect(st).toEqual({ pinned: false, unseen: false });
    st = { ...st, unseen: true }; // new content arrived while away
    st = nextPinState(st, false);
    expect(st).toEqual({ pinned: false, unseen: true });
    st = nextPinState(st, true);
    expect(st).toEqual({ pinned: true, unseen: false });
  });
});

describe("groupBlocks", () => {
  it("keeps stream order and folds a result into the call with the same id", () => {
    const blocks: ChatBlock[] = [
      { kind: "text", text: "Let me look." },
      { kind: "tool", id: "a", name: "shell", input: "ls" },
      { kind: "result", id: "a", status: "success", text: "a b c" },
      { kind: "tool", id: "b", name: "read", input: "x" },
      { kind: "text", text: "Done." },
    ];
    const rows = groupBlocks(blocks);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toEqual({ kind: "text", text: "Let me look." });
    expect(rows[1]).toEqual({
      kind: "activity",
      items: [
        { kind: "tool", name: "shell", input: "ls", sub: undefined, result: { status: "success", text: "a b c" } },
        { kind: "tool", name: "read", input: "x", sub: undefined },
      ],
    });
    expect(rows[2]).toEqual({ kind: "text", text: "Done." });
  });
  it("pairs a multi-call turn's results by id, not by position", () => {
    // read(read-1), shell(shell-2), result(read-1), result(shell-2) — the real stream order.
    const rows = groupBlocks([
      { kind: "tool", id: "read-1", name: "read", input: "missing.txt" },
      { kind: "tool", id: "shell-2", name: "shell", input: "pwd" },
      { kind: "result", id: "read-1", status: "error", text: "missing.txt: file not found" },
      { kind: "result", id: "shell-2", status: "success", text: "/workspace" },
    ]);
    expect(rows).toEqual([
      {
        kind: "activity",
        items: [
          { kind: "tool", name: "read", input: "missing.txt", sub: undefined, result: { status: "error", text: "missing.txt: file not found" } },
          { kind: "tool", name: "shell", input: "pwd", sub: undefined, result: { status: "success", text: "/workspace" } },
        ],
      },
    ]);
  });
  it("pairs across a text row (the result landed after narration) and never pairs the same call twice", () => {
    const rows = groupBlocks([
      { kind: "tool", id: "a", name: "t" },
      { kind: "text", text: "meanwhile…" },
      { kind: "result", id: "a", status: "ok", text: "1" },
      { kind: "result", id: "a", status: "ok", text: "dup" },
    ]);
    expect(rows).toEqual([
      { kind: "activity", items: [{ kind: "tool", name: "t", input: undefined, sub: undefined, result: { status: "ok", text: "1" } }] },
      { kind: "text", text: "meanwhile…" },
      { kind: "activity", items: [{ kind: "result", status: "ok", text: "dup" }] },
    ]);
  });
  it("never guesses: results without an id (or whose call is unknown) stay independent rows", () => {
    const rows = groupBlocks([
      { kind: "result", status: "error", text: "boom" },
      { kind: "tool", name: "t" },
      { kind: "result", status: "ok", text: "1" },
      { kind: "tool", id: "x", name: "u" },
      { kind: "result", id: "y", status: "ok", text: "2" },
    ]);
    expect(rows).toEqual([
      {
        kind: "activity",
        items: [
          { kind: "result", status: "error", text: "boom" },
          { kind: "tool", name: "t", input: undefined, sub: undefined },
          { kind: "result", status: "ok", text: "1" },
          { kind: "tool", name: "u", input: undefined, sub: undefined },
          { kind: "result", status: "ok", text: "2" },
        ],
      },
    ]);
  });
  it("does not mutate the input blocks", () => {
    const tool: ChatBlock = { kind: "tool", id: "a", name: "t", input: "i" };
    groupBlocks([tool, { kind: "result", id: "a", status: "ok", text: "r" }]);
    expect(tool).toEqual({ kind: "tool", id: "a", name: "t", input: "i" });
  });
});

describe("previewInput", () => {
  it("collapses JSON objects to key: value pairs and clips", () => {
    expect(previewInput('{"command": "ls -la", "timeout": 5}')).toBe("command: ls -la · timeout: 5");
    expect(previewInput("  multi\n  line   text ")).toBe("multi line text");
    expect(previewInput("x".repeat(200), 10)).toBe("xxxxxxxxx…");
    expect(previewInput(undefined)).toBe("");
    expect(previewInput("{not json")).toBe("{not json");
  });
});

describe("isThinking", () => {
  it("shows dots while streaming with no text at the tail", () => {
    expect(isThinking(true, null)).toBe(true);
    expect(isThinking(true, [])).toBe(true);
    expect(isThinking(true, [{ kind: "activity", items: [] }])).toBe(true);
    expect(isThinking(true, [{ kind: "text", text: "hi" }])).toBe(false);
    expect(isThinking(false, [])).toBe(false);
  });
});

describe("history window", () => {
  const msg = (historyId: string | undefined, text = historyId ?? "live"): ChatMessage => ({
    role: "user",
    text,
    ...(historyId ? { historyId } : {}),
  });

  it("anchors the scroll position across a prepend (no jump)", () => {
    // Reader at scrollTop 120 in a 1000px document; 1800px of older messages land on top.
    expect(anchoredScrollTop(120, 1000, 2800)).toBe(1920);
    // Nothing prepended (or a shrink): stay where we were.
    expect(anchoredScrollTop(120, 1000, 1000)).toBe(120);
    expect(anchoredScrollTop(120, 1000, 900)).toBe(120);
    expect(anchoredScrollTop(0, 0, 500)).toBe(500);
  });

  it("flags the top zone for auto-loading", () => {
    expect(isNearTop(0)).toBe(true);
    expect(isNearTop(200)).toBe(true);
    expect(isNearTop(201)).toBe(false);
    expect(isNearTop(50, 40)).toBe(false);
  });

  it("prepends an older page in order, dropping ids already on screen", () => {
    const existing = [msg("m5"), msg("m6"), msg(undefined, "streaming")];
    const older = [msg("m3"), msg("m4"), msg("m5")];
    const merged = prependPage(existing, older);
    expect(merged.map((m) => m.text)).toEqual(["m3", "m4", "m5", "m6", "streaming"]);
    // Live bubbles (no historyId) in the older page are kept, not deduped away.
    expect(prependPage([msg("a")], [msg(undefined, "x")]).map((m) => m.text)).toEqual(["x", "a"]);
  });

  it("returns the same array when the page adds nothing", () => {
    const existing = [msg("m1"), msg("m2")];
    expect(prependPage(existing, [])).toBe(existing);
    expect(prependPage(existing, [msg("m1"), msg("m2")])).toBe(existing);
  });

  it("captions the end of history by cause", () => {
    expect(historyCaption("cursor", null)).toBeNull();
    expect(historyCaption("cursor", "ledger_window")).toBeNull(); // more pages first
    expect(historyCaption(null, null)).toBe("Beginning of conversation");
    expect(historyCaption(undefined, undefined)).toBe("Beginning of conversation");
    expect(historyCaption(null, "memory_event_limit")).toBe("Older history unavailable (memory ceiling)");
    expect(historyCaption(null, "ledger_window")).toMatch(/ledger window/);
  });
});

describe("bounded previews", () => {
  it("previewInput only looks at the first 2 KB of a huge payload", () => {
    const huge = "{" + '"k":"' + "v".repeat(PREVIEW_SOURCE_MAX * 4) + '"}';
    const out = previewInput(huge);
    expect(out.length).toBeLessThanOrEqual(96);
    expect(out.startsWith('{"k":"vvv')).toBe(true); // not JSON after the cut — raw text path
  });
  it("clipDetail passes small text through and clips large text with a size note", () => {
    expect(clipDetail("ok")).toBe("ok");
    expect(clipDetail(undefined)).toBe("");
    const big = "x".repeat(40 * 1024);
    const out = clipDetail(big);
    expect(out.length).toBeLessThan(big.length);
    expect(out).toMatch(/40 KB total/);
    expect(clipDetail("abcdef", 3)).toMatch(/^abc\n…/);
  });
});

describe("foldToolTurns — stored turns become the bubble a stream would have drawn", () => {
  const h = (id: string, m: Omit<ChatMessage, "historyId">): ChatMessage => ({ ...m, historyId: id });
  // What handler._memory_messages emits for one agent turn with two tool calls (the real shape).
  const ask = h("u1", { role: "user", text: "look around", blocks: [{ kind: "text", text: "look around" }], timestamp: "t0" });
  const calls = h("a1", {
    role: "agent",
    text: "Checking.",
    blocks: [
      { kind: "text", text: "Checking." },
      { kind: "tool", id: "r1", name: "read", input: "x" },
      { kind: "tool", id: "s2", name: "shell", input: "pwd" },
    ],
    timestamp: "t1",
  });
  const results = h("u2", {
    role: "user",
    text: "",
    blocks: [
      { kind: "result", id: "s2", status: "success", text: "/w" },
      { kind: "result", id: "r1", status: "error", text: "nope" },
    ],
    timestamp: "t2",
  });
  const answer = h("a2", { role: "agent", text: "Done.", blocks: [{ kind: "text", text: "Done." }], timestamp: "t3" });

  it("folds calls + results + follow-up into ONE agent bubble, in stream order", () => {
    const out = foldToolTurns([ask, calls, results, answer]);
    expect(out).toHaveLength(2);
    expect(out[0]).toBe(ask);
    expect(out[1].role).toBe("agent");
    expect(out[1].blocks!.map((b) => b.kind)).toEqual(["text", "tool", "tool", "result", "result", "text"]);
    expect(out[1].text).toBe("Checking.\nDone.");
    expect(out[1].timestamp).toBe("t3"); // when the turn finished
    expect(out[1].historyId).toBe("a1");
    expect(out[1].historyIds).toEqual(["a1", "u2", "a2"]);
    // …and groupBlocks then pairs each result with its call by id: no spinner, no orphan rows.
    const rows = groupBlocks(out[1].blocks!);
    expect(rows.map((r) => r.kind)).toEqual(["text", "activity", "text"]);
    const items = (rows[1] as { items: ActivityRow[] }).items;
    expect(items).toEqual([
      { kind: "tool", name: "read", input: "x", sub: undefined, result: { status: "error", text: "nope" } },
      { kind: "tool", name: "shell", input: "pwd", sub: undefined, result: { status: "success", text: "/w" } },
    ]);
  });
  it("keeps folding while the turn keeps calling tools; stops once it ends in text", () => {
    const more = h("a3", { role: "agent", text: "", blocks: [{ kind: "tool", id: "q", name: "grep" }], timestamp: "t4" });
    const moreResults = h("u3", { role: "user", text: "", blocks: [{ kind: "result", id: "q", status: "success", text: "hit" }] });
    const next = h("u4", { role: "user", text: "thanks", blocks: [{ kind: "text", text: "thanks" }] });
    const out = foldToolTurns([ask, calls, results, more, moreResults, answer, next]);
    expect(out.map((m) => m.role)).toEqual(["user", "agent", "user"]);
    expect(out[1].blocks!.map((b) => b.kind)).toEqual(["text", "tool", "tool", "result", "result", "tool", "result", "text"]);
    expect(out[2]).toBe(next);
  });
  it("is idempotent — folding a folded list changes nothing (the page seam re-runs it)", () => {
    const once = foldToolTurns([ask, calls, results, answer]);
    expect(foldToolTurns(once)).toEqual(once);
  });
  it("never glues a result onto a bubble whose calls it does not answer", () => {
    const foreign = h("u9", { role: "user", text: "", blocks: [{ kind: "result", id: "zz", status: "success", text: "?" }] });
    const out = foldToolTurns([calls, foreign]);
    expect(out).toHaveLength(2);
    // …nor onto a bubble with no open call at all (legacy, no ids).
    const plain = h("a0", { role: "agent", text: "hi", blocks: [{ kind: "text", text: "hi" }] });
    const legacyResult = h("u0", { role: "user", text: "", blocks: [{ kind: "result", status: "success", text: "?" }] });
    expect(foldToolTurns([plain, legacyResult])).toHaveLength(2);
  });
  it("folds by position when the API has no ids (legacy blocks)", () => {
    const legacyCalls = h("a0", { role: "agent", text: "", blocks: [{ kind: "tool", name: "shell", input: "ls" }] });
    const legacyResult = h("u0", { role: "user", text: "", blocks: [{ kind: "result", status: "success", text: "a" }] });
    const out = foldToolTurns([legacyCalls, legacyResult, answer]);
    expect(out).toHaveLength(1);
    expect(out[0].blocks!.map((b) => b.kind)).toEqual(["tool", "result", "text"]);
  });
  it("leaves live bubbles and panel-local messages alone", () => {
    const live: ChatMessage = { role: "agent", text: "", blocks: [{ kind: "tool", id: "l", name: "t" }], streaming: true };
    const liveRes: ChatMessage = { role: "user", text: "", blocks: [{ kind: "result", id: "l", status: "ok", text: "" }] };
    expect(foldToolTurns([live, liveRes])).toHaveLength(2);
    const send: ChatMessage = { role: "user", text: "go", id: 7 };
    const reply: ChatMessage = { role: "agent", text: "", blocks: [{ kind: "tool", id: "x", name: "t" }, { kind: "result", id: "x", status: "ok", text: "" }] };
    const follow: ChatMessage = { role: "agent", text: "ok", blocks: [{ kind: "text", text: "ok" }], id: 8 };
    expect(foldToolTurns([send, reply, follow])).toHaveLength(3);
  });
  it("marks the bubble failed when any part was", () => {
    const failedAnswer = h("a2", { role: "agent", text: "boom", failed: true, blocks: [{ kind: "text", text: "boom", failed: true }] });
    const out = foldToolTurns([calls, results, failedAnswer]);
    expect(out).toHaveLength(1);
    expect(out[0].failed).toBe(true);
  });
  it("prependPage folds the seam and dedups against every folded id", () => {
    // Page 1 (newest) opened with the results + answer; the calls were on the older page.
    const newest = foldToolTurns([results, answer, h("u5", { role: "user", text: "more" })]);
    expect(newest[0].role).toBe("user"); // nothing to fold into yet
    const older = [ask, calls];
    const stitched = prependPage(newest, older);
    expect(stitched.map((m) => m.role)).toEqual(["user", "agent", "user"]);
    expect(stitched[1].blocks!.map((b) => b.kind)).toEqual(["text", "tool", "tool", "result", "result", "text"]);
    expect(stitched[1].historyIds).toEqual(["a1", "u2", "a2"]);
    // A re-served boundary: every id already on screen is dropped, including ones inside the fold.
    expect(prependPage(stitched, [calls, results])).toBe(stitched);
    expect(prependPage(stitched, [ask])).toBe(stitched);
  });
});

describe("context blocks (text the agent added to a turn)", () => {
  const h = (id: string, m: Omit<ChatMessage, "historyId">): ChatMessage => ({ ...m, historyId: id });
  const ctx = (source: string, text = "added"): ChatBlock => ({ kind: "context", source, text });

  it("render as a compact item after the user's text, not inside it", () => {
    const rows = groupBlocks([{ kind: "text", text: "what about this?" }, ctx("caller_context:injector")]);
    expect(rows).toEqual([
      { kind: "text", text: "what about this?" },
      { kind: "activity", items: [ctx("caller_context:injector")] },
    ]);
  });

  it("a tool-result message with context still folds into the turn, and the follow-up too", () => {
    const calls = h("a1", {
      role: "agent",
      text: "",
      blocks: [{ kind: "tool", id: "t1", name: "shell", input: "{}" }],
    });
    const results = h("u2", {
      role: "user",
      text: "",
      blocks: [{ kind: "result", id: "t1", status: "success", text: "ok" }, ctx("caller_context:injector")],
    });
    const answer = h("a2", { role: "agent", text: "Done.", blocks: [{ kind: "text", text: "Done." }] });
    const out = foldToolTurns([calls, results, answer]);
    expect(out).toHaveLength(1);
    expect(out[0].blocks!.map((b) => b.kind)).toEqual(["tool", "result", "context", "text"]);
    expect(out[0].text).toBe("Done.");
  });

  it("a message of context alone is not a tool-result message", () => {
    const calls = h("a1", { role: "agent", text: "", blocks: [{ kind: "tool", id: "t1", name: "x" }] });
    const lone = h("u2", { role: "user", text: "", blocks: [ctx("caller_context:injector")] });
    expect(foldToolTurns([calls, lone])).toHaveLength(2);
  });

  it("labels the chip by its source in words", () => {
    expect(contextLabel("caller_context:injector")).toBe("caller");
    expect(contextLabel("strands:environment:injector")).toBe("environment");
    expect(contextLabel("github-context-injector:injector")).toBe("github");
    expect(contextLabel("summary")).toBe("summary");
    expect(contextLabel("strands:context-injector")).toBe("");
  });
});
