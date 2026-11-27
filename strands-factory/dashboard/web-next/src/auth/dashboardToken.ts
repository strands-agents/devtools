/**
 * Dashboard API bearer seam (Cognito/JWT) — NOT the GitHub token.
 *
 * This is the single place a bearer is attached to *dashboard API* requests (src/api/client.ts).
 * The real Cognito PKCE flow lives in src/auth/cognito.ts; this module is the thin, framework-free seam client.ts reads. When Cognito is unconfigured
 * (local dev / mock) getDashboardToken() returns null and isDashboardAuthEnabled() is false, so no
 * Authorization header is sent and the local loop stays login-free.
 *
 * IMPORTANT: the user's GitHub token is deliberately NOT here. It lives in src/github/token.ts and
 * is only ever used for browser → https://api.github.com calls. Keeping the two seams separate is
 * how we guarantee the GitHub token never rides on a dashboard-API request.
 */
import { getAccessToken, isCognitoEnabled } from "./cognito";

/** The live dashboard-API access token, or null when signed out / auth disabled. */
export function getDashboardToken(): string | null {
  return getAccessToken();
}

/**
 * True when Cognito auth is configured for this deployment. Callers use it to require sign-in
 * before hitting the API (rather than firing an unauthenticated request that would 401).
 */
export function isDashboardAuthEnabled(): boolean {
  return isCognitoEnabled();
}
