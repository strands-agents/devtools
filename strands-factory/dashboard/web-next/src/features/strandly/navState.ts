/**
 * Router state shared between the sessions list and the chat page — PURE so the parser is testable.
 * `viewing` is the GitHub item the user came from (issue/PR detail → "Open in Strandly"; rides each
 * turn's invocationContext); `firstMessage` is the composer-first FAB's text (sent once on mount).
 */
import type { ViewingRef } from "../../api/types";

export interface SessionNavState {
  viewing?: ViewingRef;
  firstMessage?: string;
}

/** Tolerant read of `location.state`: anything malformed (a hand-edited history entry) is dropped. */
export function readNavState(state: unknown): SessionNavState {
  if (!state || typeof state !== "object") return {};
  const s = state as { viewing?: unknown; firstMessage?: unknown };
  const v = s.viewing as Partial<ViewingRef> | undefined;
  const viewing =
    v && typeof v === "object" && typeof v.repo === "string" && typeof v.number === "number" ? (v as ViewingRef) : undefined;
  const firstMessage = typeof s.firstMessage === "string" && s.firstMessage.trim() ? s.firstMessage : undefined;
  return { viewing, firstMessage };
}
