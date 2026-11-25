import { describe, expect, it } from "vitest";
import { graphqlKey, isCacheableGraphql, ReadCache, restKey, stableStringify } from "./readCache";

function clock(start = 1_000_000) {
  let t = start;
  return { now: () => t, tick: (ms: number) => (t += ms) };
}

function counter<T>(value: () => T) {
  let calls = 0;
  const fn = async () => {
    calls++;
    return value();
  };
  return { fn, calls: () => calls };
}

describe("ReadCache", () => {
  it("returns the cached value within the TTL without calling the fetcher again", async () => {
    const c = clock();
    const cache = new ReadCache(10_000, 100, c.now);
    const f = counter(() => "a");
    expect(await cache.read("k", f.fn)).toBe("a");
    c.tick(9_999);
    expect(await cache.read("k", f.fn)).toBe("a");
    expect(f.calls()).toBe(1);
  });

  it("refetches once the TTL has elapsed", async () => {
    const c = clock();
    const cache = new ReadCache(10_000, 100, c.now);
    let v = "a";
    const f = counter(() => v);
    await cache.read("k", f.fn);
    v = "b";
    c.tick(10_000);
    expect(await cache.read("k", f.fn)).toBe("b");
    expect(f.calls()).toBe(2);
  });

  it("fresh bypasses the cache and replaces the copy", async () => {
    const c = clock();
    const cache = new ReadCache(10_000, 100, c.now);
    let v = "a";
    const f = counter(() => v);
    await cache.read("k", f.fn);
    v = "b";
    expect(await cache.read("k", f.fn, { fresh: true })).toBe("b");
    expect(await cache.read("k", f.fn)).toBe("b");
    expect(f.calls()).toBe(2);
  });

  it("reports the original fetch time on a hit", async () => {
    const c = clock(5_000);
    const cache = new ReadCache(10_000, 100, c.now);
    const metas: { fetchedAt: number; fromCache: boolean }[] = [];
    const onMeta = (m: { fetchedAt: number; fromCache: boolean }) => metas.push(m);
    await cache.read("k", async () => 1, { onMeta });
    c.tick(3_000);
    await cache.read("k", async () => 1, { onMeta });
    expect(metas).toEqual([
      { fetchedAt: 5_000, fromCache: false },
      { fetchedAt: 5_000, fromCache: true },
    ]);
  });

  it("shares one in-flight request between concurrent readers of a key", async () => {
    const cache = new ReadCache(10_000, 100, clock().now);
    let resolve!: (v: string) => void;
    const f = counter(() => new Promise<string>((r) => (resolve = r)));
    const a = cache.read("k", () => f.fn().then((p) => p));
    const b = cache.read("k", () => f.fn().then((p) => p));
    resolve("x");
    expect(await Promise.all([a, b])).toEqual(["x", "x"]);
    expect(f.calls()).toBe(1);
  });

  it("clear() forgets entries and drops the result of a fetch that was in flight", async () => {
    const cache = new ReadCache(10_000, 100, clock().now);
    await cache.read("k", async () => "a");
    expect(cache.size).toBe(1);
    let resolve!: (v: string) => void;
    const slow = cache.read("slow", () => new Promise<string>((r) => (resolve = r)));
    cache.clear();
    expect(cache.size).toBe(0);
    resolve("late");
    expect(await slow).toBe("late"); // the caller still gets its value…
    expect(cache.size).toBe(0); // …but it is not kept
  });

  it("a failed fetch is not cached and the next read retries", async () => {
    const cache = new ReadCache(10_000, 100, clock().now);
    let fail = true;
    const f = counter(() => {
      if (fail) throw new Error("boom");
      return "ok";
    });
    await expect(cache.read("k", f.fn)).rejects.toThrow("boom");
    fail = false;
    expect(await cache.read("k", f.fn)).toBe("ok");
    expect(f.calls()).toBe(2);
  });

  it("evicts the oldest entries past the cap", async () => {
    const c = clock();
    const cache = new ReadCache(10_000, 3, c.now);
    for (const k of ["a", "b", "c", "d"]) {
      await cache.read(k, async () => k);
      c.tick(1);
    }
    expect(cache.size).toBe(3);
    const f = counter(() => "a2");
    expect(await cache.read("a", f.fn)).toBe("a2"); // "a" was evicted
    expect(f.calls()).toBe(1);
  });

  it("values are cached per key", async () => {
    const cache = new ReadCache(10_000, 100, clock().now);
    expect(await cache.read("a", async () => 1)).toBe(1);
    expect(await cache.read("b", async () => 2)).toBe(2);
  });
});

describe("keys", () => {
  it("stableStringify ignores key order and undefined", () => {
    expect(stableStringify({ b: 1, a: [{ d: 2, c: null }], e: undefined })).toBe('{"a":[{"c":null,"d":2}],"b":1}');
  });

  it("graphqlKey differs by query and by variables", () => {
    const k1 = graphqlKey("query A { x }", { q: "is:open", cursor: null });
    expect(graphqlKey("query A { x }", { cursor: null, q: "is:open" })).toBe(k1);
    expect(graphqlKey("query A { x }", { q: "is:open", cursor: "abc" })).not.toBe(k1);
    expect(graphqlKey("query B { x }", { q: "is:open", cursor: null })).not.toBe(k1);
  });

  it("restKey includes the accept header (diff vs json bodies differ)", () => {
    const base = restKey("/repos/o/r/pulls/1/files?page=1");
    expect(restKey("/repos/o/r/pulls/1/files?page=1", {})).toBe(base);
    expect(restKey("/repos/o/r/pulls/1/files?page=1", { accept: "application/vnd.github.diff" })).not.toBe(base);
    expect(restKey("/repos/o/r/pulls/1/files?page=2")).not.toBe(base);
  });

  it("vary separates reads of one path/document whose answer depends on outside state", () => {
    const path = "/repos/o/r/pulls/1/files?page=1";
    expect(restKey(path, undefined, "sha-a")).not.toBe(restKey(path, undefined, "sha-b"));
    expect(restKey(path, undefined, "sha-a")).toBe(restKey(path, {}, "sha-a"));
    expect(restKey(path)).toBe(restKey(path, undefined, undefined));
    expect(graphqlKey("query A { x }", {}, "v1")).not.toBe(graphqlKey("query A { x }", {}, "v2"));
  });

  it("isCacheableGraphql rejects mutations only", () => {
    expect(isCacheableGraphql("query Today { viewer { login } }")).toBe(true);
    expect(isCacheableGraphql("{ viewer { login } }")).toBe(true);
    expect(isCacheableGraphql("  mutation Add { addComment(input: {}) { clientMutationId } }")).toBe(false);
  });
});
