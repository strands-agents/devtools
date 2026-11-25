/**
 * Canonical GitHub-item session ids — the browser-side twin of the harness'
 * `strandly.core.session_ids.canonical_session_id`. A conversation is keyed on the GitHub item it
 * happens on, so the SAME agent + AgentCore Memory thread is reachable no matter how a run was
 * triggered. Format:
 *
 *     gh-<owner>-<repo>-<h8>-<kind>-<number>      kind in {issue, pr, disc}
 *
 * where `<owner>-<repo>` is the lowercased `owner/repo` with its slash replaced by a hyphen and
 * `<h8>` is the first 8 hex characters of the SHA-256 of the lowercased `owner/repo` (harness
 * `repo_digest`), so `Acme/X` and `acme/x` share one id.
 * The slug alone is ambiguous (`acme/labs-tool` and `acme-labs/tool` both read `acme-labs-tool`);
 * the digest keeps their sessions apart. This module is PURE (no fetching, no clock) so the URL→id
 * parser and its inverse are trivially testable — the "talk to Strandly about this item" entry
 * point converts a pasted GitHub URL (or `owner/repo#num`) into the id; the inverse recovers what
 * it can for display.
 *
 * Note on the inverse: the slug cannot be split back into a unique `owner/repo`, and the digest
 * only confirms a guess. `parseSessionId` therefore returns the hyphenated `repoSlug` (not
 * `owner/repo`) plus the kind and number, which is all a display label needs. Where the true URL is
 * required, callers already carry it (the ledger row's `target`, the work product's `target_url`).
 */

import { sha256Hex } from "./sha256";

export type SessionKind = "issue" | "pr" | "disc";

export const SESSION_KINDS: SessionKind[] = ["issue", "pr", "disc"];

/** URL path segment (`pull` / `issues` / `discussions`) → session kind. */
const PATH_KIND: Record<string, SessionKind> = {
  pull: "pr",
  pulls: "pr",
  issues: "issue",
  issue: "issue",
  discussions: "disc",
  discussion: "disc",
};

/** Human label for a kind. */
export function kindLabel(kind: SessionKind): string {
  return kind === "pr" ? "Pull request" : kind === "disc" ? "Discussion" : "Issue";
}

/** `owner/repo` → the lowercased `owner-repo` slug used in a session id (harness `repo_slug`). */
function slug(repo: string): string {
  return repoName(repo).replace(/\//g, "-");
}

/** Trimmed and lowercased: GitHub names are case-insensitive, so one repository has one id. */
function repoName(repo: string): string {
  return repo.replace(/^\/+|\/+$/g, "").toLowerCase();
}

/** The first 8 hex characters of the SHA-256 of the lowercased `owner/repo` (harness `repo_digest`). */
export function repoDigest(repo: string): string {
  return sha256Hex(repoName(repo)).slice(0, 8);
}

/** The canonical `gh-<owner>-<repo>-<h8>-<kind>-<number>` id for a GitHub item. */
export function itemToSessionId(repo: string, kind: SessionKind, num: number | string): string {
  return `gh-${slug(repo)}-${repoDigest(repo)}-${kind}-${num}`;
}

export interface ParsedItem {
  repo: string; // "owner/repo"
  kind: SessionKind;
  number: number;
  sessionId: string;
}

/**
 * Parse a GitHub item reference into `{repo, kind, number, sessionId}`, or null if it isn't one.
 *
 * Accepts, in order of specificity:
 *   - a full/relative GitHub URL with a kind path segment
 *     (`https://github.com/owner/repo/pull/42`, `github.com/owner/repo/issues/42`);
 *   - a bare `owner/repo#42` or `owner/repo 42`, whose kind can't be read from the text, so it takes
 *     `defaultKind` (PRs are the common "talk to Strandly" case, hence the default).
 */
export function parseItemInput(input: string, defaultKind: SessionKind = "pr"): ParsedItem | null {
  const raw = (input || "").trim();
  if (!raw) return null;

  // A URL (or bare `owner/repo/<kind>/<n>`) — the reliable path: kind comes from the path segment.
  const urlMatch = raw.match(
    /(?:github\.com\/)?([^/\s]+)\/([^/\s]+)\/(pull|pulls|issues|issue|discussions|discussion)\/(\d+)/i,
  );
  if (urlMatch) {
    const [, owner, name, seg, num] = urlMatch;
    const kind = PATH_KIND[seg.toLowerCase()];
    if (kind) return build(`${owner}/${name}`, kind, Number(num));
  }

  // A bare `owner/repo#42` / `owner/repo 42` — kind is not in the text, so fall back to defaultKind.
  const refMatch = raw.match(/^([^/\s]+)\/([^/\s#]+)\s*#?\s*(\d+)$/);
  if (refMatch) {
    const [, owner, name, num] = refMatch;
    return build(`${owner}/${name}`, defaultKind, Number(num));
  }

  return null;
}

function build(repo: string, kind: SessionKind, number: number): ParsedItem {
  return { repo, kind, number, sessionId: itemToSessionId(repo, kind, number) };
}

/** The canonical GitHub URL for a parsed item (`pull` / `issues` / `discussions` path by kind). */
export function itemUrl(item: ParsedItem): string {
  const seg = item.kind === "pr" ? "pull" : item.kind === "disc" ? "discussions" : "issues";
  return `https://github.com/${item.repo}/${seg}/${item.number}`;
}

export interface ParsedSessionId {
  /** The hyphenated `owner-repo` slug (cannot be split back into `owner/repo` — see module header). */
  repoSlug: string;
  /** The `owner/repo` digest, or null for an id in the retired name-only format. */
  digest: string | null;
  kind: SessionKind;
  number: number;
}

/**
 * Inverse of {@link itemToSessionId} — recover `{repoSlug, digest, kind, number}` from a canonical
 * id, or null if it isn't one. Ids in the retired `gh-<owner>-<repo>-<kind>-<number>` format still
 * parse (with a null `digest`) so older sessions keep their display label.
 */
export function parseSessionId(sessionId: string): ParsedSessionId | null {
  const id = (sessionId || "").trim();
  if (!id.startsWith("gh-")) return null;
  const rest = id.slice(3);
  // From the right: number is trailing digits, kind precedes it, then the optional 8-hex digest.
  const m = rest.match(/^(.+?)(?:-([0-9a-f]{8}))?-(issue|pr|disc)-(\d+)$/);
  if (!m) return null;
  const [, repoSlug, digest, kind, num] = m;
  if (!repoSlug) return null;
  return { repoSlug, digest: digest ?? null, kind: kind as SessionKind, number: Number(num) };
}

/** A short display label for a session id (`owner-repo#42`), falling back to the raw id. */
export function sessionLabel(sessionId: string): string {
  const parsed = parseSessionId(sessionId);
  return parsed ? `${parsed.repoSlug}#${parsed.number}` : sessionId;
}

// --- Portal-minted (item-less) sessions ----------------------------------------------------------

/** The id prefix for sessions a given user starts from the portal: `portal-<login>-`. */
export function portalSessionPrefix(login: string): string {
  return `portal-${sanitizeLogin(login)}-`;
}

/**
 * A fresh id for an item-less conversation started from the portal:
 * `portal-<login>-<YYYYMMDD>-<rand8>`. The login makes "my sessions" a prefix filter on the sessions
 * list; `portal-` keeps it disjoint from the per-item `gh-…` ids. Anonymous (no token) users get
 * `portal-anon-…`. Slash-free and URL-safe; the runtime pads short ids to its 33-char minimum.
 */
export function newPortalSessionId(login: string | null | undefined, now: Date = new Date()): string {
  const stamp = now.toISOString().slice(0, 10).replace(/-/g, "");
  const rand =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID().replace(/-/g, "").slice(0, 8)
      : Math.random().toString(16).slice(2, 10);
  return `${portalSessionPrefix(login || "anon")}${stamp}-${rand}`;
}

/** True when `sessionId` was minted from the portal (any user). */
export function isPortalSession(sessionId: string): boolean {
  return sessionId.startsWith("portal-");
}

/** The login a portal session id was minted for, or null for `gh-…` / foreign ids. */
export function portalSessionOwner(sessionId: string): string | null {
  const m = sessionId.match(/^portal-([a-z0-9][a-z0-9-]*?)-\d{8}-[0-9a-f]{8}$/i);
  return m ? m[1] : null;
}

/** GitHub logins are already `[A-Za-z0-9-]`; lowercase + strip anything else so the id stays URL-safe. */
export function sanitizeLogin(login: string): string {
  return login.toLowerCase().replace(/[^a-z0-9-]/g, "") || "anon";
}
