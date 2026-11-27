/**
 * Back buttons go BACK — to the page the user came from (PR detail → "Open in Strandly" → back lands
 * on the PR), not to the section's list. react-router stamps every entry it pushes with an `idx` on
 * `history.state`; `idx > 0` means there is an in-app entry behind us. A deep link, a PWA launch or
 * a reload lands on idx 0 (or a foreign state) and falls back to the section root instead.
 */
import { useCallback } from "react";
import { useNavigate } from "react-router-dom";

/** True when `history.state` says there is an in-app entry to return to. */
export function hasHistory(state: unknown): boolean {
  if (!state || typeof state !== "object") return false;
  const idx = (state as { idx?: unknown }).idx;
  return typeof idx === "number" && idx > 0;
}

/** A click handler: history back when there is one, else `fallback` (replacing the current entry). */
export function useBack(fallback: string): () => void {
  const navigate = useNavigate();
  return useCallback(() => {
    if (hasHistory(window.history.state)) navigate(-1);
    else navigate(fallback, { replace: true });
  }, [navigate, fallback]);
}
