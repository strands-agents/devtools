/**
 * Chat helpers shared by the API client and the AgentChat panel.
 *
 * `SessionDetail` mirrors GET /api/sessions/{id} (see dashboard/api/handler.py `_session_detail` +
 * `_memory_messages`): when AgentCore Memory is wired the response carries verbatim `messages`
 * (with the agent's own narration + structured tool activity); otherwise it carries ledger-derived
 * `turns` (one prompt + result per run). `sessionDetailToMessages` prefers the richer `messages`
 * and falls back to the turns, producing the flat `ChatMessage[]` the panel renders.
 *
 * `applyEvent` folds one streamed `ChatEvent` into a mutable live agent turn: text deltas, tool calls with
 * their complete arguments (`tool_input`), tool results, and a streaming tool's nested activity
 * (`tool_stream`; no backend tool forwards it today). The turn is an ORDERED list
 * of blocks: text and tool activity interleave exactly as they arrived (a flat text + tools split
 * rendered every tool first and all the text merged after it). Turn BOUNDARIES
 * (`turn_start` rollovers into a fresh bubble) are the panel's concern, not this module's: the
 * panel owns the message list, so it decides when a boundary opens a new bubble; `applyEvent` only
 * handles the boundaries that mutate the CURRENT turn (`resume`/`retry` dropping a superseded
 * draft). Nothing in this module REORDERS blocks: once a block is on the list it keeps its place
 * (`finishTurn` edits the open text block and may put a missed head in front; `turnToMessage`
 * appends a caption) — a transcript that shuffles text after tools misrepresents what the agent did.
 */
import type { ChatBlock, ChatEvent, ChatMessage, SessionRun } from "../api/types";

interface SessionDetailTurn {
  task_id?: string;
  status?: string;
  started_at?: string;
  ended_at?: string;
  prompt?: string;
  result?: string;
  error?: string;
  /** The model string and effort the run recorded (absent on rows from before model selection). */
  model?: string;
  effort?: string;
}

/** One content block of a Memory message, in the order the SDK message held it (handler.py `blocks`). */
interface SessionDetailBlock {
  kind?: string;
  /** Tool-use id on `tool` and `result` blocks (pairs a result with its call). */
  id?: string;
  text?: string;
  name?: string;
  input?: string;
  status?: string;
  /** What added a `context` block (handler.py `_parse_content`). */
  source?: string;
}

const idOf = (b: SessionDetailBlock): { id?: string } => (typeof b.id === "string" && b.id ? { id: b.id } : {});

interface SessionDetailMessage {
  /** Stable server id — present on paginated reads (`?limit`), what older pages dedup against. */
  id?: string;
  role?: string;
  /** A ledger-reconstructed run error (paginated reads mark the reply with `failed: true`). */
  failed?: boolean;
  text?: string;
  /** The message's content in its original order; older API builds omit it (see `tools`/`tool_results`). */
  blocks?: SessionDetailBlock[];
  tools?: { name?: string; input?: string }[];
  tool_results?: { status?: string; text?: string }[];
  timestamp?: string;
}

export interface SessionDetail {
  session_id: string;
  runs: number;
  turns?: SessionDetailTurn[];
  messages?: SessionDetailMessage[];
  source?: string;
  /** Paginated reads only (`?limit`): the cursor for the page before `messages`, null at the beginning. */
  next_cursor?: string | null;
  /** Paginated reads only: why accessible history ends early (see dashboard/README.md), else null. */
  history_boundary?: "memory_event_limit" | "ledger_window" | null;
  /** The session's title, null when it has none. */
  title?: string | null;
}

/** True for a `?limit`/`?before` response: `messages` IS the page, even when empty (never fall back to turns). */
export function isPaginatedDetail(detail: SessionDetail): boolean {
  return "next_cursor" in detail;
}

const asRole = (raw: string | undefined): "user" | "agent" =>
  (raw || "").toLowerCase() === "user" ? "user" : "agent";

/** Flatten a session-detail payload into rendered chat messages (Memory-verbatim preferred). */
export function sessionDetailToMessages(detail: SessionDetail): ChatMessage[] {
  // A paginated page is authoritative even when empty: its `turns` are metadata only (no prompt/
  // result), so "falling back" to them would render nothing — or, on an older API, repeat history.
  if (detail.messages && (detail.messages.length > 0 || isPaginatedDetail(detail))) {
    // One Memory message is one SDK message: its text, then the tool calls it made (an assistant
    // message), or the results that came back (the tool-result message) — already in order.
    return detail.messages.map((m) => {
      const text = m.text ?? "";
      // The API's `blocks` carry the message's content in its ORIGINAL order — use them as they
      // are. Only an API that predates `blocks` gets the flat split re-assembled (text, then the
      // calls, then the results — the one order that split can still express).
      const blocks: ChatBlock[] = Array.isArray(m.blocks)
        ? m.blocks.flatMap((b): ChatBlock[] => {
            if (b.kind === "text") return b.text ? [{ kind: "text", text: b.text, ...(m.failed ? { failed: true } : {}) }] : [];
            if (b.kind === "tool") return [{ kind: "tool", ...idOf(b), name: b.name ?? "tool", input: b.input }];
            if (b.kind === "result") return [{ kind: "result", ...idOf(b), status: b.status ?? "unknown", text: b.text ?? "" }];
            if (b.kind === "context") return b.text ? [{ kind: "context", source: b.source ?? "", text: b.text }] : [];
            return [];
          })
        : [
            ...(text ? [{ kind: "text", text, ...(m.failed ? { failed: true } : {}) } as ChatBlock] : []),
            ...(m.tools ?? []).map((t): ChatBlock => ({ kind: "tool", name: t.name ?? "tool", input: t.input })),
            ...(m.tool_results ?? []).map(
              (r): ChatBlock => ({ kind: "result", status: r.status ?? "unknown", text: r.text ?? "" }),
            ),
          ];
      return {
        role: asRole(m.role),
        text,
        blocks,
        timestamp: m.timestamp,
        ...(m.failed ? { failed: true } : {}),
        ...(typeof m.id === "string" && m.id ? { historyId: m.id } : {}),
      };
    });
  }
  // Ledger reconstruction: each run is a user prompt followed by the agent's result/error.
  const out: ChatMessage[] = [];
  for (const t of detail.turns ?? []) {
    if (t.prompt) out.push({ role: "user", text: t.prompt, timestamp: t.started_at });
    if (t.error) out.push({ role: "agent", text: t.error, timestamp: t.ended_at, failed: true });
    else if (t.result) out.push({ role: "agent", text: t.result, timestamp: t.ended_at });
  }
  return out;
}

/**
 * A "running" ledger row older than this is presumed dead, not live — the terminal ledger write is
 * fail-open, so a recycled instance can leave a run "running" forever. Mirrors the dashboard API's
 * STALE_RUN_MINUTES (dashboard/api/handler.py): attaching the panel to a dead run would poll a task
 * that never finishes.
 */
const ACTIVE_RUN_STALE_MINUTES = 1440;

/**
 * The session's in-flight run, if the ledger shows one worth attaching to: the NEWEST turn still
 * marked "running" (turns arrive oldest-first), skipped when it is stale (see above). This is how
 * the panel discovers a run that was launched elsewhere — a mention, another tab, a colleague —
 * so it can attach at the live head instead of showing a frozen transcript.
 */
export function sessionDetailActiveRun(
  detail: SessionDetail,
  now: Date = new Date(),
): { taskId: string; startedAt?: string } | null {
  const running = (detail.turns ?? []).filter((t) => t.status === "running" && t.task_id);
  const newest = running[running.length - 1];
  if (!newest?.task_id) return null;
  if (newest.started_at) {
    const started = Date.parse(newest.started_at);
    if (Number.isFinite(started) && now.getTime() - started > ACTIVE_RUN_STALE_MINUTES * 60_000) {
      return null;
    }
  }
  return { taskId: newest.task_id, startedAt: newest.started_at };
}

/**
 * A live, mutable agent turn accumulated from a stream before it lands as a ChatMessage. `text` is
 * the turn's whole streamed text; `blocks` the ordered content (a text block per stretch of text
 * between tool activity).
 */
export interface LiveTurn {
  text: string;
  blocks: ChatBlock[];
}

export function emptyTurn(): LiveTurn {
  return { text: "", blocks: [] };
}

type ToolBlock = Extract<ChatBlock, { kind: "tool" }>;

function findTool(live: LiveTurn, id: string): ToolBlock | undefined {
  return live.blocks.find((b): b is ToolBlock => b.kind === "tool" && b.id === id);
}

/** Drop the turn's text (a superseded draft), keeping the tool activity that really ran. */
function dropText(live: LiveTurn): void {
  live.text = "";
  live.blocks = live.blocks.filter((b) => b.kind !== "text");
}

function eventText(content: { text?: string }[] | undefined): string {
  return (content ?? [])
    .map((b) => b?.text ?? "")
    .filter(Boolean)
    .join("\n");
}

// Bounds for a streaming tool's nested subagent activity: cap the line count (a subagent grinding
// through hundreds of tool calls must not grow the list unboundedly) and tail-clip the coalesced
// narration line.
const SUB_MAX_LINES = 100;
const SUB_TEXT_TAIL = 240;

/**
 * Fold one streamed event into the live turn (mutating it). Returns the same reference for
 * convenience. Unknown/no-visual kinds (reasoning, done, init) are ignored.
 */
export function applyEvent(live: LiveTurn, ev: ChatEvent): LiveTurn {
  const data = ev.data ?? {};
  switch (ev.kind) {
    case "turn_start":
      // A `resume` boundary re-runs the same ask: drop the superseded draft so the revised
      // answer replaces it rather than appending onto it.
      if (data.reason === "resume") dropText(live);
      break;
    case "retry":
      dropText(live);
      break;
    case "text": {
      if (!ev.text) break;
      live.text += ev.text;
      const last = live.blocks[live.blocks.length - 1];
      if (last?.kind === "text") last.text += ev.text;
      else live.blocks.push({ kind: "text", text: ev.text });
      break;
    }
    case "tool_start":
    case "tool_input": {
      // `tool_start` fires on the first input delta (empty/partial input); `tool_input` carries the
      // call's COMPLETE arguments and fills the block in. Either may arrive first on a tail attach.
      const id = ev.toolUseId || `t${live.blocks.length}`;
      const existing = findTool(live, id);
      if (existing) existing.input = data.input ?? existing.input;
      else live.blocks.push({ kind: "tool", id, name: ev.tool || "tool", input: data.input });
      break;
    }
    case "tool_stream": {
      // A streaming tool's intermediate progress ({kind: tool_start|tool_result|text, ...}
      // payloads), nested under the parent call so its steps read as that call's children. No
      // backend tool forwards it today. Validate the payload BEFORE touching the turn (an
      // unknown shape must not create an empty artifact).
      const inner = data.kind;
      if (inner !== "tool_start" && inner !== "tool_result" && !(inner === "text" && data.text)) break;
      const id = ev.toolUseId || "";
      let parent = findTool(live, id);
      if (!parent) {
        // Parent call unseen (buffer trimmed, or we attached mid-run): make a stub to hang the
        // child activity off, rather than dropping proof the call is alive.
        parent = { kind: "tool", id: id || `t${live.blocks.length}`, name: ev.tool || "tool" };
        live.blocks.push(parent);
      }
      const sub = parent.sub ?? (parent.sub = []);
      if (inner === "tool_start") {
        const inp = data.input ? " · " + String(data.input).slice(0, 120) : "";
        sub.push({ kind: "tool", text: `⚙ ${data.tool || "tool"}${inp}` });
      } else if (inner === "tool_result") {
        const failed = data.status === "error";
        sub.push({ kind: "result", text: `${failed ? "✗" : "✓"} ${data.tool || "tool"} · ${data.status || "ok"}`, failed });
      } else {
        // Coalesce narration into one rolling tail-clipped line instead of stacking chunks.
        const chunk = String(data.text ?? "");
        const last = sub[sub.length - 1];
        if (last && last.kind === "text") last.text = (last.text + chunk).slice(-SUB_TEXT_TAIL);
        else sub.push({ kind: "text", text: chunk.slice(-SUB_TEXT_TAIL) });
      }
      while (sub.length > SUB_MAX_LINES) sub.shift();
      break;
    }
    case "tool_result":
      live.blocks.push({
        kind: "result",
        ...(ev.toolUseId ? { id: ev.toolUseId } : {}),
        status: data.status || "ok",
        text: eventText(data.content),
      });
      break;
    default:
      break; // reasoning / done / init / unknown: no bubble mutation
  }
  return live;
}

/** The turn's text as the text blocks currently hold it (the `live.text` invariant). */
const textOf = (blocks: ChatBlock[]): string =>
  blocks.map((b) => (b.kind === "text" ? b.text : "")).join("");

/**
 * Adopt the runtime's copy of the turn's text (`lastTurnResult`, or a single-turn `result`) into
 * the live turn. That copy is what streamed with the work-product block stripped — or more than we
 * saw (the head of a turn we joined late) — so the blocks already on screen KEEP THEIR ORDER and
 * only two edits are ever made: the text block still open takes the runtime's version of its own
 * stretch (work product gone, a missed tail filled in), and text that streamed before we attached
 * is put where it belongs — in front of everything we saw. A copy that matches nothing we streamed
 * changes nothing: what arrived, in the order it arrived, stands.
 */
export function finishTurn(live: LiveTurn, text: string): LiveTurn {
  const last = live.blocks[live.blocks.length - 1];
  const open = last?.kind === "text" ? last : null;
  // Everything that streamed before the open block.
  const closed = live.text.slice(0, live.text.length - (open?.text.length ?? 0));
  const setOpen = (rest: string) => {
    if (open) {
      if (rest) open.text = rest;
      else live.blocks.pop();
    } else if (rest) {
      live.blocks.push({ kind: "text", text: rest });
    }
  };
  // The runtime `.strip()`s its copy, so a turn whose first delta opened with whitespace still
  // matches — without that lead.
  const prefix = text.startsWith(closed) ? closed : text.startsWith(closed.trimStart()) ? closed.trimStart() : null;
  if (prefix !== null) {
    setOpen(text.slice(prefix.length));
  } else {
    // Joined mid-turn: the runtime has text from BEFORE our first event. Anchor on what we did see
    // and put the missing head first — it streamed before every block here.
    const at = closed ? text.indexOf(closed) : -1;
    if (at > 0) {
      const head = text.slice(0, at);
      const first = live.blocks[0];
      if (first?.kind === "text") first.text = head + first.text; // one stretch, not two bubbles
      else live.blocks.unshift({ kind: "text", text: head });
      setOpen(text.slice(at + closed.length));
    }
  }
  live.text = textOf(live.blocks);
  return live;
}

/**
 * Snapshot a live turn as a ChatMessage bubble. `opts.text` (an error, a fallback caption) is
 * APPENDED as one more text block after everything that streamed — the tool activity and the
 * partial text stay exactly as they arrived; with `opts.failed` that block alone is the one styled
 * as the failure.
 */
export function turnToMessage(
  live: LiveTurn,
  opts: { streaming?: boolean; failed?: boolean; text?: string; note?: string } = {},
): ChatMessage {
  const blocks: ChatBlock[] = live.blocks.map((b) => ({ ...b }));
  if (opts.text !== undefined) blocks.push({ kind: "text", text: opts.text, failed: opts.failed });
  return {
    role: "agent",
    text: textOf(blocks),
    blocks,
    streaming: opts.streaming,
    failed: opts.failed,
    note: opts.note,
  };
}

/** The session's runs with the model each recorded, oldest first (the ledger `turns`). */
export function sessionDetailRuns(detail: SessionDetail): SessionRun[] {
  return (detail.turns ?? [])
    .filter((t): t is SessionDetailTurn & { task_id: string } => Boolean(t.task_id))
    .map((t) => ({
      taskId: t.task_id,
      startedAt: t.started_at,
      status: t.status,
      ...(t.model ? { model: t.model } : {}),
      ...(t.effort ? { effort: t.effort } : {}),
    }));
}
