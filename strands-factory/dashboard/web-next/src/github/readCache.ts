/**
 * Read-through TTL cache for GitHub reads (GraphQL queries + REST GETs), owned by one
 * LiveGitHubClient — so a token change (new client) starts with an empty cache.
 *
 * Within the TTL a repeat read returns the cached copy without touching the network — that is what
 * makes switching between tabs instant. `fresh: true` (the user's Refresh / a poll tick) bypasses
 * the cache and replaces the copy. Every write through the client clears the whole cache: the
 * user's own actions always show their effect. Concurrent reads of one key share one request.
 */

export const GITHUB_READ_TTL_MS = 10 * 60_000;
/** Entries kept at most (oldest evicted first); PR file pages can be large. */
export const READ_CACHE_MAX_ENTRIES = 100;

export interface ReadMeta {
  /** Wall-clock ms of the network fetch that produced the result (older than now on a cache hit). */
  fetchedAt: number;
  fromCache: boolean;
}

export interface ReadOptions {
  /** Bypass the cache and hit the network; the result replaces the cached copy. */
  fresh?: boolean;
  /**
   * Mixed into the cache key, for reads whose answer depends on state the request itself doesn't
   * name — PR file pages change with the head commit while their path stays the same.
   */
  vary?: string;
  /** Receives where the result came from and when it was fetched. */
  onMeta?: (meta: ReadMeta) => void;
}

interface Entry<T> {
  value: T;
  at: number;
}

export class ReadCache {
  private readonly entries = new Map<string, Entry<unknown>>();
  private readonly pending = new Map<string, Promise<Entry<unknown>>>();
  /** Bumped by clear(): a fetch started before a clear must not store its (possibly stale) result. */
  private generation = 0;

  constructor(
    private readonly ttlMs = GITHUB_READ_TTL_MS,
    private readonly maxEntries = READ_CACHE_MAX_ENTRIES,
    private readonly now: () => number = Date.now,
  ) {}

  async read<T>(key: string, fetcher: () => Promise<T>, opts: ReadOptions = {}): Promise<T> {
    if (!opts.fresh) {
      const hit = this.entries.get(key) as Entry<T> | undefined;
      if (hit && this.now() - hit.at < this.ttlMs) {
        opts.onMeta?.({ fetchedAt: hit.at, fromCache: true });
        return hit.value;
      }
      const inflight = this.pending.get(key) as Promise<Entry<T>> | undefined;
      if (inflight) {
        const e = await inflight;
        opts.onMeta?.({ fetchedAt: e.at, fromCache: false });
        return e.value;
      }
    }
    const gen = this.generation;
    const p: Promise<Entry<T>> = fetcher()
      .then((value) => {
        const entry = { value, at: this.now() };
        if (gen === this.generation) this.store(key, entry);
        return entry;
      })
      .finally(() => {
        if (this.pending.get(key) === p) this.pending.delete(key);
      });
    this.pending.set(key, p);
    const e = await p;
    opts.onMeta?.({ fetchedAt: e.at, fromCache: false });
    return e.value;
  }

  /** Forget everything (after a write). In-flight reads still resolve but are not stored. */
  clear(): void {
    this.entries.clear();
    this.pending.clear();
    this.generation++;
  }

  get size(): number {
    return this.entries.size;
  }

  private store(key: string, entry: Entry<unknown>): void {
    this.entries.delete(key);
    this.entries.set(key, entry);
    if (this.entries.size <= this.maxEntries) return;
    const now = this.now();
    for (const [k, e] of this.entries) {
      if (now - e.at >= this.ttlMs) this.entries.delete(k);
    }
    for (const k of this.entries.keys()) {
      if (this.entries.size <= this.maxEntries) break;
      this.entries.delete(k);
    }
  }
}

/** JSON with object keys sorted at every level, so `{a,b}` and `{b,a}` variables share a key. */
export function stableStringify(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  if (value && typeof value === "object") {
    const keys = Object.keys(value as Record<string, unknown>)
      .filter((k) => (value as Record<string, unknown>)[k] !== undefined)
      .sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${stableStringify((value as Record<string, unknown>)[k])}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

export function graphqlKey(query: string, variables: Record<string, unknown>, vary?: string): string {
  return `graphql\n${query}\n${stableStringify(variables)}\n${vary ?? ""}`;
}

export function restKey(path: string, headers?: Record<string, string>, vary?: string): string {
  return `rest GET ${path}\n${stableStringify(headers ?? {})}\n${vary ?? ""}`;
}

/** Only query documents are cached; a mutation (none today) goes straight through. */
export function isCacheableGraphql(query: string): boolean {
  return !/^\s*mutation\b/i.test(query);
}
