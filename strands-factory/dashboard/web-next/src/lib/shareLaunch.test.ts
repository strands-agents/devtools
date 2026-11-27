import { describe, expect, it, vi } from "vitest";
import { SHARE_PARAMS, consumeShareLaunch, isShareLaunch, resolveShareLaunch, shareRoute } from "./shareLaunch";
import { SHARE_TARGET } from "./shareTarget";

const q = (params: Record<string, string>) => "?" + new URLSearchParams(params).toString();

describe("shareLaunch", () => {
  it("is not a share launch without any share_* param", () => {
    expect(isShareLaunch("")).toBe(false);
    expect(isShareLaunch("?utm_source=slack&url=https://github.com/o/r/pull/1")).toBe(false);
    expect(resolveShareLaunch("?foo=bar")).toBeNull();
  });

  it("opens a shared PR / issue URL on its detail page", () => {
    expect(resolveShareLaunch(q({ share_url: "https://github.com/strands-agents/sdk-python/pull/42" }))).toMatchObject({
      to: "/pulls/strands-agents/sdk-python/42",
      item: { repo: "strands-agents/sdk-python", kind: "pr", number: 42 },
    });
    expect(resolveShareLaunch(q({ share_url: "https://github.com/o/r/issues/7#issuecomment-1" }))?.to).toBe(
      "/issues/o/r/7",
    );
  });

  it("finds the link inside free text (Android puts the URL in `text`)", () => {
    const r = resolveShareLaunch(q({ share_text: "check this out https://github.com/o/r/pull/9 pls" }));
    expect(r?.to).toBe("/pulls/o/r/9");
    expect(r?.item?.sessionId).toBe("gh-o-r-96458419-pr-9");
  });

  it("scans url, then text, then title — first GitHub item wins", () => {
    const r = resolveShareLaunch(
      q({ share_title: "Fix the thing · Pull Request #3", share_text: "https://github.com/o/r/issues/3", share_url: "https://example.com/x" }),
    );
    expect(r?.to).toBe("/issues/o/r/3");
    expect(resolveShareLaunch(q({ share_title: "see https://github.com/o/r/pull/5" }))?.to).toBe("/pulls/o/r/5");
  });

  it("tolerates what real shares look like: sub-paths, queries, fragments, punctuation, case, www", () => {
    const pr = "/pulls/o/r/42";
    expect(resolveShareLaunch(q({ share_text: "https://github.com/o/r/pull/42/files" }))?.to).toBe(pr);
    expect(resolveShareLaunch(q({ share_text: "https://github.com/o/r/pull/42?diff=split#r123" }))?.to).toBe(pr);
    expect(resolveShareLaunch(q({ share_text: "look: https://github.com/o/r/pull/42." }))?.to).toBe(pr);
    expect(resolveShareLaunch(q({ share_text: "(https://github.com/o/r/pull/42)" }))?.to).toBe(pr);
    expect(resolveShareLaunch(q({ share_text: "HTTPS://GITHUB.COM/o/r/PULL/42" }))?.to).toBe(pr);
    expect(resolveShareLaunch(q({ share_text: "http://www.github.com/o/r/pull/42" }))?.to).toBe(pr);
    expect(resolveShareLaunch(q({ share_text: "github.com/o/r/pull/42" }))?.to).toBe(pr);
    expect(resolveShareLaunch(q({ share_url: "https://github.com/o/r/issues/7?x=y" }))?.to).toBe("/issues/o/r/7");
    // Android's GitHub app / Chrome prepend the title to the text.
    expect(
      resolveShareLaunch(q({ share_text: "Fix the thing · Pull Request #42 · o/r\nhttps://github.com/o/r/pull/42" }))?.to,
    ).toBe(pr);
  });

  it("only a github.com URL counts — not a look-alike host or a non-GitHub /a/b/issues/n path", () => {
    expect(resolveShareLaunch(q({ share_text: "https://notgithub.com/o/r/pull/42" }))?.item).toBeNull();
    expect(resolveShareLaunch(q({ share_text: "https://github.community/o/r/pull/42" }))?.item).toBeNull();
    // A foreign tracker URL shaped like `/x/y/issues/n`, with github.com mentioned elsewhere in the text.
    expect(
      resolveShareLaunch(q({ share_text: "https://tracker.example/acme/widgets/issues/5 (mirrored from github.com/o/r)" }))
        ?.item,
    ).toBeNull();
    // Repo root / user page / a kind segment without a number are not items.
    expect(resolveShareLaunch(q({ share_url: "https://github.com/o/r/pulls" }))?.item).toBeNull();
    expect(resolveShareLaunch(q({ share_url: "https://github.com/o/r/issues/new" }))?.item).toBeNull();
    expect(resolveShareLaunch(q({ share_url: "https://github.com/o" }))?.item).toBeNull();
  });

  it("sends a discussion to its gh- session (no in-app page)", () => {
    expect(resolveShareLaunch(q({ share_url: "https://github.com/o/r/discussions/12" }))?.to).toBe("/strandly/gh-o-r-96458419-disc-12");
  });

  it("ignores a bare owner/repo#n in share text (only URL form counts)", () => {
    const r = resolveShareLaunch(q({ share_text: "o/r#12 looks broken" }));
    expect(r?.item).toBeNull();
    expect(r?.to).toBe("/strandly");
  });

  it("falls back to the Strandly tab with the payload when nothing is a GitHub item", () => {
    const r = resolveShareLaunch(q({ share_text: "https://github.com/o/r", share_title: "a repo" }));
    expect(r).toEqual({ to: "/strandly", item: null, shared: "https://github.com/o/r a repo" });
    expect(resolveShareLaunch(q({ share_text: "" }))).toEqual({ to: "/strandly", item: null, shared: "" });
  });

  it("shareRoute maps kinds to routes", () => {
    expect(shareRoute({ repo: "o/r", kind: "issue", number: 1, sessionId: "gh-o-r-96458419-issue-1" })).toBe("/issues/o/r/1");
    expect(shareRoute({ repo: "o/r", kind: "disc", number: 1, sessionId: "gh-o-r-96458419-disc-1" })).toBe("/strandly/gh-o-r-96458419-disc-1");
  });

  it("consumeShareLaunch rewrites to the hash route on the same pathname and drops the query", () => {
    const replaceState = vi.fn();
    const loc = { search: q({ share_text: "https://github.com/o/r/pull/2" }), pathname: "/portal/index.html" } as Location;
    const r = consumeShareLaunch(loc, { replaceState } as unknown as History);
    expect(r?.to).toBe("/pulls/o/r/2");
    expect(replaceState).toHaveBeenCalledWith(null, "", "/portal/index.html#/pulls/o/r/2");
  });

  it("consumeShareLaunch is a no-op on a normal load", () => {
    const replaceState = vi.fn();
    expect(consumeShareLaunch({ search: "", pathname: "/" } as Location, { replaceState } as unknown as History)).toBeNull();
    expect(replaceState).not.toHaveBeenCalled();
  });

  it("manifest share_target: GET onto the app root (same relative URL as start_url/scope), namespaced params", () => {
    // vite.config.ts emits SHARE_TARGET verbatim, so this pins what ships in manifest.webmanifest.
    expect(SHARE_TARGET.action).toBe("./");
    expect(SHARE_TARGET.method).toBe("GET");
    expect(SHARE_TARGET.params).toBe(SHARE_PARAMS);
    expect(Object.keys(SHARE_PARAMS).sort()).toEqual(["text", "title", "url"]);
    for (const name of Object.values(SHARE_PARAMS)) expect(name).toMatch(/^share_[a-z]+$/);
  });
});
