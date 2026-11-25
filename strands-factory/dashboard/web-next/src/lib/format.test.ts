import { afterEach, describe, expect, it, vi } from "vitest";
import { relTime, relTimeAgo } from "./format";

afterEach(() => vi.useRealTimers());

describe("relTime / relTimeAgo", () => {
  it("coarsens to m / h / d and says 'just now' under a minute", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T12:00:00Z"));
    expect(relTime("2026-01-01T11:59:40Z")).toBe("just now");
    expect(relTime("2026-01-01T11:57:00Z")).toBe("3m");
    expect(relTime("2026-01-01T10:00:00Z")).toBe("2h");
    expect(relTime("2025-12-27T12:00:00Z")).toBe("5d");
    expect(relTime(undefined)).toBe("");
    expect(relTime("not a date")).toBe("");
  });

  it("relTimeAgo adds 'ago' except for 'just now' and blanks", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T12:00:00Z"));
    expect(relTimeAgo("2026-01-01T11:57:00Z")).toBe("3m ago");
    expect(relTimeAgo("2026-01-01T10:00:00Z")).toBe("2h ago");
    expect(relTimeAgo("2026-01-01T11:59:50Z")).toBe("just now");
    expect(relTimeAgo(undefined)).toBe("");
  });
});
