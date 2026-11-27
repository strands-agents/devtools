/**
 * Route builders — the ONE place in-app paths are spelled out, so a view never hand-concatenates a
 * route (and a route rename is a one-line change). Hash-routed (see main.tsx), so these are the
 * part after `#`.
 *
 *   /today                              Today feed
 *   /strandly                           Strandly sessions list
 *   /strandly/:sessionId                a session's chat
 *   /issues  · /issues/:owner/:repo/:n  issues list · issue detail
 *   /pulls   · /pulls/:owner/:repo/:n   PRs list · PR detail (conversation + files)
 *   /schedules · /schedules/:id         scheduled jobs · one job (runs, revisions)
 *   /settings
 */
import { itemToSessionId, type SessionKind } from "./sessionId";

export const ROUTES = {
  today: "/today",
  strandly: "/strandly",
  issues: "/issues",
  pulls: "/pulls",
  schedules: "/schedules",
  settings: "/settings",
} as const;

/** `owner/repo` → the two encoded path segments. */
function repoSegments(repo: string): string {
  return repo
    .split("/")
    .map((s) => encodeURIComponent(s))
    .join("/");
}

/** Deep link to a session's chat. */
export function sessionLink(sessionId: string): string {
  return `${ROUTES.strandly}/${encodeURIComponent(sessionId)}`;
}

/** Deep link to a scheduled job's page. */
export function scheduleLink(id: string): string {
  return `${ROUTES.schedules}/${encodeURIComponent(id)}`;
}

/** Deep link to an issue's detail page. `repo` is `owner/repo`. */
export function issueLink(repo: string, number: number): string {
  return `${ROUTES.issues}/${repoSegments(repo)}/${number}`;
}

/** Deep link to a PR's detail page. `repo` is `owner/repo`. */
export function pullLink(repo: string, number: number): string {
  return `${ROUTES.pulls}/${repoSegments(repo)}/${number}`;
}

/** Detail page for a GitHub item of either kind (discussions have no page — GitHub URL instead). */
export function itemLink(repo: string, kind: SessionKind, number: number, url?: string): string {
  if (kind === "pr") return pullLink(repo, number);
  if (kind === "issue") return issueLink(repo, number);
  return url ?? `https://github.com/${repo}/discussions/${number}`;
}

/** The chat for a GitHub item's canonical `gh-…` session — the "talk to Strandly about this" shortcut. */
export function itemSessionLink(repo: string, kind: SessionKind, number: number): string {
  return sessionLink(itemToSessionId(repo, kind, number));
}
