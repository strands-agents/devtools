import { describe, expect, it, vi } from "vitest";
import { cleanTitle, commitTitle, SESSION_TITLE_MAX_LEN, titleEditKey } from "./sessionTitle";

describe("cleanTitle", () => {
  it("makes one line without invisible characters", () => {
    expect(cleanTitle("  Fix\nthe\tbuild\u0000\u202e  now ")).toBe("Fix the build now");
    expect(cleanTitle(" \n ")).toBe("");
  });
  it("matches the server's length bound", () => {
    expect(SESSION_TITLE_MAX_LEN).toBe(80);
  });
});

describe("titleEditKey", () => {
  it("Enter saves, Escape cancels, an IME composition's Enter types", () => {
    expect(titleEditKey("Enter")).toBe("save");
    expect(titleEditKey("Escape")).toBe("cancel");
    expect(titleEditKey("a")).toBeNull();
    expect(titleEditKey("Enter", true)).toBeNull();
  });
});

describe("commitTitle — optimistic rename", () => {
  it("shows the new title at once, then what the server stored", async () => {
    const shown: (string | null)[] = [];
    let resolve!: (v: string | null) => void;
    const save = vi.fn(() => new Promise<string | null>((r) => (resolve = r)));
    const done = commitTitle({ previous: "Old", next: "  New\ttitle ", apply: (t) => shown.push(t), save });
    expect(shown).toEqual(["New title"]);
    expect(save).toHaveBeenCalledWith("New title");
    resolve("New title");
    await expect(done).resolves.toBe("New title");
    expect(shown).toEqual(["New title", "New title"]);
  });

  it("rolls back to the previous title and rethrows when the save fails", async () => {
    const shown: (string | null)[] = [];
    const err = new Error("503");
    await expect(
      commitTitle({ previous: "Old", next: "New", apply: (t) => shown.push(t), save: () => Promise.reject(err) }),
    ).rejects.toBe(err);
    expect(shown).toEqual(["New", "Old"]);
  });

  it("an empty title clears (shown as null, sent as empty)", async () => {
    const shown: (string | null)[] = [];
    const save = vi.fn(async () => null);
    await commitTitle({ previous: "Old", next: "   ", apply: (t) => shown.push(t), save });
    expect(save).toHaveBeenCalledWith("");
    expect(shown).toEqual([null, null]);
  });

  it("an unchanged title never calls the server", async () => {
    const save = vi.fn(async () => "Same");
    const apply = vi.fn();
    await commitTitle({ previous: "Same", next: " Same ", apply, save });
    await commitTitle({ previous: null, next: "", apply, save });
    expect(save).not.toHaveBeenCalled();
    expect(apply).not.toHaveBeenCalled();
  });
});
