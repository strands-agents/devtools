import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LiveGitHubClient } from "./client";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

describe("LiveGitHubClient read cache", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).endsWith("/graphql")) return jsonResponse({ data: { n: fetchMock.mock.calls.length } });
      if (init?.method === "POST" || init?.method === "PATCH") return jsonResponse({ ok: true }, 201);
      return jsonResponse([{ page: fetchMock.mock.calls.length }]);
    });
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("serves a repeated GraphQL query from the cache and refetches with fresh", async () => {
    const c = new LiveGitHubClient("t");
    const a = await c.graphql<{ n: number }>("query A { x }", { q: "one" });
    const b = await c.graphql<{ n: number }>("query A { x }", { q: "one" });
    expect(b).toEqual(a);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await c.graphql("query A { x }", { q: "two" }); // different variables → different key
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const metas: boolean[] = [];
    await c.graphql("query A { x }", { q: "one" }, { fresh: true, onMeta: (m) => metas.push(m.fromCache) });
    await c.graphql("query A { x }", { q: "one" }, { onMeta: (m) => metas.push(m.fromCache) });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(metas).toEqual([false, true]);
  });

  it("caches REST GETs by path and clears everything after a write", async () => {
    const c = new LiveGitHubClient("t");
    await c.rest("GET", "/repos/o/r/pulls/1/files?page=1");
    await c.rest("GET", "/repos/o/r/pulls/1/files?page=1");
    await c.graphql("query A { x }");
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await c.rest("POST", "/repos/o/r/pulls/1/reviews", { event: "APPROVE" });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await c.rest("GET", "/repos/o/r/pulls/1/files?page=1");
    await c.graphql("query A { x }");
    expect(fetchMock).toHaveBeenCalledTimes(5); // both re-fetched

    await c.postComment("o/r", 1, "hi"); // the human writes use the same seam
    await c.graphql("query A { x }");
    expect(fetchMock).toHaveBeenCalledTimes(7);
  });

  it("a GET with a different vary is a different entry (PR files under a new head)", async () => {
    const c = new LiveGitHubClient("t");
    const path = "/repos/o/r/pulls/1/files?page=1";
    const a = await c.rest<{ page: number }[]>("GET", path, undefined, undefined, { vary: "sha-a" });
    expect(await c.rest("GET", path, undefined, undefined, { vary: "sha-a" })).toEqual(a);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const b = await c.rest<{ page: number }[]>("GET", path, undefined, undefined, { vary: "sha-b" });
    expect(b).not.toEqual(a);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("viewer() is never served from the cache", async () => {
    const c = new LiveGitHubClient("t");
    fetchMock.mockImplementation(async () => jsonResponse({ data: { viewer: { login: `u${fetchMock.mock.calls.length}`, name: null } } }));
    expect((await c.viewer()).login).toBe("u1");
    expect((await c.viewer()).login).toBe("u2");
  });

  it("a failed write leaves the cache intact", async () => {
    const c = new LiveGitHubClient("t");
    await c.graphql("query A { x }");
    fetchMock.mockImplementationOnce(async () => jsonResponse({ message: "nope" }, 403));
    await expect(c.rest("PATCH", "/repos/o/r/issues/1", { state: "closed" })).rejects.toThrow("GitHub 403: nope");
    await c.graphql("query A { x }");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("never caches a GraphQL error", async () => {
    const c = new LiveGitHubClient("t");
    fetchMock.mockImplementationOnce(async () => jsonResponse({ errors: [{ message: "bad" }] }));
    await expect(c.graphql("query A { x }")).rejects.toThrow("GitHub GraphQL: bad");
    await c.graphql("query A { x }");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
