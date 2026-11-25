/**
 * Dashboard sign-in state (Cognito), exposed to React.
 *
 * Wraps the framework-free engine in src/auth/cognito.ts. On mount it awaits initCognitoSession()
 * once — which handles a redirect ?code= exchange (or adopts a stored token) BEFORE any child
 * renders — so no API-backed view fires a request mid-exchange. When Cognito is unconfigured
 * (local dev / mock) it is a pass-through: `enabled` is false, nothing gates, no redirect.
 *
 * This is the DASHBOARD-API identity only. The GitHub identity (useUser / useGitHub) is separate
 * and unrelated — the two token stores never mix.
 */
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { Center, Loader } from "@mantine/core";
import {
  getIdentity,
  initCognitoSession,
  isAuthenticated,
  isCognitoEnabled,
  login,
  logout,
  subscribeCognito,
  type Identity,
} from "./cognito";

interface AuthCtx {
  /** True when Cognito auth is configured for this deployment (else the login-free local/mock path). */
  enabled: boolean;
  /** True when a live access token is held. Always false when auth is disabled. */
  authenticated: boolean;
  /** The signed-in identity (from the id_token), or null. */
  identity: Identity | null;
  /** Begin the Hosted-UI redirect sign-in. No-op when disabled. */
  login: () => void;
  /** Clear tokens and redirect through the Hosted-UI logout. No-op when disabled. */
  logout: () => void;
}

const Ctx = createContext<AuthCtx | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const enabled = isCognitoEnabled();
  // When disabled, we are ready immediately — no gating, no async boot.
  const [ready, setReady] = useState(!enabled);
  const [authenticated, setAuthenticated] = useState(false);
  const [identity, setIdentity] = useState<Identity | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let live = true;
    const sync = () => {
      if (!live) return;
      setAuthenticated(isAuthenticated());
      setIdentity(getIdentity());
    };
    initCognitoSession()
      // A refused callback (no stash, state mismatch, failed exchange) leaves the user signed out.
      .catch((err: unknown) => console.warn("Portal sign-in callback rejected:", err))
      .finally(() => {
        sync();
        if (live) setReady(true);
      });
    // Refresh/logout elsewhere (silent refresh timer, expiry) keeps the header state honest.
    const unsub = subscribeCognito(sync);
    return () => {
      live = false;
      unsub();
    };
  }, [enabled]);

  // Gate the app while the redirect exchange / token bootstrap runs (enabled only).
  if (!ready) {
    return (
      <Center h="100vh">
        <Loader />
      </Center>
    );
  }

  const value: AuthCtx = { enabled, authenticated, identity, login, logout };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
