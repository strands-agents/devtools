/**
 * Session titles: a preview of the server's normalization (`clean_session_title` in
 * strandly/core/session_ids.py, which stays authoritative), the rename editor's key map,
 * and the optimistic commit.
 */

/** Mirrors strandly.core.session_ids.SESSION_TITLE_MAX_LEN; the API rejects longer titles. */
export const SESSION_TITLE_MAX_LEN = 80;

// Control (Cc) and format (Cf: zero-width, bidi overrides) characters never reach a title.
const INVISIBLE = /[\p{Cc}\p{Cf}]/gu;

/** One display line: whitespace runs collapse to a space, invisible characters are dropped. */
export function cleanTitle(raw: string): string {
  return raw.replace(/\s+/g, " ").replace(INVISIBLE, "").replace(/ {2,}/g, " ").trim();
}

/** What a key does in the rename box: Enter saves, Escape cancels, anything else types. */
export function titleEditKey(key: string, composing = false): "save" | "cancel" | null {
  if (composing) return null;
  if (key === "Enter") return "save";
  if (key === "Escape") return "cancel";
  return null;
}

/**
 * Optimistic rename: show `next` at once, then settle on what the server stored — or put
 * `previous` back and rethrow when the save fails. A no-op edit never calls the server.
 */
export async function commitTitle(opts: {
  previous: string | null;
  next: string;
  apply: (title: string | null) => void;
  save: (title: string) => Promise<string | null>;
}): Promise<string | null> {
  const next = cleanTitle(opts.next);
  if (next === (opts.previous ?? "")) return opts.previous;
  opts.apply(next || null);
  try {
    const stored = await opts.save(next);
    opts.apply(stored);
    return stored;
  } catch (e) {
    opts.apply(opts.previous);
    throw e;
  }
}
