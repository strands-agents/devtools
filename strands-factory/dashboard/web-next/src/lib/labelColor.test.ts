import { describe, expect, it } from "vitest";
import { labelBg, labelTextColor, parseHex } from "./labelColor";

describe("label colors", () => {
  it("parses 6- and 3-digit hex, rejects garbage", () => {
    expect(parseHex("d73a4a")).toEqual([215, 58, 74]);
    expect(parseHex("#FFF")).toEqual([255, 255, 255]);
    expect(parseHex("zzz")).toBeNull();
    expect(parseHex("")).toBeNull();
  });
  it("picks dark text on light backgrounds and light text on dark ones", () => {
    expect(labelTextColor("ffffff")).toBe("#000000");
    expect(labelTextColor("fbca04")).toBe("#000000"); // GitHub yellow
    expect(labelTextColor("000000")).toBe("#ffffff");
    expect(labelTextColor("d73a4a")).toBe("#ffffff"); // GitHub red
    expect(labelTextColor("0075ca")).toBe("#ffffff"); // GitHub blue
  });
  it("labelBg normalizes and falls back", () => {
    expect(labelBg("D73A4A")).toBe("#d73a4a");
    expect(labelBg("#0075ca")).toBe("#0075ca");
    expect(labelBg("nope")).toBe("#6e7681");
  });
});
