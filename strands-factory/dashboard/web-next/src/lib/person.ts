/**
 * "Who are you?" — the GitHub login this browser says it belongs to. Every dashboard-API request
 * carries it as `X-Strandly-Person: github:<login>` (src/api/client.ts), the same header the CLI
 * sends. The API takes the person from the people-directory row bound to the signed-in Cognito
 * user, never from this header: a login that is not the bound person's is ignored.
 *
 * Advisory, not authentication: it never selects a person. "Use my GitHub login" fills it from the
 * GitHub token's viewer, checked in the browser; the token itself never reaches the dashboard API.
 *
 * Set in Settings → Who are you. localStorage-backed with the same get / set / subscribe trio as
 * the other settings stores (useSyncExternalStore-friendly).
 */

export const PERSON_HEADER = "X-Strandly-Person";

const STORAGE_KEY = "strandly.person.github";
// GitHub's login rule (mirrors dashboard/api/handler.py and ops/people.py): alphanumerics and
// single inner hyphens, at most 39 characters.
const LOGIN_RE = /^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$/;

type Listener = () => void;
const listeners = new Set<Listener>();

/** `octocat` / `@octocat` → `octocat`; anything that is not a GitHub login → "". */
export function normalizeLogin(raw: string): string {
  const login = raw.trim().replace(/^@/, "");
  return LOGIN_RE.test(login) ? login : "";
}

/** The saved login, or "" when unset. */
export function getPersonLogin(): string {
  try {
    return normalizeLogin(localStorage.getItem(STORAGE_KEY) ?? "");
  } catch {
    return "";
  }
}

/** Save a login ("" or an invalid value clears it); returns what was stored. */
export function setPersonLogin(raw: string): string {
  const login = normalizeLogin(raw);
  try {
    if (login) localStorage.setItem(STORAGE_KEY, login);
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    // storage unavailable: the value just won't persist this session
  }
  listeners.forEach((l) => l());
  return login;
}

export function subscribePersonLogin(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** The header to send with a dashboard-API request: `{}` when no login is set. */
export function personHeaders(): Record<string, string> {
  const login = getPersonLogin();
  return login ? { [PERSON_HEADER]: `github:${login}` } : {};
}
