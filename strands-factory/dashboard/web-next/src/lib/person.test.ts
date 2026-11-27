import { renderToString } from "react-dom/server";
import { createElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { MantineProvider } from "@mantine/core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getPersonLogin, normalizeLogin, personHeaders, setPersonLogin, subscribePersonLogin } from "./person";
import { LiveApi } from "../api/client";
import { PersonBadge } from "../components/AppShell";

vi.mock("../auth/dashboardToken", () => ({
  getDashboardToken: () => null,
  isDashboardAuthEnabled: () => false,
}));

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

beforeEach(() => vi.stubGlobal("localStorage", memoryStorage()));
afterEach(() => vi.unstubAllGlobals());

describe("person login store", () => {
  it("accepts GitHub logins only, with or without @", () => {
    expect(normalizeLogin(" @Octo-Cat ")).toBe("Octo-Cat");
    expect(normalizeLogin("bad--login")).toBe("");
    expect(normalizeLogin("-leading")).toBe("");
    expect(normalizeLogin("a".repeat(40))).toBe("");
    expect(normalizeLogin("name with spaces")).toBe("");
  });

  it("persists, notifies and clears", () => {
    const seen = vi.fn();
    const off = subscribePersonLogin(seen);
    expect(getPersonLogin()).toBe("");
    expect(personHeaders()).toEqual({});
    expect(setPersonLogin("@octocat")).toBe("octocat");
    expect(getPersonLogin()).toBe("octocat");
    expect(personHeaders()).toEqual({ "X-Strandly-Person": "github:octocat" });
    setPersonLogin("not a login");
    expect(getPersonLogin()).toBe("");
    expect(seen).toHaveBeenCalledTimes(2);
    off();
  });

  it("ignores a tampered stored value", () => {
    localStorage.setItem("strandly.person.github", "x\ny");
    expect(getPersonLogin()).toBe("");
    expect(personHeaders()).toEqual({});
  });
});

describe("dashboard API requests carry the person header", () => {
  function stubFetch() {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ sessions: [] }))));
    vi.stubGlobal("fetch", fetch);
    return fetch;
  }
  const headersOf = (fetch: ReturnType<typeof vi.fn>) => fetch.mock.calls[0][1].headers as Record<string, string>;

  it("sends X-Strandly-Person when a login is set", async () => {
    setPersonLogin("octocat");
    const fetch = stubFetch();
    await new LiveApi("https://portal.test").listSessions();
    expect(headersOf(fetch)["X-Strandly-Person"]).toBe("github:octocat");
  });

  it("sends no person header without a login", async () => {
    const fetch = stubFetch();
    await new LiveApi("https://portal.test").listSessions();
    expect(Object.keys(headersOf(fetch))).not.toContain("X-Strandly-Person");
  });
});

describe("PersonBadge", () => {
  const render = (login: string) =>
    renderToString(
      createElement(MantineProvider, null, createElement(MemoryRouter, null, createElement(PersonBadge, { login }))),
    );

  it("shows the login", () => {
    expect(render("octocat")).toMatch(/@(<!-- -->)?octocat/);
  });

  it("asks who you are without one", () => {
    expect(render("")).toContain("Who are you?");
  });
});
