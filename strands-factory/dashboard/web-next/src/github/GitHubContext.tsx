/**
 * GitHub context — the browser-only identity + client seam.
 *
 * A real token ALWAYS wins: GitHub data comes from the user's browser token whenever one is saved,
 * regardless of the (work-product) data source — so "add a token → real GitHub data" holds even
 * while work products are still mock/local. MockGitHubClient is only the no-token fallback: on the
 * `mock` data source with no token it powers the demo UI; on `live` with no token the client is null
 * (unauthenticated, read-only). Resolves the viewer identity from the token and exposes it plus
 * token save/clear. The token itself never leaves this subtree — no consumer needs the raw string.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { useDataSource } from "../api/ApiContext";
import { LiveGitHubClient } from "./client";
import { MockGitHubClient } from "./mock";
import { getGithubToken, setGithubToken, subscribeGithubToken } from "./token";
import type { GitHubClient, GithubViewer } from "./types";

interface GitHubCtx {
  /** The client for reads/writes, or null when live with no token (unauthenticated). */
  client: GitHubClient | null;
  /** The resolved identity, or null when unauthenticated / still validating / invalid. */
  viewer: GithubViewer | null;
  /** True when a client is available (mock always; live only with a token) — the write gate. */
  hasToken: boolean;
  /** True when running on mock data (token optional). */
  isMock: boolean;
  /** A token is saved but its identity is still being validated. */
  validating: boolean;
  /** The last validation error (bad/expired token), else null. */
  tokenError: string | null;
  /** Save + persist a token (validation happens via the viewer effect). */
  saveToken: (token: string) => void;
  /** Clear the saved token (back to unauthenticated). */
  clearToken: () => void;
}

const Ctx = createContext<GitHubCtx | null>(null);

export function GitHubProvider({ children }: { children: ReactNode }) {
  const dataSource = useDataSource();
  const dataSourceIsMock = dataSource === "mock";

  // Reflect the localStorage token reactively (survives across the app + settings panel).
  const token = useSyncExternalStore(subscribeGithubToken, getGithubToken, () => null);
  // Scope (orgs) is NOT part of the client: features read `getScope()` themselves (reactively, via
  // `subscribeScope`) when they build their searches, so a scope change refetches without
  // rebuilding the client or re-validating the token.
  const client = useMemo<GitHubClient | null>(() => {
    if (token) return new LiveGitHubClient(token); // real GitHub whenever a token is present
    if (dataSourceIsMock) return new MockGitHubClient(); // demo identity only as the no-token fallback
    return null; // live data source, no token → unauthenticated, read-only
  }, [dataSourceIsMock, token]);

  // GitHub data is mock only when there is no token AND we're on the mock data source.
  const isMock = !token && dataSourceIsMock;

  const [viewer, setViewer] = useState<GithubViewer | null>(null);
  const [validating, setValidating] = useState(false);
  const [tokenError, setTokenError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    if (!client) {
      setViewer(null);
      setTokenError(null);
      setValidating(false);
      return;
    }
    setValidating(true);
    setTokenError(null);
    client
      .viewer()
      .then((v) => {
        if (!live) return;
        setViewer(v);
        setTokenError(null);
      })
      .catch((e) => {
        if (!live) return;
        setViewer(null);
        setTokenError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => live && setValidating(false));
    return () => {
      live = false;
    };
  }, [client]);

  const saveToken = useCallback((t: string) => setGithubToken(t), []);
  const clearToken = useCallback(() => setGithubToken(null), []);

  const value = useMemo<GitHubCtx>(
    () => ({
      client,
      viewer,
      hasToken: client !== null,
      isMock,
      validating,
      tokenError,
      saveToken,
      clearToken,
    }),
    [client, viewer, isMock, validating, tokenError, saveToken, clearToken],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useGitHub(): GitHubCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useGitHub must be used within GitHubProvider");
  return ctx;
}
