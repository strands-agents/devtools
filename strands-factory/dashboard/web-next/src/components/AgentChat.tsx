/**
 * AgentChat — a reusable streaming chat panel that talks to the agent behind a session.
 *
 * Given a `sessionId` (the canonical `gh-<owner>-<repo>-<h8>-<kind>-<number>` key), it loads the prior
 * transcript, then launches + polls chat turns against POST/GET /api/chat: a launch returns a task
 * id, and a since-cursor poll loop streams the run's events back (text deltas, tool activity) so the
 * agent is seen thinking in real time. The run executes detached on the runtime — closing the panel
 * never stops the agent, and reopening the session reloads the durable transcript.
 *
 * Sending is NEVER blocked by a turn in flight: a message posted mid-run SUPERSEDES the running turn
 * (the runtime cancels it — it ends `cancelled` — and starts a new run that sees the whole
 * conversation, so "stop, wrong repo" takes effect at once). A per-panel DRIVER streams the runs
 * this panel watches one after another: the superseded run's bubble closes with a "stopped" caption,
 * then the new run streams into its own bubble. A resumed turn inside one run (a critic retry)
 * stays on its task id and is handled at its `turn_start`.
 *
 * On mount the panel also ATTACHES to a run it did not launch: the session snapshot surfaces the
 * ledger's in-flight run (see lib/chat sessionDetailActiveRun), and the panel joins it at the live
 * head (`since = -1`) — streaming a mention-triggered run, a colleague's turn, or a mid-run reload
 * exactly like a turn it started itself.
 *
 * This is the "talk to the agent that did the task" surface: the agent answers with its own memory
 * at `sessionId`, so "re-check finding 3 against the new commit" reaches the same agent that
 * produced the work product — no new GitHub comment is spawned (a chat turn is a conversation, not
 * a publish). Works end-to-end against the mock API (a canned streamed reply, including a
 * superseded turn) with no backend.
 *
 * History is WINDOWED, not loaded whole: the panel opens on the newest HISTORY_PAGE_SIZE messages
 * (`GET /api/sessions/{id}?limit=50`) and walks back one page at a time — a "Load older messages"
 * button at the top, also triggered by scrolling near it — prepending each page while keeping the
 * reader's scroll position anchored (see chatView `anchoredScrollTop`). Pages are deduplicated by
 * the server's message id; a 409 `cursor_expired` (history changed under us) offers a reload. Rows
 * are memoized so a streaming turn re-renders only its own bubble, never every old Markdown one.
 *
 * Presentation (this file's JSX + AgentChat.module.css) is separate from the protocol above: the
 * driver, `applyEvent`, captions and cancel are untouched by layout changes.
 * `layout="fill"` makes the panel take its parent's height (the immersive /strandly/:id column);
 * the default `panel` layout keeps a fixed-height embed for issue/PR pages.
 */
import {
  forwardRef,
  memo,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { ActionIcon, Alert, Badge, Loader, Text, Textarea, Tooltip, UnstyledButton } from "@mantine/core";
import {
  IconArrowDown,
  IconArrowUp,
  IconChevronDown,
  IconChevronRight,
  IconPlayerStopFilled,
} from "@tabler/icons-react";
import type {
  ChatEvent,
  ChatMessage,
  ChatPoll,
  HistoryBoundary,
  InvocationContext,
  SessionRun,
  SubActivityLine,
} from "../api/types";
import { useApi } from "../api/ApiContext";
import { ApiError, CursorExpiredError } from "../api/errors";
import { useUser } from "../auth/auth";
import { relTime } from "../lib/format";
import { getUserContext } from "../lib/userContext";
import {
  loadModelSelection,
  saveModelSelection,
  selectionForTurn,
  type ModelSelection,
} from "../lib/modelChoice";
import { Markdown } from "./Markdown";
import { ModelPicker, RunModels } from "./ModelPicker";
import {
  applyEvent,
  emptyTurn,
  finishTurn,
  turnToMessage,
  type LiveTurn,
} from "../lib/chat";
import {
  anchoredScrollTop,
  clipDetail,
  contextLabel,
  foldToolTurns,
  groupBlocks,
  HISTORY_PAGE_SIZE,
  historyCaption,
  isNearBottom,
  isNearTop,
  isThinking,
  LOAD_OLDER_THRESHOLD_PX,
  LOAD_OLDER_THROTTLE_MS,
  nextPinState,
  prependPage,
  previewInput,
  type ActivityRow,
  type ToolRow,
} from "./chatView";
import classes from "./AgentChat.module.css";

const POLL_FIRST_MS = 600;
const POLL_MS = 1200;
const POLL_MAX_ATTEMPTS = 400; // ~8 minutes at the live cadence — then we tell them to check back

// The cursor that means "attach at the live head": the runtime answers with no backlog, just the
// next sequence number (see serve/agentcore_app.py _TaskStore.events_since). Used whenever the
// panel joins a run that was ALREADY in flight (a mount-time attach). Starting such an attach at 0
// would replay the run's whole buffer, redrawing turns the durable transcript above already shows.
const TAIL_CURSOR = -1;

// Consecutive "unknown" polls before we quietly give up on an attach: a stale ledger row can point
// at a task the runtime no longer knows (instance recycled). A run we launched ourselves never
// reports unknown, so this only trims dead mount-time attaches.
const UNKNOWN_GIVE_UP = 3;
/** A `starting` launch (cold session): look for its run every 5 s for up to 3 minutes. */
const COLD_START_ATTACH_INTERVAL_MS = 5000;
const COLD_START_ATTACH_TRIES = 36;

const JOIN_NOTE = "joined this answer in progress — earlier parts of the turn are in the transcript above";
const STOPPED_NOTE = "stopped before it finished";

export interface AgentChatProps {
  sessionId: string;
  /** A short label for the item this session is about (e.g. "owner/repo#42"), shown in the panel header. */
  contextHint?: string;
  /**
   * `panel` (default): a fixed-height embed with its own small header — the issue/PR pages.
   * `fill`: no header, fills the parent's height (the parent is a flex column) — the chat page.
   */
  layout?: "panel" | "fill";
  /** Transcript height in `panel` layout. Ignored by `fill`. */
  height?: number;
  /**
   * Ambient UI state (what the user is viewing in the surrounding view) sent with every turn, so
   * the agent can resolve "this issue"/"this PR" without a pasted URL. Read at send time, so a
   * view that re-renders with a new item automatically stamps the new one.
   */
  invocationContext?: InvocationContext;
  /**
   * A message to send ONCE, right after the transcript loads — the composer-first FAB mints a
   * session and hands the typed text over here. Sent at most once per sessionId for the life of
   * the panel (StrictMode's double effect run cannot send it twice).
   */
  initialMessage?: string;
  /** Fires when a run starts/stops streaming into the panel (the page header's Stop button reads it). */
  onBusyChange?: (busy: boolean) => void;
}

/** What the surrounding page can drive imperatively. */
export interface AgentChatHandle {
  /** Ask the runtime to stop the session's in-flight turn (same as the composer's Stop). */
  stop: () => Promise<void>;
  /** Focus the composer. */
  focus: () => void;
}

/** A run the panel will stream: which task, and HOW to attach to it. */
interface WatchEntry {
  id: string;
  /** Attach at the live head (the run predates this watcher) instead of replaying its buffer. */
  tail: boolean;
}

/** Per-panel bookkeeping, kept in a ref because the driver mutates it inside long async loops. */
interface PanelState {
  tasks: WatchEntry[];
  task: string | null;
  driving: boolean;
  driverGen: number;
}

const freshPanelState = (): PanelState => ({
  tasks: [],
  task: null,
  driving: false,
  driverGen: -1,
});

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
export const AgentChat = forwardRef<AgentChatHandle, AgentChatProps>(function AgentChat(
  { sessionId, contextHint, layout = "panel", height = 340, invocationContext, initialMessage, onBusyChange },
  ref,
) {
  const api = useApi();
  // The signed-in maintainer (cached viewer identity). Stamped into every turn's invocation
  // context below so the agent knows WHO is talking — centrally here, so all chat surfaces
  // (review, triage, standalone chat) get it without each view wiring it.
  const user = useUser();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false); // a driver is streaming (Stop shown) — sending stays open
  const [launching, setLaunching] = useState(false); // POST /api/chat in flight — the send button waits
  const [status, setStatus] = useState<string>("");
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // This browser's model choice (free text, saved), and the session's runs with the model each used.
  const [selection, setSelection] = useState<ModelSelection>(loadModelSelection);
  const [runs, setRuns] = useState<SessionRun[]>([]);
  // The history window: the cursor for the page before the oldest message on screen (null = the
  // beginning of accessible history), why that history ends early, and the older-page load state.
  // Mirrored into refs because the scroll handler and the async loader read them between renders.
  const [history, setHistory] = useState<{ nextCursor: string | null; boundary: HistoryBoundary | null }>({
    nextCursor: null,
    boundary: null,
  });
  const historyRef = useRef(history);
  historyRef.current = history;
  const [loadingOlder, setLoadingOlder] = useState(false);
  const loadingOlderRef = useRef(false);
  const [olderError, setOlderError] = useState<{ message: string; expired: boolean } | null>(null);
  const olderErrorRef = useRef(olderError);
  olderErrorRef.current = olderError;
  // Bumped by "Reload" after a cursor_expired: re-runs the first-page load for the same session.
  const [reloadNonce, setReloadNonce] = useState(0);
  // Scroll geometry captured right before an older page is prepended; consumed by the layout effect.
  const anchorRef = useRef<{ scrollTop: number; scrollHeight: number } | null>(null);
  const lastAutoLoadRef = useRef(0);
  // The scroll handler is memoized once; it reaches the CURRENT loader (current session/api) here.
  const loadOlderRef = useRef<() => Promise<void>>(async () => {});

  // A generation counter aborts an in-flight driver/poll loop when the session changes or the panel
  // unmounts (the run keeps going on the runtime; reopening reloads the transcript).
  const genRef = useRef(0);
  const stateRef = useRef<PanelState>(freshPanelState());
  const msgIdRef = useRef(0);
  const viewportRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // The initial message is read when the transcript lands, and remembered per session so it goes
  // out once — a StrictMode remount or a prop re-render must not re-send it.
  const initialRef = useRef(initialMessage);
  initialRef.current = initialMessage;
  const initialSentFor = useRef<string | null>(null);

  // --- Auto-scroll: follow new content while the reader is at the bottom; otherwise offer a pill.
  const [pin, setPin] = useState({ pinned: true, unseen: false });
  const pinRef = useRef(pin);
  pinRef.current = pin;

  const scrollToBottom = useCallback((smooth = false) => {
    const vp = viewportRef.current;
    if (!vp) return;
    vp.scrollTo({ top: vp.scrollHeight, behavior: smooth ? "smooth" : "auto" });
    setPin({ pinned: true, unseen: false });
  }, []);

  const onScroll = useCallback(() => {
    const vp = viewportRef.current;
    if (!vp) return;
    const near = isNearBottom(vp.scrollTop, vp.clientHeight, vp.scrollHeight);
    setPin((prev) => {
      const next = nextPinState(prev, near);
      return next.pinned === prev.pinned && next.unseen === prev.unseen ? prev : next;
    });
    // Reading near the top with older history available: fetch the next page before they hit the
    // edge (throttled — a scroll fires many times per frame of momentum). Only when there is real
    // room to scroll: the programmatic scroll-to-bottom on open also fires this handler, and in a
    // transcript shorter than the threshold it would otherwise fetch a page nobody asked for. Never
    // after a failed load: an expired cursor fails the same way every time, and re-firing it on
    // every scroll tick would hammer the API — the Retry / Reload button is the only way on.
    const room = vp.scrollHeight - vp.clientHeight;
    if (
      room > LOAD_OLDER_THRESHOLD_PX &&
      isNearTop(vp.scrollTop) &&
      historyRef.current.nextCursor &&
      !loadingOlderRef.current &&
      !olderErrorRef.current
    ) {
      const now = Date.now();
      if (now - lastAutoLoadRef.current >= LOAD_OLDER_THROTTLE_MS) {
        lastAutoLoadRef.current = now;
        void loadOlderRef.current();
      }
    }
  }, []);

  // Content changed: an older page was prepended → keep the reader on the exact message they were
  // reading (shift scrollTop by the height that landed above it — no jump); otherwise stick to the
  // bottom when pinned (instant — a smooth scroll per streamed delta jitters), else flag that there
  // is something unseen below.
  useLayoutEffect(() => {
    const vp = viewportRef.current;
    if (!vp) return;
    const anchor = anchorRef.current;
    if (anchor) {
      anchorRef.current = null;
      vp.scrollTop = anchoredScrollTop(anchor.scrollTop, anchor.scrollHeight, vp.scrollHeight);
      return;
    }
    if (pinRef.current.pinned) {
      vp.scrollTop = vp.scrollHeight;
    } else if (messages.length > 0) {
      setPin((p) => (p.unseen ? p : { ...p, unseen: true }));
    }
  }, [messages, loadingHistory]);

  useEffect(() => {
    onBusyChange?.(busy);
  }, [busy, onBusyChange]);

  function pickModel(next: ModelSelection) {
    setSelection(next);
    saveModelSelection(next);
  }

  useImperativeHandle(
    ref,
    () => ({
      stop: () => stop(),
      focus: () => textareaRef.current?.focus(),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sessionId],
  );

  // Load prior transcript on session change; bump the generation so any running loop abandons.
  // When the snapshot shows a run already live on the session, ATTACH: watch it tail-first and
  // start the driver, so a mid-run open streams instead of freezing.
  useEffect(() => {
    genRef.current += 1;
    const gen = genRef.current;
    stateRef.current = freshPanelState();
    let live = true;
    setMessages([]);
    setBusy(false);
    setLaunching(false);
    setStatus("");
    setError(null);
    setLoadingHistory(true);
    setHistory({ nextCursor: null, boundary: null });
    setRuns([]);
    setLoadingOlder(false);
    loadingOlderRef.current = false;
    setOlderError(null);
    anchorRef.current = null;
    setPin({ pinned: true, unseen: false });
    // The newest page only (HISTORY_PAGE_SIZE). Older history is walked back on demand — see
    // loadOlder — so a long-lived session never ships (or renders) its whole transcript.
    api
      .loadSession(sessionId, { limit: HISTORY_PAGE_SIZE })
      .then(
        (snap) => {
          if (!live) return;
          // Stored turns arrive as several messages (calls / results / follow-up) — fold them into
          // the one bubble a stream would have drawn. Older pages are folded at their seam by prependPage.
          setMessages(foldToolTurns(snap.messages));
          setHistory({ nextCursor: snap.nextCursor ?? null, boundary: snap.historyBoundary ?? null });
          setRuns(snap.runs ?? []);
          if (snap.activeTaskId && genRef.current === gen) {
            setStatus("A run is already in progress on this session — attaching…");
            watchTask(snap.activeTaskId, { tail: true });
            void drive(gen);
          }
        },
        (e: unknown) => live && setError(e instanceof Error ? e.message : String(e)),
      )
      .then(() => {
        // The composer-first hand-off: send the typed text now that the transcript is on screen
        // (sending earlier would let the snapshot overwrite the bubble). Once per session — and
        // even when the snapshot failed: the list page already stripped the text from history, so
        // the attempt (and its "not sent" caption) is the only place the user's words survive.
        const first = initialRef.current?.trim();
        if (live && first && initialSentFor.current !== sessionId && genRef.current === gen) {
          initialSentFor.current = sessionId;
          void sendText(first);
        }
      })
      .finally(() => live && setLoadingHistory(false));
    return () => {
      live = false;
      genRef.current += 1; // abandon the driver/poll loop on unmount
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, sessionId, reloadNonce]);

  /**
   * Fetch the page before the oldest message on screen and PREPEND it. Single-flight; the result is
   * dropped if the panel moved to another session meanwhile (same generation guard as the first
   * load). Captures the viewport geometry first so the layout effect can hold the reader's place.
   */
  async function loadOlder() {
    const cursor = historyRef.current.nextCursor;
    if (!cursor || loadingOlderRef.current) return;
    const gen = genRef.current;
    loadingOlderRef.current = true;
    setLoadingOlder(true);
    setOlderError(null);
    try {
      const page = await api.loadSession(sessionId, { limit: HISTORY_PAGE_SIZE, before: cursor });
      if (genRef.current !== gen) return; // the user left this session — not ours to render
      setMessages((prev) => {
        const next = prependPage(prev, page.messages);
        // Anchor only when something was really prepended: an all-duplicate page changes nothing,
        // and a lingering anchor would misfire on the next streamed delta instead.
        const vp = viewportRef.current;
        if (next !== prev && vp) anchorRef.current = { scrollTop: vp.scrollTop, scrollHeight: vp.scrollHeight };
        return next;
      });
      setHistory({ nextCursor: page.nextCursor ?? null, boundary: page.historyBoundary ?? null });
    } catch (e) {
      if (genRef.current !== gen) return;
      const expired = e instanceof CursorExpiredError;
      setOlderError({
        message: expired ? "History changed — reload" : "Couldn't load older messages: " + (e instanceof Error ? e.message : String(e)),
        expired,
      });
    } finally {
      if (genRef.current === gen) {
        loadingOlderRef.current = false;
        setLoadingOlder(false);
      }
    }
  }
  loadOlderRef.current = loadOlder;

  /** Replace the LAST streaming bubble (there is at most one); append when none exists. */
  const upsertLive = useCallback((msg: ChatMessage) => {
    setMessages((prev) => {
      const i = prev.map((m) => !!m.streaming).lastIndexOf(true);
      if (i >= 0) {
        const next = [...prev];
        next[i] = msg;
        return next;
      }
      return [...prev, msg];
    });
  }, []);

  /** Finalize the LAST streaming bubble: replace it with `msg`, or remove it when `msg` is null. */
  const closeLive = useCallback((msg: ChatMessage | null) => {
    setMessages((prev) => {
      const i = prev.map((m) => !!m.streaming).lastIndexOf(true);
      if (i < 0) return msg ? [...prev, msg] : prev;
      const next = [...prev];
      if (msg) next[i] = msg;
      else next.splice(i, 1);
      return next;
    });
  }, []);

  /** Update the caption of one message (a send's not-sent note), addressed by panel-local id. */
  const noteFor = useCallback((id: number, note: string | undefined, failed = false) => {
    setMessages((prev) => prev.map((m) => (m.id === id ? { ...m, note, noteFailed: failed } : m)));
  }, []);

  /** Never watch one run twice: a second watcher renders the same events into a second bubble. */
  function watchTask(taskId: string, opts: { tail?: boolean } = {}): boolean {
    const st = stateRef.current;
    if (!taskId || st.task === taskId || st.tasks.some((t) => t.id === taskId)) return false;
    st.tasks.push({ id: taskId, tail: !!opts.tail });
    return true;
  }

  /**
   * One driver per panel, streaming the task ids it watches in order. Single-flight:
   * `driverGen` ties the running driver to its panel generation, so a driver whose panel has been
   * replaced exits at its next loop head instead of rendering into a stale message list.
   */
  async function drive(gen: number) {
    const st = stateRef.current;
    if (st.driving && st.driverGen === gen) return; // already streaming this panel
    st.driving = true;
    st.driverGen = gen;
    setBusy(true);
    try {
      while (genRef.current === gen && st.tasks.length) {
        const next = st.tasks.shift()!;
        st.task = next.id;
        await streamTurn(next.id, gen, next.tail);
      }
    } catch (e) {
      // Nobody awaits drive(); an unexpected error would otherwise be an invisible unhandled
      // rejection with the panel stuck looking busy.
      if (genRef.current === gen) setStatus("Streaming stopped: " + (e instanceof Error ? e.message : String(e)));
    } finally {
      if (st.driverGen === gen) {
        st.driving = false;
        st.task = null;
        if (genRef.current === gen) {
          setBusy(false);
          setStatus("");
        }
      }
    }
  }

  /**
   * Stream ONE run into the panel. A `turn_start` after the run's first opens a new bubble, except
   * a critic retry (`resume`), which reuses the bubble it re-attempts. A run that ends `cancelled`
   * (stopped, or superseded by a newer message) keeps what it streamed under a "stopped" caption.
   */
  async function streamTurn(taskId: string, gen: number, tail: boolean): Promise<void> {
    let live: LiveTurn = emptyTurn();
    let turns = tail ? 1 : 0; // a tail join never saw the first turn_start — the next one IS a rollover
    let reuseTurn = false; // a transient retry's turn_start continues THIS turn (older runtimes name no reason)
    let joined = tail; // the live bubble holds only what arrived after we attached — caption it

    const streamingMsg = () => turnToMessage(live, { streaming: true, note: joined ? JOIN_NOTE : undefined });
    const say = (t: string) => {
      if (genRef.current === gen) setStatus(t);
    };
    const hasContent = () => live.blocks.length > 0;
    const openBubble = () => {
      live = emptyTurn();
      setMessages((prev) => [...prev, streamingMsg()]);
    };
    // Close the current bubble: keep it (un-pended) when the turn produced anything; drop it when
    // it stayed empty — unless it is a turn we JOINED and never got the text for, whose caption is
    // the only pointer at the transcript that has the full answer.
    const closeBubble = () => {
      if (hasContent()) closeLive(turnToMessage(live, { note: joined ? JOIN_NOTE : undefined }));
      else if (joined) closeLive({ role: "agent", text: "", note: JOIN_NOTE });
      else closeLive(null);
    };

    const onEvent = (ev: ChatEvent) => {
      const data = ev.data ?? {};
      if (ev.kind === "turn_start") {
        turns += 1;
        const reason = data.reason;
        if (reason === "resume") {
          // Same ask, second attempt (the goal loop's critic rejected the draft): reuse the bubble
          // and drop the superseded draft.
          applyEvent(live, ev);
          upsertLive(streamingMsg());
          say("Taking another pass at that answer…");
          return;
        }
        // The run's own first turn keeps the bubble already open for it. `reuseTurn` covers the
        // same thing after a transient retry on a runtime too old to name boundaries.
        if (reason === "start" || (reason === undefined && (turns === 1 || reuseTurn))) {
          reuseTurn = false;
          return;
        }
        // Rollover: another turn inside this run (e.g. a background-results continuation).
        closeBubble();
        joined = false;
        openBubble();
        say("Starting the next turn…");
        return;
      }
      if (ev.kind === "retry") {
        // Transient model error, the model call re-runs: the partial draft is being re-generated.
        // (An older runtime re-ran the whole turn, whose turn_start continues this bubble.)
        reuseTurn = true;
        applyEvent(live, ev);
        upsertLive(streamingMsg());
        say("Transient error — retrying…");
        return;
      }
      applyEvent(live, ev);
      upsertLive(streamingMsg());
    };

    openBubble();
    if (tail) say("Attached to the turn already running…");

    let since = tail ? TAIL_CURSOR : 0;
    let attached = false;
    let unknownStreak = 0;
    for (let i = 0; i < POLL_MAX_ATTEMPTS; i++) {
      await sleep(i === 0 ? POLL_FIRST_MS : POLL_MS);
      if (genRef.current !== gen) return; // panel replaced — abandon quietly
      let poll: ChatPoll;
      try {
        poll = await api.pollChat(taskId, sessionId, since);
      } catch (e) {
        // Transient poll error: keep the cursor and retry (nothing is skipped).
        say("Reconnecting… " + (e instanceof Error ? e.message : String(e)));
        continue;
      }
      if (genRef.current !== gen) return;
      // Belt and braces for the tail attach: a runtime that predates the tail cursor answers a
      // negative cursor with the WHOLE buffer; dropping the first response's events keeps the
      // attach from redrawing turns the transcript above already shows.
      const backlog = tail && !attached;
      attached = true;
      if (typeof poll.nextSeq === "number" && poll.nextSeq > since) since = poll.nextSeq;
      if (!backlog) for (const ev of poll.events ?? []) onEvent(ev);

      const stStatus = poll.status;
      if (stStatus === "cancelled") {
        if (hasContent()) closeLive(turnToMessage(live, { note: STOPPED_NOTE }));
        else closeLive(null);
        return;
      }
      if (stStatus === "completed" || stStatus === "failed") {
        if (stStatus === "failed") {
          closeLive(turnToMessage(live, { text: "Run failed: " + (poll.error || "unknown error"), failed: true }));
          return;
        }
        // Prefer the runtime's copy of the FINAL turn — durable, and it fills in whatever the live
        // cursor missed (everything before a tail attach; a run that finished before our first
        // poll). An EMPTY string is a real answer (a tools-only turn). Without it, only a
        // single-turn run we launched may take `result` (for a multi-turn run it is every turn's
        // text MERGED, and on a tail attach `turns` proves nothing).
        const lastTurn = typeof poll.lastTurnResult === "string" ? poll.lastTurnResult : null;
        if (lastTurn !== null) {
          joined = false; // we have the whole turn — the "joined in progress" caption is now untrue
          if (lastTurn) finishTurn(live, lastTurn);
        } else if (poll.result && turns <= 1 && !tail) {
          finishTurn(live, poll.result);
        }
        closeBubble();
        return;
      }
      if (stStatus === "unknown") {
        // A stale attach: the ledger said "running" but the runtime doesn't know the task (recycled
        // instance). Give it a few polls (Memory-backed resolution can lag), then bow out quietly.
        unknownStreak += 1;
        if (unknownStreak >= UNKNOWN_GIVE_UP && !hasContent()) {
          closeLive(null);
          say("");
          return;
        }
      } else {
        unknownStreak = 0;
      }
      say(`Agent is working — streaming live (${stStatus || "running"})…`);
    }
    closeLive(
      live.text
        ? turnToMessage(live)
        : turnToMessage(live, { text: "Still working — check back shortly; the run continues on the runtime." }),
    );
  }
  // Sending is never gated on `busy`: a message sent mid-run supersedes the running turn, and the
  // new run streams into this panel once the superseded one has closed.
  function send() {
    const text = input.trim();
    if (!text) return;
    setInput("");
    void sendText(text);
  }

  async function sendText(text: string) {
    if (!text) return;
    setError(null);
    const gen = genRef.current;
    const st = stateRef.current;
    const midRun = st.driving && st.driverGen === gen;
    const id = ++msgIdRef.current;
    setPin({ pinned: true, unseen: false }); // your own message always scrolls into view
    setMessages((prev) => [
      ...prev,
      { role: "user", text, id, note: midRun ? "sending — this replaces the turn in progress…" : "sending…" },
    ]);

    let launch;
    setLaunching(true);
    try {
      // Identity + the user's standing "Strandly context" (Settings) ride every turn: merge them
      // into whatever ambient context the surrounding view supplied — or carry them alone.
      const userContext = getUserContext();
      const base: InvocationContext = { surface: "maintainer-portal", ...invocationContext };
      if (user.login) base.user = { login: user.login, name: user.name };
      if (userContext) base.userContext = userContext;
      const context: InvocationContext | undefined =
        user.login || userContext || invocationContext ? base : undefined;
      launch = await api.startChat(sessionId, text, {
        triggerSource: "portal:chat",
        invocationContext: context,
        ...selectionForTurn(selection),
      });
    } catch (e) {
      if (genRef.current !== gen) return;
      const detail = e instanceof Error ? e.message : String(e);
      // A 400 names what the server refused (such as a model the harness cannot build): show its words.
      const refusal = e instanceof ApiError && e.status === 400 ? e.serverError : undefined;
      noteFor(
        id,
        detail.includes("503")
          ? "not sent — chat isn't enabled on this deployment (no runtime configured)"
          : "not sent — " + (refusal ?? detail),
        true,
      );
      return;
    } finally {
      if (genRef.current === gen) setLaunching(false);
    }
    // The panel moved on while the launch was in flight (another session opened): the message is
    // sent and the runtime owns it — reopening the session shows it from the transcript.
    if (genRef.current !== gen) return;
    if (launch.status === "starting") {
      // The message reached the runtime but its session is cold-starting (a Runtime instance boots
      // in ~1 min, longer than the API's 30 s): the run WILL start. Don't call it "not sent" (that
      // invites a resend = a duplicate run) — wait for the run to appear on the session and attach.
      noteFor(id, "sent — the agent is starting up (about a minute); its reply will appear here");
      void attachWhenStarted(gen);
      return;
    }
    const taskId = launch.taskId;
    if (!taskId || launch.status === "rejected") {
      noteFor(id, "not sent — " + (launch.reason || launch.error || "the runtime returned no task id"), true);
      return;
    }
    noteFor(id, undefined);
    // The model this turn asked for; a default turn's model is known once the ledger records it.
    setRuns((prev) => [
      ...prev,
      { taskId, startedAt: new Date().toISOString(), status: "running", ...selectionForTurn(selection) },
    ]);
    // The run answering this message is new, so it streams from its first event.
    watchTask(taskId);
    void drive(gen);
  }

  /** After a `starting` launch: poll the session until its run shows up, then watch it. */
  async function attachWhenStarted(gen: number) {
    for (let i = 0; i < COLD_START_ATTACH_TRIES; i++) {
      await sleep(COLD_START_ATTACH_INTERVAL_MS);
      if (genRef.current !== gen) return;
      try {
        const snap = await api.loadSession(sessionId, { limit: 1 });
        if (genRef.current !== gen) return;
        if (snap.activeTaskId) {
          watchTask(snap.activeTaskId);
          void drive(gen);
          return;
        }
      } catch {
        // transient read error: keep waiting
      }
    }
  }

  async function stop() {
    setStatus("Stopping…");
    try {
      const res = await api.cancelChat(sessionId);
      setStatus(res.status === "cancelled" ? "Stop requested — wrapping up…" : "Nothing is running.");
    } catch (e) {
      setStatus("Couldn't stop: " + (e instanceof Error ? e.message : String(e)));
    }
    // The poll loop resolves on the run's terminal status; the button just signals the runtime.
  }

  const fill = layout === "fill";
  const canSend = input.trim().length > 0 && !launching;

  const onComposerKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter inserts a newline. An IME composition's Enter is left alone.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className={`${classes.root} ${fill ? classes.fill : classes.panel}`}>
      {!fill ? (
        <div className={classes.panelHeader}>
          <Text size="sm" fw={600} className="truncate">
            Talk to Strandly
          </Text>
          {contextHint ? (
            <Badge size="sm" variant="light" radius="sm">
              {contextHint}
            </Badge>
          ) : null}
        </div>
      ) : null}

      <div
        ref={viewportRef}
        className={classes.transcript}
        onScroll={onScroll}
        style={fill ? undefined : { height, flex: "0 0 auto" }}
      >
        <div className={classes.column}>
          {loadingHistory ? (
            <div className={classes.empty}>
              <Loader size="xs" mr={8} style={{ verticalAlign: "middle" }} /> Loading the conversation…
            </div>
          ) : messages.length === 0 ? (
            <div className={classes.empty}>
              {contextHint
                ? "Ask about this item — e.g. “re-check finding 3 against the new commit” or “why did you flag the auth change?”."
                : "Ask Strandly anything about the repos you maintain — it answers from this session's memory; nothing is posted to GitHub."}
            </div>
          ) : (
            <>
              <HistoryEdge
                nextCursor={history.nextCursor}
                boundary={history.boundary}
                loading={loadingOlder}
                error={olderError}
                onLoad={() => void loadOlder()}
                onReload={() => setReloadNonce((n) => n + 1)}
              />
              {messages.map((m, i) => (
                <Message key={m.historyId ?? m.id ?? `m${i}`} message={m} />
              ))}
            </>
          )}
        </div>
      </div>

      {pin.unseen && !pin.pinned ? (
        <div className={classes.pillWrap}>
          <UnstyledButton className={classes.pill} onClick={() => scrollToBottom(true)}>
            <IconArrowDown size={14} /> New messages
          </UnstyledButton>
        </div>
      ) : null}

      <div className={classes.composer}>
        <div className={classes.composerInner}>
          {error ? (
            <Alert color="red" variant="light" p="xs" radius="md">
              {error}
            </Alert>
          ) : null}
          {status ? (
            <div className={classes.status} title={status}>
              {status}
            </div>
          ) : null}
          <div className={classes.inputRow}>
            <Textarea
              ref={textareaRef}
              className={classes.textarea}
              variant="unstyled"
              placeholder={busy ? "Reply — it reaches the running agent at its next step…" : "Message Strandly…"}
              aria-label="Message"
              value={input}
              onChange={(e) => setInput(e.currentTarget.value)}
              onKeyDown={onComposerKey}
              autosize
              minRows={1}
              maxRows={6}
              enterKeyHint="send"
              autoCapitalize="sentences"
            />
            {busy ? (
              <Tooltip label="Stop the running turn" withArrow>
                <ActionIcon
                  size={40}
                  radius="xl"
                  variant="light"
                  color="red"
                  onClick={() => void stop()}
                  aria-label="Stop"
                >
                  <IconPlayerStopFilled size={16} />
                </ActionIcon>
              </Tooltip>
            ) : null}
            <ActionIcon
              size={40}
              radius="xl"
              variant="filled"
              onClick={send}
              disabled={!canSend}
              loading={launching}
              aria-label="Send"
            >
              <IconArrowUp size={18} stroke={2.5} />
            </ActionIcon>
          </div>
          <div className={classes.modelRow}>
            <ModelPicker selection={selection} onChange={pickModel} disabled={launching} />
            <RunModels runs={runs} />
          </div>
        </div>
      </div>
    </div>
  );
});

// --- Presentation ---------------------------------------------------------------------------------

/**
 * The top of the transcript: "Load older messages" while a cursor points at more (also auto-fired
 * from the scroll handler), the reason history ends once it doesn't, and the recovery for a cursor
 * the server no longer honors.
 */
function HistoryEdge({
  nextCursor,
  boundary,
  loading,
  error,
  onLoad,
  onReload,
}: {
  nextCursor: string | null;
  boundary: HistoryBoundary | null;
  loading: boolean;
  error: { message: string; expired: boolean } | null;
  onLoad: () => void;
  onReload: () => void;
}) {
  if (error) {
    return (
      <div className={classes.historyEdge}>
        <span className={`${classes.historyCaption} ${classes.captionFailed}`}>{error.message}</span>
        <UnstyledButton className={classes.loadOlder} onClick={error.expired ? onReload : onLoad}>
          {error.expired ? "Reload" : "Retry"}
        </UnstyledButton>
      </div>
    );
  }
  if (nextCursor) {
    return (
      <div className={classes.historyEdge}>
        <UnstyledButton className={classes.loadOlder} onClick={onLoad} disabled={loading} aria-busy={loading}>
          {loading ? <Loader size={14} color="gray" /> : <IconArrowUp size={14} />}
          {loading ? "Loading older messages…" : "Load older messages"}
        </UnstyledButton>
      </div>
    );
  }
  const caption = historyCaption(nextCursor, boundary);
  return caption ? (
    <div className={classes.historyEdge}>
      <span className={classes.historyCaption}>{caption}</span>
    </div>
  ) : null;
}

/**
 * One transcript row. Memoized: a streaming turn replaces ONE element of the message list per
 * delta, so every other row keeps its props identity and skips re-rendering (and re-parsing its
 * Markdown). The history window is 50–300 rows; with memoized rows that needs no virtualization.
 */
const Message = memo(function Message({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  const rows = message.blocks ? groupBlocks(message.blocks) : null;
  const thinking = isThinking(message.streaming, rows);
  return (
    <div className={`${classes.message} ${isUser ? classes.user : classes.agent}`}>
      <div className={classes.content}>
        {rows ? (
          rows.map((row, i) =>
            row.kind === "text" ? (
              <Bubble key={i} text={row.text} isUser={isUser} failed={row.failed} />
            ) : (
              <Activity key={i} items={row.items} streaming={!!message.streaming} />
            ),
          )
        ) : !thinking ? (
          <Bubble text={message.text} isUser={isUser} failed={message.failed} />
        ) : null}
        {thinking ? <Dots /> : null}
      </div>
      {message.note ? (
        <div className={`${classes.caption} ${message.noteFailed ? classes.captionFailed : ""}`}>{message.note}</div>
      ) : null}
      {message.timestamp && !message.streaming ? <div className={classes.caption}>{relTime(message.timestamp)}</div> : null}
    </div>
  );
});

function Bubble({ text = "", isUser, failed }: { text?: string; isUser: boolean; failed?: boolean }) {
  if (!text && !failed) return null;
  const tone = isUser ? classes.bubbleUser : failed ? classes.bubbleFailed : classes.bubbleAgent;
  return (
    <div className={`${classes.bubble} ${tone}`}>
      {/* The agent writes Markdown — render it. What the user typed and a run's error text are verbatim. */}
      {!isUser && !failed ? <Markdown text={text} /> : text}
    </div>
  );
}

/** The streaming indicator — three pulsing dots. */
function Dots() {
  return (
    <div className={classes.dots} aria-label="Strandly is working" role="status">
      <span />
      <span />
      <span />
    </div>
  );
}

/** One run of tool activity: compact rows in stream order, each expanding into the full detail. */
function Activity({ items, streaming }: { items: ActivityRow[]; streaming: boolean }) {
  return (
    <div className={classes.activity}>
      {items.map((it, i) =>
        it.kind === "tool" ? (
          <ToolLine key={i} row={it} streaming={streaming} />
        ) : it.kind === "context" ? (
          <ContextLine key={i} source={it.source} text={it.text} />
        ) : (
          <ResultLine key={i} status={it.status} text={it.text} />
        ),
      )}
    </div>
  );
}

function Chevron({ open }: { open: boolean }) {
  return open ? (
    <IconChevronDown size={14} className={classes.toolChevron} />
  ) : (
    <IconChevronRight size={14} className={classes.toolChevron} />
  );
}

/**
 * `⚙ name · preview · ✓` — click to expand into the full input, result and nested subagent lines.
 * A call without a result spins only while the bubble is still STREAMING; in a stored transcript a
 * missing result means the run ended before the tool answered (or the result was never kept), not
 * that it is still running — that reads as a dimmed dash.
 */
function ToolLine({ row, streaming }: { row: ToolRow; streaming: boolean }) {
  const [open, setOpen] = useState(false);
  const failed = row.result?.status === "error";
  return (
    <div>
      <UnstyledButton
        className={classes.toolRow}
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <Chevron open={open} />
        <span className={classes.toolName}>{row.name}</span>
        <span className={classes.toolInput}>{open ? "" : previewInput(row.input)}</span>
        {row.result ? (
          <span className={`${classes.toolStatus} ${failed ? classes.err : classes.ok}`}>{failed ? "✗" : "✓"}</span>
        ) : streaming ? (
          <Loader size={12} color="gray" style={{ flex: "0 0 auto", marginTop: 3 }} />
        ) : (
          <span className={`${classes.toolStatus} ${classes.noResult}`} title="No result recorded">
            —
          </span>
        )}
      </UnstyledButton>
      {open ? (
        <div className={classes.detail}>
          <span className={classes.detailLabel}>Input</span>
          <pre className={classes.pre}>{clipDetail(row.input) || "(empty)"}</pre>
          {row.result ? (
            <>
              <span className={classes.detailLabel}>Result · {row.result.status}</span>
              <pre className={classes.pre}>{clipDetail(row.result.text) || "(empty)"}</pre>
            </>
          ) : null}
        </div>
      ) : null}
      {row.sub && row.sub.length > 0 ? <SubActivity lines={row.sub} /> : null}
    </div>
  );
}

/** A result whose call isn't on this message (a tail attach, a Memory tool-result message). */
function ResultLine({ status, text }: { status: string; text: string }) {
  const [open, setOpen] = useState(false);
  const failed = status === "error";
  return (
    <div>
      <UnstyledButton
        className={classes.toolRow}
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <Chevron open={open} />
        <span className={`${classes.toolName} ${failed ? classes.err : ""}`}>{failed ? "✗" : "✓"} result</span>
        <span className={classes.toolInput}>{open ? "" : previewInput(text)}</span>
      </UnstyledButton>
      {open ? (
        <div className={classes.detail}>
          <span className={classes.detailLabel}>Result · {status}</span>
          <pre className={classes.pre}>{clipDetail(text) || "(empty)"}</pre>
        </div>
      ) : null}
    </div>
  );
}

/** Context the agent added to the turn: a small collapsed chip that expands into the text. */
function ContextLine({ source, text }: { source: string; text: string }) {
  const [open, setOpen] = useState(false);
  const label = contextLabel(source);
  return (
    <div>
      <UnstyledButton
        className={classes.contextChip}
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-label={label ? `Context: ${label}` : "Context"}
      >
        <Chevron open={open} />
        <span>context</span>
        {label ? <span className={classes.contextSource}>· {label}</span> : null}
      </UnstyledButton>
      {open ? (
        <div className={classes.detail}>
          <span className={classes.detailLabel}>{label ? `Context · ${label}` : "Context"}</span>
          <pre className={classes.pre}>{clipDetail(text)}</pre>
        </div>
      ) : null}
    </div>
  );
}

/** A streaming tool's nested progress — see SubActivityLine. */
function SubActivity({ lines }: { lines: SubActivityLine[] }) {
  return (
    <div className={classes.sub}>
      {lines.map((line, i) => (
        <div
          key={i}
          className={`${classes.subLine} ${line.kind === "text" ? classes.subText : ""} ${line.failed ? classes.err : ""}`}
          title={line.text}
        >
          ↳ {line.kind === "text" ? "… " : ""}
          {line.text}
        </div>
      ))}
    </div>
  );
}
