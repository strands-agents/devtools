/**
 * The chat model a user chose: free text, persisted per browser (localStorage) and sent with each turn.
 *
 * The portal checks nothing beyond trimming: the runtime hands the strings to the harness model
 * resolution, and the chat shows its refusal (dashboard/api handler.py `_chat_model`). The
 * suggestions are examples, not a list of what is allowed.
 */

const STORAGE_KEY = "strandly.chatModel";

/** A chosen model and effort; `""` is the deployment's default for either. */
export interface ModelSelection {
  model: string;
  effort: string;
}

export const DEFAULT_SELECTION: ModelSelection = { model: "", effort: "" };

/**
 * Model strings the harness already runs: its default model and the subagent tiers' models
 * (harness core/constants.py `MODEL_TIERS`). Hints only; any harness model string may be typed.
 */
export const MODEL_SUGGESTIONS = [
  "bedrock/global.anthropic.claude-opus-5-5",
  "bedrock/global.anthropic.claude-fable-5-2",
  "bedrock/global.anthropic.claude-haiku-5-5",
];

/** Harness effort levels to suggest; empty runs the model's default effort. */
export const EFFORT_SUGGESTIONS = ["low", "medium", "high", "xhigh", "max", "auto"];

/** The saved selection, or the default when nothing (or nothing readable) is saved. */
export function loadModelSelection(): ModelSelection {
  let raw: unknown;
  try {
    raw = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null");
  } catch {
    return DEFAULT_SELECTION;
  }
  if (!raw || typeof raw !== "object") return DEFAULT_SELECTION;
  const { model, effort } = raw as { model?: unknown; effort?: unknown };
  return {
    model: typeof model === "string" ? model.trim() : "",
    effort: typeof effort === "string" ? effort.trim() : "",
  };
}

export function saveModelSelection(selection: ModelSelection): void {
  try {
    if (selection.model.trim() || selection.effort.trim()) {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(selection));
    } else {
      localStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    // storage unavailable: the choice holds for this page only
  }
}

/** What a turn sends: each trimmed, non-empty field; nothing for the default. */
export function selectionForTurn(selection: ModelSelection): { model?: string; effort?: string } {
  const model = selection.model.trim();
  const effort = selection.effort.trim();
  return { ...(model ? { model } : {}), ...(effort ? { effort } : {}) };
}

/** A model string without its provider prefix, for a compact label. */
export function modelLabel(id: string | undefined): string {
  if (!id) return "";
  return id.replace(/^[a-z-]+\//, "");
}
