/**
 * GitHub client contract.
 *
 * Browser → api.github.com ONLY, with the user's browser-only token (github/token.ts). The portal
 * never proxies GitHub through the dashboard API, so the token never leaves the browser. Feature
 * modules (src/features/<area>/data.ts) build their own GraphQL/REST calls on the two generic
 * seams below; the client itself stays small and view-agnostic.
 */

import type { ReadOptions } from "./readCache";

export type { ReadMeta, ReadOptions } from "./readCache";

/** The message the mock client rejects feature reads with (shown as the "add a token" empty state). */
export const MOCK_UNAVAILABLE = "Not available in demo mode — add a GitHub token in Settings.";

/** The signed-in GitHub identity (resolved from the token). */
export interface GithubViewer {
  login: string;
  name: string;
}

export interface GitHubClient {
  /** Resolve the current user's identity (login + display name). */
  viewer(): Promise<GithubViewer>;
  /**
   * Run an arbitrary GraphQL query against api.github.com with the user's token. Throws on HTTP or
   * GraphQL errors. The mock client rejects with {@link MOCK_UNAVAILABLE} — features show their
   * "add a GitHub token" empty state instead. Query results are served from a 10-minute read cache
   * unless `opts.fresh` (see github/readCache.ts); any write through the client clears it.
   */
  graphql<T>(query: string, variables?: Record<string, unknown>, opts?: ReadOptions): Promise<T>;
  /**
   * Run an arbitrary REST call against api.github.com (path starts with `/`) and parse the JSON
   * body (`undefined` for 204). Same token + error semantics as {@link graphql}. `accept` may be
   * overridden via `headers` (e.g. `application/vnd.github.diff` + `text` for raw diffs). GETs go
   * through the same read cache as {@link graphql}; other methods clear it.
   */
  rest<T>(
    method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE",
    path: string,
    body?: unknown,
    headers?: Record<string, string>,
    opts?: ReadOptions,
  ): Promise<T>;
  // --- Human-authored / human-decision writes (the user's own actions, under their identity) -----
  /** Assign users to an issue/PR (a human triage decision). */
  assign(repo: string, number: number, assignees: string[]): Promise<void>;
  /** Add labels to an issue/PR (a human triage decision). */
  addLabels(repo: string, number: number, labels: string[]): Promise<void>;
  /** Remove a label from an issue/PR. */
  removeLabel(repo: string, number: number, label: string): Promise<void>;
  /** Post a comment the human typed themselves (their words, under their identity). */
  postComment(repo: string, number: number, body: string): Promise<void>;
}
