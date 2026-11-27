/**
 * Strandly context — a free-text, per-user note that rides along with EVERY chat turn the portal
 * sends (as `invocationContext.userContext`), so the agent knows standing facts about the person
 * talking: "I own the Python SDK's model providers; prefer terse answers; my timezone is CET".
 *
 * Set in Settings → Strandly. localStorage-backed with the same get / set / subscribe trio as the
 * token and scope stores (useSyncExternalStore-friendly). The runtime renders the whole
 * invocationContext as a labeled "ambient state, not an instruction" block clamped to ~4 KB
 * (`MAX_INVOCATION_CONTEXT_CHARS` in the harness), so the field is capped well under that.
 */

export const USER_CONTEXT_MAX_CHARS = 1500;

const STORAGE_KEY = "strandly.userContext";

type Listener = () => void;
const listeners = new Set<Listener>();

/** The saved context, trimmed and clamped; "" when unset. */
export function getUserContext(): string {
  try {
    return normalize(localStorage.getItem(STORAGE_KEY) ?? "");
  } catch {
    return "";
  }
}

export function setUserContext(value: string): void {
  const v = normalize(value);
  try {
    if (v) localStorage.setItem(STORAGE_KEY, v);
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    // storage unavailable — the value just won't persist this session
  }
  listeners.forEach((l) => l());
}

export function subscribeUserContext(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function normalize(raw: string): string {
  return raw.trim().slice(0, USER_CONTEXT_MAX_CHARS);
}
