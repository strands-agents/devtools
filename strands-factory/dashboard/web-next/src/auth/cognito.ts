/**
 * Cognito Hosted-UI + Authorization-Code + PKCE engine (framework-agnostic).
 *
 * The flow:
 *   - build the Hosted-UI /oauth2/authorize URL with a PKCE S256 code_challenge and a random state,
 *   - handle the redirect callback (?code=…&state=…): refuse it unless this tab stashed a verifier
 *     and the same state, then exchange the code at /oauth2/token
 *     (public client — client_id only, NO secret),
 *   - persist access/id/refresh tokens + expiry in localStorage,
 *   - silently refresh ~1 min before the (default 1h) access-token expiry,
 *   - expose the live access token synchronously to the dashboard-API bearer seam.
 *
 * It is DORMANT until configureCognito() is handed a real { domain, clientId }. With no config
 * (local dev, or the mock data source) isCognitoEnabled() is false and
 * every entry point is a no-op — no bearer, no redirect — so `npm run dev` stays login-free.
 *
 * IMPORTANT: this is the DASHBOARD-API identity only. The user's GitHub token is a separate store
 * (src/github/token.ts) and must never flow through here — the two seams stay independent.
 */

export interface CognitoConfig {
  /** Hosted-UI domain, e.g. https://strandly-portal.auth.us-west-2.amazoncognito.com (no trailing slash). */
  domain: string;
  /** The public app-client id. */
  clientId: string;
  /** Registered redirect URI; falls back to `${origin}/` when empty. */
  redirectUri: string;
}

export interface Identity {
  email: string;
  username: string;
}

interface StoredTokens {
  access_token: string;
  id_token?: string;
  refresh_token?: string;
  /** Epoch ms at which access_token expires. */
  expires_at: number;
}

// localStorage/sessionStorage keys — namespaced so they never collide with the GitHub token store.
const LS = {
  verifier: "strandly.portal.pkce_verifier",
  state: "strandly.portal.oauth_state",
  tokens: "strandly.portal.tokens",
  route: "strandly.portal.return_route",
} as const;

// How long before expiry we proactively refresh.
const REFRESH_LEAD_MS = 60_000;

let config: CognitoConfig | null = null;
// The live access token, kept in a module var so the (synchronous) dashboard-API bearer seam
// (auth/dashboardToken.ts → api/client.ts) can read it without awaiting. Kept fresh by the silent
// refresh timer + the boot bootstrap.
let currentAccessToken: string | null = null;
let refreshTimer: ReturnType<typeof setTimeout> | null = null;

type Listener = () => void;
const listeners = new Set<Listener>();

function notify(): void {
  listeners.forEach((l) => l());
}

/** Subscribe to login/logout/refresh changes. Returns an unsubscribe fn. */
export function subscribeCognito(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Install (or clear) the resolved Cognito config. Called once at boot from initApi() after the
 * runtime config.json is loaded. Passing null (or a config missing domain/clientId) leaves auth
 * disabled — the login-free local/mock path.
 */
export function configureCognito(cfg: CognitoConfig | null): void {
  config = cfg && cfg.domain && cfg.clientId ? cfg : null;
}

/** True only when a usable Cognito config is present — the single gate for the whole flow. */
export function isCognitoEnabled(): boolean {
  return config !== null;
}

// ---------- PKCE helpers ----------

function randString(n = 64): string {
  const a = new Uint8Array(n);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => ("0" + (b & 0xff).toString(16)).slice(-2)).join("");
}

function b64url(buf: ArrayBuffer): string {
  return btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

async function challenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return b64url(digest);
}

function redirectUri(): string {
  return (config && config.redirectUri) || window.location.origin + "/";
}

// ---------- token store ----------

function loadStoredTokens(): StoredTokens | null {
  try {
    const raw = localStorage.getItem(LS.tokens);
    return raw ? (JSON.parse(raw) as StoredTokens) : null;
  } catch {
    return null;
  }
}

interface TokenResponse {
  access_token: string;
  id_token?: string;
  refresh_token?: string;
  expires_in?: number;
}

function storeTokens(data: TokenResponse, previous?: StoredTokens | null): StoredTokens {
  const tokens: StoredTokens = {
    access_token: data.access_token,
    id_token: data.id_token ?? previous?.id_token,
    // The refresh token is not re-issued on a refresh grant — keep the prior one.
    refresh_token: data.refresh_token ?? previous?.refresh_token,
    expires_at: Date.now() + (data.expires_in ?? 3600) * 1000,
  };
  try {
    localStorage.setItem(LS.tokens, JSON.stringify(tokens));
  } catch {
    // localStorage unavailable (private mode) — the token just won't survive a reload.
  }
  currentAccessToken = tokens.access_token;
  scheduleRefresh(tokens);
  notify();
  return tokens;
}

function clearTokens(): void {
  try {
    localStorage.removeItem(LS.tokens);
  } catch {
    /* ignore */
  }
  currentAccessToken = null;
  if (refreshTimer) {
    clearTimeout(refreshTimer);
    refreshTimer = null;
  }
  notify();
}

function scheduleRefresh(tokens: StoredTokens): void {
  if (refreshTimer) clearTimeout(refreshTimer);
  refreshTimer = null;
  if (!tokens.refresh_token) return; // nothing to refresh with — let it expire
  const delay = Math.max(tokens.expires_at - Date.now() - REFRESH_LEAD_MS, 0);
  refreshTimer = setTimeout(() => {
    void refreshTokens();
  }, delay);
}

// ---------- OAuth calls ----------

async function exchangeCode(code: string, returnedState: string | null): Promise<StoredTokens> {
  if (!config) throw new Error("Cognito is not configured");
  const verifier = sessionStorage.getItem(LS.verifier);
  const expectedState = sessionStorage.getItem(LS.state);
  // Single use: a replayed or forged redirect must never find this attempt's verifier and state.
  sessionStorage.removeItem(LS.verifier);
  sessionStorage.removeItem(LS.state);
  // A ?code= this tab did not ask for (no stash, or another sign-in's state) is a login-fixation
  // attempt or a stale link; never exchange it.
  if (!verifier || !expectedState) throw new Error("no sign-in in progress; sign in again");
  if (returnedState !== expectedState) throw new Error("sign-in state mismatch; sign in again");
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: config.clientId,
    code,
    redirect_uri: redirectUri(),
    code_verifier: verifier,
  });
  const r = await fetch(config.domain + "/oauth2/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!r.ok) throw new Error("token exchange failed (" + r.status + ")");
  return storeTokens((await r.json()) as TokenResponse);
}

async function refreshTokens(): Promise<StoredTokens | null> {
  if (!config) return null;
  const prev = loadStoredTokens();
  if (!prev || !prev.refresh_token) return null;
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    client_id: config.clientId,
    refresh_token: prev.refresh_token,
  });
  try {
    const r = await fetch(config.domain + "/oauth2/token", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body,
    });
    if (!r.ok) {
      // A refused refresh means the session is dead — drop it so the UI shows signed-out.
      clearTokens();
      return null;
    }
    return storeTokens((await r.json()) as TokenResponse, prev);
  } catch {
    return null; // transient network error — keep the (soon-stale) token, the next call retries
  }
}

// ---------- public API ----------

/** Start the Hosted-UI sign-in: stash a PKCE verifier, an OAuth state + the current route, then redirect. */
export async function login(): Promise<void> {
  if (!config) return;
  const verifier = randString(64);
  const state = randString(32);
  sessionStorage.setItem(LS.verifier, verifier);
  sessionStorage.setItem(LS.state, state);
  // The hash route never reaches the server and Cognito redirects to the bare redirect_uri, so a
  // deep link (#/review/…) would be lost. Stash it; the callback restores it after the exchange.
  if (window.location.hash) sessionStorage.setItem(LS.route, window.location.hash);
  else sessionStorage.removeItem(LS.route); // a stale stash from an aborted flow must not hijack this one
  const url = new URL(config.domain + "/oauth2/authorize");
  url.search = new URLSearchParams({
    response_type: "code",
    client_id: config.clientId,
    redirect_uri: redirectUri(),
    scope: "openid email profile",
    code_challenge_method: "S256",
    code_challenge: await challenge(verifier),
    state,
  }).toString();
  window.location.assign(url.toString());
}

/** Revoke the refresh token (best effort), clear tokens and redirect through the Hosted-UI logout endpoint. */
export function logout(): void {
  if (!config) return;
  const refreshToken = loadStoredTokens()?.refresh_token;
  if (refreshToken) {
    // keepalive lets the revoke finish while the page navigates to the logout endpoint.
    void fetch(config.domain + "/oauth2/revoke", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ token: refreshToken, client_id: config.clientId }),
      keepalive: true,
    }).catch(() => undefined);
  }
  clearTokens();
  const url = new URL(config.domain + "/logout");
  url.search = new URLSearchParams({
    client_id: config.clientId,
    logout_uri: redirectUri(),
  }).toString();
  window.location.assign(url.toString());
}

/**
 * Boot the session: exchange a redirect ?code= if present, otherwise adopt any stored token
 * (refreshing it when it is at/near expiry). Awaited once by AuthProvider before the app renders,
 * so no API-backed view mounts (or fires a request) mid-exchange. No-op when auth is disabled.
 */
export async function initCognitoSession(): Promise<void> {
  if (!config) return;
  const params = new URLSearchParams(window.location.search);
  const code = params.get("code");
  let rejected: unknown = null;
  if (code || params.has("error")) {
    try {
      if (code) {
        await exchangeCode(code, params.get("state"));
        return;
      }
      // Hosted UI sent back ?error= (cancelled or refused sign-in): end this attempt too.
      sessionStorage.removeItem(LS.verifier);
      sessionStorage.removeItem(LS.state);
      rejected = new Error("sign-in failed: " + params.get("error"));
    } catch (err) {
      rejected = err;
    } finally {
      // Strip ?code=/?error= and restore any deep link stashed before the redirect (see login).
      const returnRoute = sessionStorage.getItem(LS.route) ?? "";
      sessionStorage.removeItem(LS.route);
      window.history.replaceState({}, "", redirectUri() + returnRoute);
    }
  }
  // A refused callback (e.g. a planted ?code= link) must not sign out a user who already holds a
  // session: adopt the stored tokens, then report the rejection.
  await adoptStoredSession();
  if (rejected) throw rejected;
}

async function adoptStoredSession(): Promise<void> {
  const stored = loadStoredTokens();
  if (!stored) return;
  if (Date.now() > stored.expires_at - REFRESH_LEAD_MS) {
    await refreshTokens();
  } else {
    currentAccessToken = stored.access_token;
    scheduleRefresh(stored);
  }
}

/** The live access token for the dashboard-API bearer seam, or null when signed out/disabled. */
export function getAccessToken(): string | null {
  return currentAccessToken;
}

/** True when a live access token is held (i.e. signed in). */
export function isAuthenticated(): boolean {
  return currentAccessToken !== null;
}

/** The signed-in identity from the id_token claims, or null when signed out. */
export function getIdentity(): Identity | null {
  const stored = loadStoredTokens();
  if (!stored || !stored.id_token || !currentAccessToken) return null;
  const claims = decodeJwtClaims(stored.id_token);
  if (!claims) return null;
  const email = typeof claims.email === "string" ? claims.email : "";
  const username =
    (typeof claims["cognito:username"] === "string" && claims["cognito:username"]) ||
    (typeof claims.username === "string" && claims.username) ||
    email;
  return { email, username };
}

function decodeJwtClaims(jwt: string): Record<string, unknown> | null {
  try {
    const payload = jwt.split(".")[1];
    if (!payload) return null;
    const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(json) as Record<string, unknown>;
  } catch {
    return null;
  }
}
