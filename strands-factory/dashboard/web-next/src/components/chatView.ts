/**
 * Pure helpers for AgentChat's PRESENTATION — nothing here touches the run-train protocol
 * (see lib/chat for that). Kept out of the component so the scroll-pinning rule and the way a turn's
 * blocks collapse into rows are unit-testable.
 */
import type { ChatBlock, ChatMessage, HistoryBoundary } from "../api/types";

/** How close to the bottom (px) still counts as "reading the latest" for auto-scroll. */
export const PIN_THRESHOLD_PX = 48;

/** True when a scroll position is within `threshold` of the bottom (the user is following along). */
export function isNearBottom(
  scrollTop: number,
  clientHeight: number,
  scrollHeight: number,
  threshold: number = PIN_THRESHOLD_PX,
): boolean {
  return scrollHeight - (scrollTop + clientHeight) <= threshold;
}

/**
 * Pin-state transition on a scroll event: pinned follows the position; the "new messages" pill
 * clears the moment the user is back at the bottom (nothing is unseen any more).
 */
export function nextPinState(
  prev: { pinned: boolean; unseen: boolean },
  nearBottom: boolean,
): { pinned: boolean; unseen: boolean } {
  return { pinned: nearBottom, unseen: nearBottom ? false : prev.unseen };
}

/** One rendered line of tool activity: a call, paired with its result when the ids match. */
export interface ToolRow {
  kind: "tool";
  name: string;
  input?: string;
  sub?: Extract<ChatBlock, { kind: "tool" }>["sub"];
  result?: { status: string; text: string };
}
/** A result that cannot be paired with a call (no id on either side, or its call is not in this message). */
export interface ResultRow {
  kind: "result";
  status: string;
  text: string;
}
export type TextRow = Extract<ChatBlock, { kind: "text" }>;
export type ContextRow = Extract<ChatBlock, { kind: "context" }>;
export type ActivityRow = ToolRow | ResultRow | ContextRow;
export type Row = TextRow | { kind: "activity"; items: ActivityRow[] };

/**
 * A message's blocks as rendered rows, in stream order: each stretch of text is a row; each run of
 * consecutive tool activity is ONE compact group. A result is folded into its call's row (one line:
 * name · input · ✓) ONLY when both carry the same tool-use id — results of a multi-call turn come
 * back in completion order, so "the call just before" is a guess that misattributes errors.
 * Unpaired results stay as their own rows. A `context` block is a compact item of the group too.
 */
export function groupBlocks(blocks: ChatBlock[]): Row[] {
  const rows: Row[] = [];
  const pending = new Map<string, ToolRow>();
  for (const b of blocks) {
    if (b.kind === "text") {
      rows.push(b);
      continue;
    }
    if (b.kind === "result" && b.id && pending.has(b.id)) {
      const call = pending.get(b.id)!;
      pending.delete(b.id);
      call.result = { status: b.status, text: b.text };
      continue;
    }
    let last = rows[rows.length - 1];
    if (last?.kind !== "activity") {
      last = { kind: "activity", items: [] };
      rows.push(last);
    }
    if (b.kind === "tool") {
      const row: ToolRow = { kind: "tool", name: b.name, input: b.input, sub: b.sub };
      last.items.push(row);
      if (b.id) pending.set(b.id, row);
    } else if (b.kind === "context") {
      last.items.push(b);
    } else {
      last.items.push({ kind: "result", status: b.status, text: b.text });
    }
  }
  return rows;
}

// --- History window (pagination) ------------------------------------------------------------------

/** Messages per transcript page: the newest 50 on open, 50 more per "Load older messages". */
export const HISTORY_PAGE_SIZE = 50;

/** Scrolling within this many px of the top auto-loads the next older page (throttled). */
export const LOAD_OLDER_THRESHOLD_PX = 200;

/** Minimum gap between two scroll-triggered older-page loads. */
export const LOAD_OLDER_THROTTLE_MS = 800;

/** True when the reader is within `threshold` of the top (the cue to fetch older history). */
export function isNearTop(scrollTop: number, threshold: number = LOAD_OLDER_THRESHOLD_PX): boolean {
  return scrollTop <= threshold;
}

/**
 * Where to put `scrollTop` after content was PREPENDED so the reader sees exactly what they saw
 * before: the viewport grew by `nextScrollHeight - prevScrollHeight`, and everything they were
 * looking at moved down by that much. Never negative (a shrink — nothing prepended — stays put).
 */
export function anchoredScrollTop(prevScrollTop: number, prevScrollHeight: number, nextScrollHeight: number): number {
  return Math.max(0, prevScrollTop + Math.max(0, nextScrollHeight - prevScrollHeight));
}

/**
 * Stitch an older page onto the front of the transcript, dropping any message whose server id is
 * already on screen (a page boundary can be re-served after a reload race; live bubbles have no
 * `historyId` and are never matched). Order within the page is kept (oldest first). The seam is
 * then re-folded (see `foldToolTurns`): a turn split across the two pages — the agent's tool calls
 * on the older page, their results and the follow-up text on the newer one — becomes one bubble.
 */
export function prependPage(existing: ChatMessage[], older: ChatMessage[]): ChatMessage[] {
  if (older.length === 0) return existing;
  const seen = new Set<string>();
  for (const m of existing) for (const id of historyIdsOf(m)) seen.add(id);
  const fresh = older.filter((m) => !historyIdsOf(m).some((id) => seen.has(id)));
  return fresh.length ? foldToolTurns([...fresh, ...existing]) : existing;
}

/** Every server message id a transcript row stands for (a folded turn carries all of its parts'). */
export function historyIdsOf(m: ChatMessage): string[] {
  if (m.historyIds && m.historyIds.length) return m.historyIds;
  return m.historyId ? [m.historyId] : [];
}

// --- Folding stored turns -------------------------------------------------------------------------

/**
 * Fold a STORED transcript into the bubbles a live stream would have produced.
 *
 * The SDK stores one agent turn as several messages: the assistant message (text + `toolUse`
 * blocks), then a user-role message holding ONLY the `toolResult` blocks, then the assistant's
 * follow-up (more text or more calls) — and so on until the turn ends in plain text. Rendered 1:1
 * that shows a tool row spinning forever, its result right-aligned inside a *user* bubble, and the
 * agent's answer split over several bubbles; streaming shows the whole turn in ONE bubble. This
 * restores that shape:
 *
 *   - a user-role message made only of `result` blocks is appended to the agent bubble before it
 *     (its results pair with that bubble's calls by tool-use id — `groupBlocks` does the pairing);
 *   - an agent message that follows such a fold (the bubble now ends in a result) is appended too —
 *     it is the same turn continuing — and the chain goes on until the agent message ends in text.
 *
 * Only history is folded: a bubble that is still `streaming`, or any panel-local message (`id`: a
 * send, or a live turn's "not sent" caption), is left exactly where it is. The folded
 * bubble keeps the FIRST part's `historyId` and all parts' ids in `historyIds` (page dedup), takes
 * the LAST part's timestamp (when the turn finished), and is `failed` when any part was. Pure and
 * idempotent — safe to re-run over an already folded list (the page seam does exactly that).
 */
export function foldToolTurns(messages: ChatMessage[]): ChatMessage[] {
  const out: ChatMessage[] = [];
  for (const m of messages) {
    const prev = out[out.length - 1];
    if (prev && canFoldInto(prev, m)) {
      out[out.length - 1] = mergeInto(prev, m);
      continue;
    }
    out.push(m);
  }
  return out;
}

const isHistory = (m: ChatMessage): boolean => !m.streaming && m.id === undefined;

// A tool-result message may also carry context the agent added (older transcripts); it still folds.
const isResultOnly = (m: ChatMessage): boolean =>
  !!m.blocks &&
  m.blocks.some((b) => b.kind === "result") &&
  m.blocks.every((b) => b.kind === "result" || b.kind === "context");

const endsInResult = (m: ChatMessage): boolean => {
  const last = m.blocks?.filter((b) => b.kind !== "context").pop();
  return last?.kind === "result";
};

/** True when `prev` has more calls than results — at least one call is still waiting (ids or not). */
function hasOpenCall(prev: ChatMessage): boolean {
  let n = 0;
  for (const b of prev.blocks ?? []) {
    if (b.kind === "tool") n += 1;
    else if (b.kind === "result") n -= 1;
  }
  return n > 0;
}

/** The ids of `prev`'s calls that no result in `prev` has answered yet. */
function openCallIds(prev: ChatMessage): Set<string> {
  const open = new Set<string>();
  for (const b of prev.blocks ?? []) {
    if (b.kind === "tool" && b.id) open.add(b.id);
    else if (b.kind === "result" && b.id) open.delete(b.id);
  }
  return open;
}

function canFoldInto(prev: ChatMessage, m: ChatMessage): boolean {
  if (prev.role !== "agent" || !isHistory(prev) || !isHistory(m) || !prev.blocks) return false;
  if (isResultOnly(m)) {
    // The tool-result message of the turn. When ids are known on both sides they must agree — a
    // result for a call that is not in this bubble belongs to some other turn (or to nothing we
    // can see), and must not be glued onto the wrong one.
    const open = openCallIds(prev);
    const ids = m.blocks!.flatMap((b) => (b.kind === "result" && b.id ? [b.id] : []));
    if (ids.length > 0 && open.size > 0) return ids.every((id) => open.has(id));
    // No ids to check on one side (an API that predates them): fall back to "the bubble has a call
    // still waiting for a result" — the only thing a result-only message can answer.
    return hasOpenCall(prev);
  }
  // The assistant continuing after its tools came back: the bubble so far ends in a result.
  return m.role === "agent" && endsInResult(prev);
}

function mergeInto(prev: ChatMessage, m: ChatMessage): ChatMessage {
  const blocks = [...(prev.blocks ?? []), ...(m.blocks ?? [])];
  const text = blocks.map((b) => (b.kind === "text" ? b.text : "")).filter(Boolean).join("\n");
  const historyIds = [...historyIdsOf(prev), ...historyIdsOf(m)];
  return {
    ...prev,
    text,
    blocks,
    timestamp: m.timestamp ?? prev.timestamp,
    ...(prev.failed || m.failed ? { failed: true } : {}),
    ...(historyIds.length ? { historyIds } : {}),
  };
}

/**
 * The line that replaces the "Load older messages" button once there is no older page: why the
 * history stops here. Null while a cursor still points at more.
 */
export function historyCaption(nextCursor: string | null | undefined, boundary: HistoryBoundary | null | undefined): string | null {
  if (nextCursor) return null;
  if (boundary === "memory_event_limit") return "Older history unavailable (memory ceiling)";
  if (boundary === "ledger_window") return "Older history unavailable (only the recent ledger window is kept)";
  return "Beginning of conversation";
}

// --- Bounded previews -----------------------------------------------------------------------------

/** How much of a tool input/result the collapsed one-line preview even LOOKS at (bytes of text). */
export const PREVIEW_SOURCE_MAX = 2 * 1024;

/** Expanded tool detail is clipped here (chars) — a 5 MB result must not become a 5 MB `<pre>`. */
export const DETAIL_MAX_CHARS = 32 * 1024;

/** `text` clipped to `max` chars with a size note, or as-is when it fits. */
export function clipDetail(text: string | undefined, max: number = DETAIL_MAX_CHARS): string {
  if (!text) return "";
  if (text.length <= max) return text;
  const kb = Math.round(text.length / 1024);
  return `${text.slice(0, max)}\n… (${kb} KB total — open on the run for the full output)`;
}

/**
 * One-line preview of a tool's input: JSON collapsed to `key: value` pairs, whitespace squashed,
 * clipped. Only the first PREVIEW_SOURCE_MAX chars are considered so a huge payload is not parsed
 * or regex-scanned on every render.
 */
export function previewInput(input: string | undefined, max = 96): string {
  if (!input) return "";
  let s = (input.length > PREVIEW_SOURCE_MAX ? input.slice(0, PREVIEW_SOURCE_MAX) : input).trim();
  if (s.startsWith("{")) {
    try {
      const obj = JSON.parse(s) as Record<string, unknown>;
      s = Object.entries(obj)
        .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
        .join(" · ");
    } catch {
      // not JSON after all — fall through to the raw text
    }
  }
  s = s.replace(/\s+/g, " ");
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

/**
 * What added a context chip's text, from the block's `source`: its words without the `strands:`
 * prefix, "injector" or "context" (`caller_context:injector` → "caller",
 * `github-context-injector:injector` → "github"). Empty when the source says nothing more.
 */
export function contextLabel(source: string): string {
  return source
    .replace(/^strands:/, "")
    .split(/[_:\-\s]+/)
    .filter((word) => word && word !== "injector" && word !== "context")
    .join(" ");
}

/** True when a streaming agent message should show the "thinking" dots instead of a text caret. */
export function isThinking(streaming: boolean | undefined, rows: Row[] | null): boolean {
  if (!streaming) return false;
  if (!rows || rows.length === 0) return true;
  return rows[rows.length - 1].kind !== "text";
}
