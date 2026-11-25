/**
 * Live GitHub client — talks browser → https://api.github.com ONLY, authenticated with the user's
 * browser-only token.
 *
 * This module is the ONLY place the GitHub token is read for a network call, and every request
 * target is https://api.github.com. It never imports the dashboard API client, so the token has no
 * path to a dashboard-API request. Feature queries live in src/features/<area>/data.ts on top of
 * the generic `graphql` / `rest` seams; this file only holds identity + the human writes.
 *
 * Reads (GraphQL queries, REST GETs) go through a per-client 10-minute cache (readCache.ts) so
 * switching tabs doesn't refetch; `opts.fresh` bypasses it and every write clears it.
 */
import { graphqlKey, isCacheableGraphql, ReadCache, restKey } from "./readCache";
import type { GitHubClient, GithubViewer, ReadOptions } from "./types";

const GITHUB_API = "https://api.github.com";

/**
 * Deployment-configured orgs the portal scans (empty until configured). Only the
 * DEFAULT — the live orgs come from the per-user scope store (lib/scope.ts), which falls back to
 * this; every feature search adds `org:` clauses from `getScope().orgs`.
 */
export { GITHUB_ORGS } from "../lib/team";

/**
 * Path-segment-encode an `owner/name` repo + issue/PR number. The host is always the fixed
 * GITHUB_API constant, so a malformed `repo` can never redirect the token off api.github.com — but
 * a stray `?`/`#`/`..` in a server-supplied `repo` could otherwise hit an unintended endpoint under
 * the user's token. Encoding each segment keeps every REST call pinned to the exact resource.
 */
function repoIssuePath(repo: string, number: number): string {
  const encRepo = repo.split("/").map(encodeURIComponent).join("/");
  return `/repos/${encRepo}/issues/${encodeURIComponent(String(number))}`;
}

export class LiveGitHubClient implements GitHubClient {
  /** @param token the browser-only GitHub token (see github/token.ts). */
  constructor(
    private readonly token: string,
    private readonly cache: ReadCache = new ReadCache(),
  ) {}

  private headers(extra?: Record<string, string>): Record<string, string> {
    return {
      authorization: `Bearer ${this.token}`,
      accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
      ...extra,
    };
  }

  async graphql<T>(query: string, variables: Record<string, unknown> = {}, opts: ReadOptions = {}): Promise<T> {
    if (!isCacheableGraphql(query)) {
      const data = await this.graphqlRaw<T>(query, variables);
      this.cache.clear();
      return data;
    }
    return this.cache.read(graphqlKey(query, variables, opts.vary), () => this.graphqlRaw<T>(query, variables), opts);
  }

  private async graphqlRaw<T>(query: string, variables: Record<string, unknown>): Promise<T> {
    const r = await fetch(`${GITHUB_API}/graphql`, {
      method: "POST",
      headers: this.headers({ "content-type": "application/json" }),
      body: JSON.stringify({ query, variables }),
    });
    if (!r.ok) {
      throw new Error(`GitHub GraphQL ${r.status}: ${(await r.text().catch(() => "")).slice(0, 200)}`);
    }
    const j = (await r.json()) as { data?: T; errors?: { message: string }[] };
    if (j.errors?.length) throw new Error(`GitHub GraphQL: ${j.errors.map((e) => e.message).join("; ")}`);
    return j.data as T;
  }

  /** Generic REST call (see {@link GitHubClient.rest}): JSON body in/out, `undefined` on 204. */
  async rest<T>(
    method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE",
    path: string,
    body?: unknown,
    headers?: Record<string, string>,
    opts: ReadOptions = {},
  ): Promise<T> {
    if (method !== "GET") return this.restJson<T>(method, path, body, headers);
    return this.cache.read(restKey(path, headers, opts.vary), () => this.restJson<T>(method, path, body, headers), opts);
  }

  private async restJson<T>(
    method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE",
    path: string,
    body?: unknown,
    headers?: Record<string, string>,
  ): Promise<T> {
    const r = await this.restRaw(path, {
      method,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      ...(headers ? { headers } : {}),
    });
    if (r.status === 204) return undefined as T;
    const accept = headers?.accept ?? "";
    if (accept && !accept.includes("json")) return (await r.text()) as unknown as T;
    return (await r.json()) as T;
  }

  /**
   * REST helper — throws with GitHub's own error text so rejected calls surface clearly. Anything
   * but a GET is a write: the read cache is cleared once it succeeds.
   */
  private async restRaw(path: string, init: RequestInit): Promise<Response> {
    const r = await fetch(`${GITHUB_API}${path}`, {
      ...init,
      headers: this.headers({ "content-type": "application/json", ...(init.headers as object) }),
    });
    if (r.ok && init.method !== "GET") this.cache.clear();
    if (!r.ok) {
      let msg = "";
      try {
        const body = (await r.json()) as { message?: string };
        msg = body.message ?? "";
      } catch {
        msg = (await r.text().catch(() => "")).slice(0, 200);
      }
      throw new Error(`GitHub ${r.status}${msg ? `: ${msg}` : ""}`);
    }
    return r;
  }

  /** Token validation — once per client and live by design, so it skips the read cache. */
  async viewer(): Promise<GithubViewer> {
    const d = await this.graphqlRaw<{ viewer: { login: string; name: string | null } }>(
      `query { viewer { login name } }`,
      {},
    );
    return { login: d.viewer.login, name: d.viewer.name || d.viewer.login };
  }

  async assign(repo: string, number: number, assignees: string[]): Promise<void> {
    if (assignees.length === 0) return;
    await this.restRaw(`${repoIssuePath(repo, number)}/assignees`, {
      method: "POST",
      body: JSON.stringify({ assignees }),
    });
  }

  async addLabels(repo: string, number: number, labels: string[]): Promise<void> {
    if (labels.length === 0) return;
    await this.restRaw(`${repoIssuePath(repo, number)}/labels`, {
      method: "POST",
      body: JSON.stringify({ labels }),
    });
  }

  async removeLabel(repo: string, number: number, label: string): Promise<void> {
    await this.restRaw(`${repoIssuePath(repo, number)}/labels/${encodeURIComponent(label)}`, {
      method: "DELETE",
    });
  }

  async postComment(repo: string, number: number, body: string): Promise<void> {
    await this.restRaw(`${repoIssuePath(repo, number)}/comments`, {
      method: "POST",
      body: JSON.stringify({ body }),
    });
  }
}
