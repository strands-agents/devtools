/**
 * Browser-only GitHub token store.
 *
 * HARD RULE (non-negotiable): this token lives in the browser (localStorage) ONLY. It is used
 * exclusively for direct browser → https://api.github.com calls (see src/github/client.ts). It is
 * NEVER attached to any request to the dashboard API. The boundary is structural: src/api/client.ts
 * does not — and must not — import this module. The dashboard's own bearer seam is a separate thing
 * (src/auth/dashboardToken.ts), reserved for the Cognito/JWT flow.
 *
 * localStorage is readable by any script running on this origin, so a successful XSS on the portal
 * could exfiltrate this token. Mitigations the settings panel spells out for the user: use a
 * fine-grained PAT for a single resource owner and only the selected repositories you need. Grant
 * Pull requests and Issues read/write for reviews and triage, Metadata read, and organization
 * Members read only for team lookup. A fine-grained PAT cannot span multiple resource owners;
 * match the portal's configured scope to its access. Clear it when done on a shared machine.
 */
const STORAGE_KEY = "strandly.github_token";

type Listener = () => void;
const listeners = new Set<Listener>();

/** The current token, or null if none is saved. */
export function getGithubToken(): string | null {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v && v.trim() ? v : null;
  } catch {
    return null;
  }
}

/** Save (trimmed) or, with null/empty, clear the token. Notifies subscribers either way. */
export function setGithubToken(token: string | null): void {
  try {
    if (token && token.trim()) localStorage.setItem(STORAGE_KEY, token.trim());
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    // localStorage unavailable (private mode / blocked) — the token just won't persist this session.
  }
  listeners.forEach((l) => l());
}

export function clearGithubToken(): void {
  setGithubToken(null);
}

/** Subscribe to save/clear changes. Returns an unsubscribe fn. */
export function subscribeGithubToken(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
