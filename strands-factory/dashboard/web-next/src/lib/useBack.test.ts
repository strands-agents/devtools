import { describe, expect, it } from "vitest";
import { hasHistory } from "./useBack";

describe("hasHistory", () => {
  it("is true once react-router has pushed past the first entry", () => {
    expect(hasHistory({ usr: null, key: "abc", idx: 1 })).toBe(true);
    expect(hasHistory({ idx: 7 })).toBe(true);
  });

  it("is false on the first entry (deep link, PWA launch, reload)", () => {
    expect(hasHistory({ usr: null, key: "abc", idx: 0 })).toBe(false);
  });

  it("is false for foreign or missing state", () => {
    expect(hasHistory(null)).toBe(false);
    expect(hasHistory(undefined)).toBe(false);
    expect(hasHistory("idx")).toBe(false);
    expect(hasHistory({})).toBe(false);
    expect(hasHistory({ idx: "1" })).toBe(false);
  });
});
