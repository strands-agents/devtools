import { describe, expect, it } from "vitest";
import { readNavState } from "./navState";

const viewing = { type: "pr", repo: "o/r", number: 7, url: "https://github.com/o/r/pull/7" };

describe("readNavState", () => {
  it("tolerates null / non-object state", () => {
    expect(readNavState(null)).toEqual({});
    expect(readNavState(undefined)).toEqual({});
    expect(readNavState("nope")).toEqual({});
  });
  it("keeps a well-formed viewing ref and a non-blank first message", () => {
    expect(readNavState({ viewing, firstMessage: "hi" })).toEqual({ viewing, firstMessage: "hi" });
  });
  it("drops a malformed viewing ref and a blank first message", () => {
    expect(readNavState({ viewing: { repo: "o/r" }, firstMessage: "   " })).toEqual({ viewing: undefined, firstMessage: undefined });
    expect(readNavState({ viewing: "o/r#7", firstMessage: 42 })).toEqual({ viewing: undefined, firstMessage: undefined });
  });
});
