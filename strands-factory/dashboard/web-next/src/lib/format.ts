/** Coarse relative time ("3m", "2h", "4d", "just now") for list rows and chat captions. */
export function relTime(iso: string | undefined): string {
  if (!iso) return "";
  const ms = Date.now() - Date.parse(iso);
  if (!Number.isFinite(ms)) return "";
  const min = Math.round(ms / 60_000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h`;
  return `${Math.round(hr / 24)}d`;
}

/**
 * {@link relTime} as prose for "opened … / updated …" captions: "3m ago", "2h ago", "just now", "".
 * Shared by Today rows and the issue / PR detail meta lines so every page says it the same way.
 */
export function relTimeAgo(iso: string | undefined): string {
  const rel = relTime(iso);
  return !rel || rel === "just now" ? rel : `${rel} ago`;
}
