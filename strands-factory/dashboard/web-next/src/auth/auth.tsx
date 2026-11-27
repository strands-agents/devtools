/**
 * Identity, derived client-side from the user's browser GitHub token.
 *
 * The current user (login/name) comes from the GitHub `viewer` query (see GitHubContext), resolved
 * with the browser-only token. There is no server-side sign-in and the username is never persisted
 * server-side — it only ever travels as `curated_by` on a curate call and as `user` inside a chat
 * turn's `invocationContext` (user data, not the token). When no token is present (live,
 * unauthenticated) the app is read-only; `login` is empty and views gate their write actions on
 * `useGitHub().hasToken`.
 *
 * The last RESOLVED identity is cached in localStorage so it is available synchronously on the
 * next visit — the `viewer` query is async, and a chat turn sent in the first seconds after load
 * would otherwise go out anonymous. The cache is only ever read while a token is present (it
 * mirrors that token's viewer; an unauthenticated session must not claim a stale identity), and a
 * fresh resolution always overwrites it — so after a token swap it is wrong for at most the one
 * in-flight `viewer` round-trip.
 */
import { useEffect } from "react";
import { useGitHub } from "../github/GitHubContext";

export interface User {
  login: string;
  name: string;
}

const ANONYMOUS: User = { login: "", name: "Not signed in" };

/** localStorage key for the last resolved viewer identity (see module header for the contract). */
const VIEWER_CACHE_KEY = "portal.viewer.v1";

function readCachedUser(): User | null {
  try {
    const raw = localStorage.getItem(VIEWER_CACHE_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw) as { login?: unknown; name?: unknown };
    if (typeof v.login !== "string" || !v.login) return null;
    return { login: v.login, name: typeof v.name === "string" && v.name ? v.name : v.login };
  } catch {
    return null;
  }
}

/** The resolved GitHub identity, or an anonymous placeholder when unauthenticated. */
export function useUser(): User {
  const { viewer, hasToken } = useGitHub();

  // Persist each resolution so the identity is ready synchronously on the next load.
  useEffect(() => {
    if (!viewer) return;
    try {
      localStorage.setItem(VIEWER_CACHE_KEY, JSON.stringify({ login: viewer.login, name: viewer.name }));
    } catch {
      // Storage unavailable (private mode, quota) — identity still works, just not cached.
    }
  }, [viewer]);

  if (viewer) return { login: viewer.login, name: viewer.name };
  // Token present but viewer not (yet) resolved — bridge with the cached identity.
  if (hasToken) return readCachedUser() ?? ANONYMOUS;
  return ANONYMOUS;
}
