/**
 * Sessions-list filtering — PURE (no React, no fetching) so the Mine / Portal / Items / All scopes
 * and the free-text box are unit-testable. Mine = sessions that carry the viewer's login (see
 * isOwnedBy); the text box also matches a typed login, and a pasted GitHub URL / `owner/repo#n`
 * becomes a jump to that item's session (see itemTargets).
 */
import type { SessionSummary, ViewingRef } from "../../api/types";
import {
  isPortalSession,
  itemUrl,
  parseItemInput,
  parseSessionId,
  portalSessionOwner,
  sanitizeLogin,
  sessionLabel,
  type ParsedItem,
} from "../../lib/sessionId";

export type SessionScope = "mine" | "portal" | "items" | "all";

export const SCOPES: { value: SessionScope; label: string }[] = [
  { value: "mine", label: "Mine" },
  { value: "portal", label: "Portal" },
  { value: "items", label: "Items" },
  { value: "all", label: "All" },
];

/** The default scope: Mine when we know who is looking, else All. */
export function defaultScope(login: string | null | undefined): SessionScope {
  return login ? "mine" : "all";
}

/** True when `id` is a canonical GitHub-item session (`gh-…`). */
export function isItemSession(id: string): boolean {
  return id.startsWith("gh-");
}

/** Does the session fall inside the scope? `mine` needs a login; without one it matches nothing. */
export function inScope(session: SessionSummary, scope: SessionScope, login: string | null | undefined): boolean {
  const id = session.session_id;
  switch (scope) {
    case "mine":
      return !!login && isOwnedBy(id, login);
    case "portal":
      return isPortalSession(id);
    case "items":
      return isItemSession(id);
    default:
      return true;
  }
}

/**
 * Does the session belong to `login`? Portal ids carry the owner verbatim, so they match on the
 * parsed owner EXACTLY (a prefix test would let `bob` see `portal-bob-smith-…`). Sessions minted by
 * other surfaces (Slack, CLI, …) spell the login somewhere in their id, so any non-item id that
 * contains the login as a whole `[^a-z0-9]`-bounded token counts too (a hyphen is both a login
 * character and a delimiter there, so `bob` also sees a foreign `…-bob-smith-…` — accepted; those
 * ids' grammar isn't ours). `gh-…` item sessions are keyed on the repo, never on a person —
 * `gh-alice-repo-<h8>-pr-1` is alice's REPO, not her conversation.
 */
export function isOwnedBy(sessionId: string, login: string): boolean {
  const me = sanitizeLogin(login);
  if (isPortalSession(sessionId)) return portalSessionOwner(sessionId) === me;
  if (isItemSession(sessionId)) return false;
  // sanitizeLogin leaves only [a-z0-9-], so `me` is regex-safe as is.
  return new RegExp(`(^|[^a-z0-9])${me}([^a-z0-9]|$)`).test(sessionId.toLowerCase());
}

/** The row's primary label: the title when set; else `owner-repo#42` for items, the description's first line otherwise. */
export function primaryLabel(session: SessionSummary): string {
  const id = session.session_id;
  const title = session.title?.trim();
  if (title) return title;
  if (isItemSession(id)) return sessionLabel(id);
  const first = firstLine(session.description) || firstLine(session.latest_prompt);
  return first || (isPortalSession(id) ? "New conversation" : id);
}

/** The secondary line: the originating ask (items), or the latest prompt when it differs from the label. */
export function secondaryText(session: SessionSummary): string {
  const label = primaryLabel(session);
  // A titled item session keeps its item label visible on the second line.
  const itemLabel = isItemSession(session.session_id) ? sessionLabel(session.session_id) : null;
  const candidates = [itemLabel, session.description, session.latest_prompt];
  for (const c of candidates) {
    const line = firstLine(c);
    if (line && line !== label) return line;
  }
  return "";
}

function firstLine(text: string | null | undefined): string {
  if (!text) return "";
  const line = text.split(/\r?\n/).find((l) => l.trim());
  return (line ?? "").trim();
}

/**
 * Case-insensitive free-text match against the id, the display label, the description, the latest
 * prompt, the item target URL and (portal sessions) the owner login. Every whitespace-separated
 * term must match somewhere. A leading `@` is dropped so "@bob" (the placeholder's own example)
 * matches the login baked into `portal-bob-…`.
 */
export function matchesText(session: SessionSummary, query: string): boolean {
  const terms = query
    .toLowerCase()
    .split(/\s+/)
    .map((t) => t.replace(/^@+/, ""))
    .filter(Boolean);
  if (terms.length === 0) return true;
  const id = session.session_id;
  const hay = [
    id,
    primaryLabel(session),
    session.description ?? "",
    session.latest_prompt ?? "",
    session.target ?? "",
    portalSessionOwner(id) ?? "",
  ]
    .join("\n")
    .toLowerCase();
  return terms.every((t) => hay.includes(t));
}

/** Newest activity first; sessions without a timestamp sink to the bottom (stable). */
export function sortByActivity(sessions: SessionSummary[]): SessionSummary[] {
  return [...sessions].sort((a, b) => {
    const ta = a.last_activity ? Date.parse(a.last_activity) : Number.NaN;
    const tb = b.last_activity ? Date.parse(b.last_activity) : Number.NaN;
    const va = Number.isFinite(ta) ? ta : -Infinity;
    const vb = Number.isFinite(tb) ? tb : -Infinity;
    return vb - va;
  });
}

/**
 * Scope + text + sort, in one call — what the page renders. A query that names a GitHub item (see
 * itemTargets) also matches that item's session by id, in ANY scope: Mine is the default and would
 * otherwise hide the very `gh-…` session the pasted URL points at.
 */
export function filterSessions(
  sessions: SessionSummary[],
  scope: SessionScope,
  query: string,
  login: string | null | undefined,
): SessionSummary[] {
  const targetIds = new Set(itemTargets(query).map((t) => t.sessionId));
  return sortByActivity(
    sessions.filter((s) => targetIds.has(s.session_id) || (inScope(s, scope, login) && matchesText(s, query))),
  );
}

// --- Jump to an item's session ----------------------------------------------------------------------

/**
 * The GitHub item(s) a search-box query names, as jump targets — "paste a URL, land in that item's
 * Strandly session". A URL (or `owner/repo/pull/42` path) carries its kind, so it yields exactly one
 * target; a bare `owner/repo#42` doesn't, so it offers both the PR and the issue. Anything else
 * (plain text, a login) yields none and the box stays a text filter.
 */
export function itemTargets(query: string): ParsedItem[] {
  const raw = query.trim();
  if (!raw) return [];
  const asUrl = parseItemInput(raw);
  if (!asUrl) return [];
  if (/\/(pull|pulls|issues?|discussions?)\/\d+/i.test(raw)) return [asUrl];
  return [asUrl, parseItemInput(raw, "issue")].filter((t): t is ParsedItem => !!t);
}

const GITHUB_URL = /https?:\/\/github\.com\/\S+|(?<![\w.-])github\.com\/\S+/i;

/**
 * Split a composer draft that contains a GitHub item URL into the item and the rest of the message
 * (the URL removed, whitespace collapsed). The sheet sends the rest as the first message of that
 * item's session. Only full URLs count here — `owner/repo#42` inside prose is too often just prose.
 */
export function splitItemDraft(draft: string): { item: ParsedItem; message: string } | null {
  const m = draft.match(GITHUB_URL);
  if (!m) return null;
  const item = parseItemInput(m[0]);
  if (!item) return null;
  const message = (draft.slice(0, m.index) + " " + draft.slice((m.index ?? 0) + m[0].length)).replace(/\s+/g, " ").trim();
  return { item, message };
}

/** The `viewing` half of the invocation context for a jump target — so the chat knows the item too. */
export function viewingFor(item: ParsedItem): ViewingRef {
  return {
    type: item.kind === "pr" ? "pr" : item.kind === "disc" ? "discussion" : "issue",
    repo: item.repo,
    number: item.number,
    url: itemUrl(item),
  };
}

/** The kind chip for an item session ("Issue" / "PR" / "Discussion"), or null for the rest. */
export function itemKindLabel(id: string): string | null {
  const parsed = parseSessionId(id);
  if (!parsed) return null;
  return parsed.kind === "pr" ? "PR" : parsed.kind === "disc" ? "Discussion" : "Issue";
}

// --- Paging ---------------------------------------------------------------------------------------

/** Rows rendered per "Show more" on the sessions list (README.md "Pagination & limits"). */
export const SESSIONS_PAGE_SIZE = 50;

/**
 * The visible window of an already-FILTERED list: the first `shown` rows plus how many "Show more"
 * would add. Paging is client-side on purpose — GET /api/sessions serves one fixed recent window
 * (handler.py SESSIONS_WINDOW, 200 ledger rows grouped into sessions) with no cursor, so the whole
 * set is already in memory; what we bound is the DOM, not the fetch. Filtering runs on the full set
 * first (see filterSessions); the window applies to its result.
 */
export function pageWindow<T>(rows: T[], shown: number, pageSize: number = SESSIONS_PAGE_SIZE): { rows: T[]; remaining: number; nextStep: number } {
  const n = Math.max(0, Math.min(shown, rows.length));
  const remaining = rows.length - n;
  return { rows: n === rows.length ? rows : rows.slice(0, n), remaining, nextStep: Math.min(pageSize, remaining) };
}
