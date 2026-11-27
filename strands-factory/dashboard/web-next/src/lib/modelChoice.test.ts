import { renderToString } from "react-dom/server";
import { createElement, type FunctionComponent } from "react";
import { MantineProvider } from "@mantine/core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_SELECTION,
  EFFORT_SUGGESTIONS,
  MODEL_SUGGESTIONS,
  loadModelSelection,
  modelLabel,
  saveModelSelection,
  selectionForTurn,
} from "./modelChoice";
import { LiveApi } from "../api/client";
import { ApiError } from "../api/errors";
import { MockApi } from "../api/mock";
import { ModelPicker, RunModels } from "../components/ModelPicker";
import { sessionDetailRuns } from "./chat";

vi.mock("../auth/dashboardToken", () => ({
  getDashboardToken: () => null,
  isDashboardAuthEnabled: () => false,
}));

const OPUS = "bedrock/global.anthropic.claude-opus-5-5";
const HAIKU = "bedrock/global.anthropic.claude-haiku-5-5";

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

describe("model selection store", () => {
  it("defaults, persists any text, and clears", () => {
    expect(loadModelSelection()).toEqual(DEFAULT_SELECTION);
    saveModelSelection({ model: "openai/some-new-model", effort: "whatever" });
    expect(loadModelSelection()).toEqual({ model: "openai/some-new-model", effort: "whatever" });
    saveModelSelection({ model: "", effort: "high" });
    expect(loadModelSelection()).toEqual({ model: "", effort: "high" });
    saveModelSelection({ model: "  ", effort: "" });
    expect(localStorage.getItem("strandly.chatModel")).toBeNull();
  });

  it("reads an older saved choice and ignores unreadable storage", () => {
    localStorage.setItem("strandly.chatModel", JSON.stringify({ model: HAIKU, effort: null }));
    expect(loadModelSelection()).toEqual({ model: HAIKU, effort: "" });
    localStorage.setItem("strandly.chatModel", "{not json");
    expect(loadModelSelection()).toEqual(DEFAULT_SELECTION);
    localStorage.setItem("strandly.chatModel", JSON.stringify({ model: 5 }));
    expect(loadModelSelection()).toEqual(DEFAULT_SELECTION);
  });

  it("sends each trimmed field and nothing for the default", () => {
    expect(selectionForTurn(DEFAULT_SELECTION)).toEqual({});
    expect(selectionForTurn({ model: "  ", effort: " " })).toEqual({});
    expect(selectionForTurn({ model: ` ${HAIKU} `, effort: " low" })).toEqual({ model: HAIKU, effort: "low" });
    expect(selectionForTurn({ model: "", effort: "max" })).toEqual({ effort: "max" });
    expect(selectionForTurn({ model: "anything/at all", effort: "" })).toEqual({ model: "anything/at all" });
  });

  it("labels a model by its id without the provider", () => {
    expect(modelLabel(HAIKU)).toBe("global.anthropic.claude-haiku-5-5");
    expect(modelLabel("bedrock-mantle/openai.gpt-oss-120b")).toBe("openai.gpt-oss-120b");
    expect(modelLabel(undefined)).toBe("");
  });

  it("suggests the repository's model strings and the harness effort levels", () => {
    expect(MODEL_SUGGESTIONS).toContain(OPUS);
    expect(MODEL_SUGGESTIONS).toContain(HAIKU);
    expect(EFFORT_SUGGESTIONS).toEqual(["low", "medium", "high", "xhigh", "max", "auto"]);
  });
});

describe("the live API", () => {
  function stubFetch(body: unknown, status = 200) {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(body), { status })));
    vi.stubGlobal("fetch", fetch);
    return fetch;
  }
  const sent = (fetch: ReturnType<typeof vi.fn>) => JSON.parse(fetch.mock.calls[0][1].body as string);

  it("sends the chosen model and effort with a turn", async () => {
    const fetch = stubFetch({ status: "accepted", taskId: "t1" });
    await new LiveApi("https://portal.test").startChat("s1", "hi", { model: "openai/gpt-5", effort: "xhigh" });
    expect(sent(fetch)).toMatchObject({ session_id: "s1", message: "hi", model: "openai/gpt-5", effort: "xhigh" });
  });

  it("sends an effort alone, and nothing for the default", async () => {
    let fetch = stubFetch({ status: "accepted", taskId: "t1" });
    await new LiveApi("https://portal.test").startChat("s1", "hi", { effort: "high" });
    expect(sent(fetch)).toMatchObject({ effort: "high" });
    expect(sent(fetch)).not.toHaveProperty("model");
    fetch = stubFetch({ status: "accepted", taskId: "t1" });
    await new LiveApi("https://portal.test").startChat("s1", "hi", {});
    expect(sent(fetch)).not.toHaveProperty("model");
    expect(sent(fetch)).not.toHaveProperty("effort");
  });

  it("carries the server's 400 reason", async () => {
    const reason = "Unknown model provider 'nope' in 'nope/x'.";
    stubFetch({ error: reason }, 400);
    const err = await new LiveApi("https://portal.test").startChat("s1", "hi", { model: "nope/x" }).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(400);
    expect(err.serverError).toBe(reason);
  });
});

describe("session runs", () => {
  it("carries the model each ledger turn recorded", () => {
    const runs = sessionDetailRuns({
      session_id: "s1",
      runs: 2,
      turns: [
        { task_id: "t1", status: "completed", model: OPUS, effort: "medium" },
        { task_id: "t2", status: "running" },
        { status: "completed" },
      ],
    });
    expect(runs).toEqual([
      { taskId: "t1", status: "completed", startedAt: undefined, model: OPUS, effort: "medium" },
      { taskId: "t2", status: "running", startedAt: undefined },
    ]);
  });

  it("the mock records each turn's model and refuses an unknown provider with the reason", async () => {
    const api = new MockApi();
    await api.startChat("s-model", "hi", { model: HAIKU, effort: "off" });
    await api.startChat("s-model", "again", {});
    await api.startChat("s-model", "more", { model: "openai/gpt-5" });
    const snap = await api.loadSession("s-model");
    expect(snap.runs?.map((r) => [r.model, r.effort])).toEqual([
      [HAIKU, "off"],
      [OPUS, "medium"],
      ["openai/gpt-5", "auto"],
    ]);
    const err = await api.startChat("s-model", "x", { model: "nope/x" }).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(400);
    expect(err.serverError).toContain("Unknown model provider 'nope'");
  });
});

describe("ModelPicker", () => {
  function render<P extends object>(el: FunctionComponent<P>, props: P): string {
    return renderToString(createElement(MantineProvider, null, createElement(el, props)));
  }

  it("renders free-text model and effort fields that default to empty", () => {
    const html = render(ModelPicker, { selection: DEFAULT_SELECTION, onChange: vi.fn() });
    expect(html).toContain('aria-label="Model"');
    expect(html).toContain('aria-label="Effort"');
    expect(html).toContain('placeholder="Default model"');
    expect(html).toContain('placeholder="Default effort"');
  });

  it("shows any typed model, on or off the suggestions", () => {
    const html = render(ModelPicker, { selection: { model: "ollama/llama3", effort: "medium" }, onChange: vi.fn() });
    expect(html).toContain('value="ollama/llama3"');
    expect(html).toContain('value="medium"');
  });

  it("shows the newest run's model", () => {
    const html = render(RunModels, {
      runs: [
        { taskId: "t1", model: OPUS, effort: "medium" },
        { taskId: "t2", model: HAIKU, effort: "off" },
      ],
    });
    expect(html).toMatch(/Last run: (<!-- -->)?global.anthropic.claude-haiku-5-5 · off/);
    expect(render(RunModels, { runs: [{ taskId: "t1" }] })).not.toContain("Last run");
  });
});
