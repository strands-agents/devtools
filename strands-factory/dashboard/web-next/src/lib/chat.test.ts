import { describe, expect, it } from "vitest";
import { applyEvent, emptyTurn, sessionDetailToMessages, turnToMessage } from "./chat";
import { groupBlocks } from "../components/chatView";

describe("tool-use ids survive into the rendered rows", () => {
  it("live stream: a result is paired with its call by toolUseId even when results arrive out of call order", () => {
    let live = emptyTurn();
    live = applyEvent(live, { kind: "tool_start", tool: "read", toolUseId: "read-1", data: { input: '{"path":"missing.txt"}' } });
    live = applyEvent(live, { kind: "tool_start", tool: "shell", toolUseId: "shell-2", data: { input: '{"command":"pwd"}' } });
    live = applyEvent(live, {
      kind: "tool_result",
      tool: "read",
      toolUseId: "read-1",
      data: { status: "error", content: [{ text: "missing.txt: file not found" }] },
    });
    live = applyEvent(live, {
      kind: "tool_result",
      tool: "shell",
      toolUseId: "shell-2",
      data: { status: "success", content: [{ text: "/workspace" }] },
    });
    const rows = groupBlocks(turnToMessage(live).blocks!);
    expect(rows).toEqual([
      {
        kind: "activity",
        items: [
          expect.objectContaining({ name: "read", result: { status: "error", text: "missing.txt: file not found" } }),
          expect.objectContaining({ name: "shell", result: { status: "success", text: "/workspace" } }),
        ],
      },
    ]);
  });

  it("live stream: a result without a toolUseId is never attached to a call", () => {
    let live = emptyTurn();
    live = applyEvent(live, { kind: "tool_start", tool: "shell", toolUseId: "s1", data: { input: "{}" } });
    live = applyEvent(live, { kind: "tool_result", data: { status: "success", content: [{ text: "out" }] } });
    const [row] = groupBlocks(turnToMessage(live).blocks!);
    expect(row).toEqual({
      kind: "activity",
      items: [
        { kind: "tool", name: "shell", input: "{}", sub: undefined },
        { kind: "result", status: "success", text: "out" },
      ],
    });
  });

  it("history: block ids from the API ride along (and are omitted when the API lacks them)", () => {
    const [msg] = sessionDetailToMessages({
      session_id: "s",
      runs: 1,
      messages: [
        {
          role: "assistant",
          text: "",
          blocks: [
            { kind: "tool", id: "a", name: "read", input: "x" },
            { kind: "tool", name: "legacy" },
            { kind: "result", id: "a", status: "success", text: "ok" },
            { kind: "result", status: "success", text: "orphan" },
          ],
        },
      ],
    });
    expect(msg.blocks).toEqual([
      { kind: "tool", id: "a", name: "read", input: "x" },
      { kind: "tool", name: "legacy", input: undefined },
      { kind: "result", id: "a", status: "success", text: "ok" },
      { kind: "result", status: "success", text: "orphan" },
    ]);
  });

  it("history: context blocks keep their source; a blank one is dropped", () => {
    const [msg] = sessionDetailToMessages({
      session_id: "s",
      runs: 1,
      messages: [
        {
          role: "user",
          text: "hi",
          blocks: [
            { kind: "text", text: "hi" },
            { kind: "context", source: "caller_context:injector", text: "# Caller context" },
            { kind: "context", source: "strands:environment:injector", text: "" },
          ],
        },
      ],
    });
    expect(msg.text).toBe("hi");
    expect(msg.blocks).toEqual([
      { kind: "text", text: "hi" },
      { kind: "context", source: "caller_context:injector", text: "# Caller context" },
    ]);
  });
});
