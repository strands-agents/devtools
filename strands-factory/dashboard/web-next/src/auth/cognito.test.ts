import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
// A fresh module per test: the engine keeps its config and live token in module state.
let cognito: typeof import("./cognito");

const PORTAL = "https://portal.test/";
const DOMAIN = "https://pool.auth.us-west-2.amazoncognito.com";

function memoryStorage(): Storage {
  const data = new Map<string, string>();
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (k: string) => data.get(k) ?? null,
    key: (i: number) => [...data.keys()][i] ?? null,
    removeItem: (k: string) => void data.delete(k),
    setItem: (k: string, v: string) => void data.set(k, String(v)),
  };
}

let assign: ReturnType<typeof vi.fn>;
let replaceState: ReturnType<typeof vi.fn>;

function landOn(search: string): void {
  vi.stubGlobal("window", {
    location: { origin: "https://portal.test", search, hash: "", assign },
    history: { replaceState },
  });
}

function stubTokenEndpoint() {
  const fetch = vi.fn().mockImplementation(() =>
    Promise.resolve(
      new Response(JSON.stringify({ access_token: "access-1", id_token: "id-1", refresh_token: "r-1", expires_in: 3600 })),
    ),
  );
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

/** Run login() and return the state it sent on the authorize request. */
async function startLogin(): Promise<string> {
  landOn("");
  await cognito.login();
  const authorize = new URL(String(assign.mock.calls[0][0]));
  return authorize.searchParams.get("state") ?? "";
}

beforeEach(async () => {
  vi.resetModules();
  cognito = await import("./cognito");
  vi.useFakeTimers();
  vi.stubGlobal("sessionStorage", memoryStorage());
  vi.stubGlobal("localStorage", memoryStorage());
  assign = vi.fn();
  replaceState = vi.fn();
  cognito.configureCognito({ domain: DOMAIN, clientId: "client-1", redirectUri: PORTAL });
});

afterEach(() => {
  cognito.configureCognito(null);
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Hosted-UI sign-in state", () => {
  it("login sends a random state with the PKCE challenge", async () => {
    const state = await startLogin();
    const authorize = new URL(String(assign.mock.calls[0][0]));
    expect(authorize.origin + authorize.pathname).toBe(DOMAIN + "/oauth2/authorize");
    expect(state).toMatch(/^[0-9a-f]{64}$/);
    expect(authorize.searchParams.get("code_challenge_method")).toBe("S256");
    expect(authorize.searchParams.get("code_challenge")).toBeTruthy();
  });

  it("exchanges the code when the returned state matches the stashed one", async () => {
    const state = await startLogin();
    const fetch = stubTokenEndpoint();
    landOn(`?code=code-1&state=${state}`);
    await cognito.initCognitoSession();
    expect(fetch).toHaveBeenCalledTimes(1);
    const body = new URLSearchParams(String(fetch.mock.calls[0][1].body));
    expect(body.get("code")).toBe("code-1");
    expect(body.get("code_verifier")).toMatch(/^[0-9a-f]{128}$/);
    expect(cognito.getAccessToken()).toBe("access-1");
    expect(replaceState).toHaveBeenCalledWith({}, "", PORTAL);
  });

  it("refuses a code when no sign-in was started in this tab", async () => {
    const fetch = stubTokenEndpoint();
    landOn("?code=attacker-code&state=anything");
    await expect(cognito.initCognitoSession()).rejects.toThrow("no sign-in in progress");
    expect(fetch).not.toHaveBeenCalled();
    expect(cognito.getAccessToken()).toBeNull();
    expect(replaceState).toHaveBeenCalledWith({}, "", PORTAL);
  });

  it("refuses a code whose state does not match, and consumes the stash", async () => {
    const state = await startLogin();
    const fetch = stubTokenEndpoint();
    landOn("?code=attacker-code&state=forged");
    await expect(cognito.initCognitoSession()).rejects.toThrow("state mismatch");
    expect(fetch).not.toHaveBeenCalled();
    expect(cognito.getAccessToken()).toBeNull();
    // The genuine redirect arriving afterwards finds no stash either.
    landOn(`?code=code-1&state=${state}`);
    await expect(cognito.initCognitoSession()).rejects.toThrow("no sign-in in progress");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("refuses a code that carries no state", async () => {
    await startLogin();
    const fetch = stubTokenEndpoint();
    landOn("?code=attacker-code");
    await expect(cognito.initCognitoSession()).rejects.toThrow("state mismatch");
    expect(fetch).not.toHaveBeenCalled();
  });
  it("keeps an existing session when a planted code is refused", async () => {
    localStorage.setItem(
      "strandly.portal.tokens",
      JSON.stringify({ access_token: "mine", refresh_token: "r-mine", expires_at: Date.now() + 3_600_000 }),
    );
    const fetch = stubTokenEndpoint();
    landOn("?code=attacker-code&state=anything");
    await expect(cognito.initCognitoSession()).rejects.toThrow("no sign-in in progress");
    expect(fetch).not.toHaveBeenCalled();
    expect(cognito.getAccessToken()).toBe("mine");
  });

  it("ends the attempt when the Hosted UI returns an error", async () => {
    const state = await startLogin();
    const fetch = stubTokenEndpoint();
    landOn(`?error=access_denied&state=${state}`);
    await expect(cognito.initCognitoSession()).rejects.toThrow("access_denied");
    expect(replaceState).toHaveBeenCalledWith({}, "", PORTAL);
    // The stash is gone, so the same state cannot be replayed with a code later.
    landOn(`?code=code-1&state=${state}`);
    await expect(cognito.initCognitoSession()).rejects.toThrow("no sign-in in progress");
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("logout", () => {
  it("revokes the refresh token, clears the session and leaves through the Hosted UI", async () => {
    const state = await startLogin();
    const fetch = stubTokenEndpoint();
    landOn(`?code=code-1&state=${state}`);
    await cognito.initCognitoSession();
    assign.mockClear();
    cognito.logout();
    const [url, init] = fetch.mock.calls[1];
    expect(url).toBe(DOMAIN + "/oauth2/revoke");
    expect(new URLSearchParams(String(init.body)).get("token")).toBe("r-1");
    expect(init.keepalive).toBe(true);
    expect(cognito.getAccessToken()).toBeNull();
    const leave = new URL(String(assign.mock.calls[0][0]));
    expect(leave.origin + leave.pathname).toBe(DOMAIN + "/logout");
    expect(leave.searchParams.get("client_id")).toBe("client-1");
  });
});
