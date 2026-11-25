/**
 * Web Share Target launch — the installed portal shows up in Android's share sheet, so a GitHub
 * issue / PR link shared from Slack, the GitHub app, Chrome, … opens straight on its page here.
 *
 * Why share-and-not-click: a PWA cannot claim taps on `github.com` links. Android App Links need an
 * `assetlinks.json` served FROM github.com (GitHub's own app holds that), Chrome's `scope_extensions`
 * needs github.com to publish an origin association, and the manifest `handle_links` member only
 * covers links inside OUR scope. The share sheet is the one standards path that works from any app,
 * at the cost of one extra tap (long-press → Share → Strands).
 *
 * The manifest declares a GET share target (`SHARE_TARGET` in shareTarget.ts, emitted by
 * vite.config.ts) whose action is the app root, with namespaced params so a share launch is
 * unmistakable from any other query string:
 *
 *     https://<portal>/?share_url=…&share_text=…&share_title=…
 *
 * Android has no URL field on a share intent — the link usually arrives in `text` (Chrome, Slack),
 * sometimes `url` (apps that fill both), occasionally only `title` — so all three are scanned, in
 * that order, for the first GitHub item reference (`parseItemInput` finds a URL inside free text).
 *
 * The app is hash-routed, so the query lives OUTSIDE the router: `main.tsx` calls
 * {@link consumeShareLaunch} once before the router mounts; it rewrites the URL to `#<route>` (drops
 * the query so a reload / back can't replay the share) and returns what happened for a notification.
 * This module's resolver is PURE and tested; only `consumeShareLaunch` touches `history`.
 */
import { ROUTES, itemLink, itemSessionLink } from "./links";
import { parseItemInput, type ParsedItem } from "./sessionId";
import { SHARE_PARAMS } from "./shareTarget";

export { SHARE_PARAMS };

export interface ShareLaunch {
  /** In-app route (hash path) to land on. */
  to: string;
  /** The GitHub item the share resolved to, or null when nothing in the share was one. */
  item: ParsedItem | null;
  /** The shared payload, for the "couldn't recognize" notification. */
  shared: string;
}

/** True when the query string carries any share-target param (i.e. this load IS a share launch). */
export function isShareLaunch(search: string): boolean {
  const q = new URLSearchParams(search);
  return Object.values(SHARE_PARAMS).some((k) => q.has(k));
}

/**
 * Resolve a share launch from `location.search`, or null when this isn't one.
 *
 *   - issue / PR → its detail page (which carries "Open in Strandly");
 *   - discussion → its `gh-…-disc-n` Strandly session (no in-app discussion page);
 *   - anything else → the Strandly tab, `item: null` so the caller can say so.
 */
export function resolveShareLaunch(search: string): ShareLaunch | null {
  if (!isShareLaunch(search)) return null;
  const q = new URLSearchParams(search);
  const fields = [SHARE_PARAMS.url, SHARE_PARAMS.text, SHARE_PARAMS.title]
    .map((k) => (q.get(k) ?? "").trim())
    .filter(Boolean);
  const shared = fields.join(" ");

  for (const field of fields) {
    const item = parseGitHubItem(field);
    if (item) return { to: shareRoute(item), item, shared };
  }
  return { to: ROUTES.strandly, item: null, shared };
}

/** The route a shared item lands on. */
export function shareRoute(item: ParsedItem): string {
  if (item.kind === "disc") return itemSessionLink(item.repo, item.kind, item.number);
  return itemLink(item.repo, item.kind, item.number);
}

/**
 * A github.com item URL inside free text: optional scheme, optional `www.`, any case, `owner/repo`,
 * a kind segment and the number. Anything after the number (`/files`, `?x=y`, `#issuecomment-…`,
 * trailing punctuation) is ignored. Anchored on the host so a non-GitHub URL that happens to have an
 * `/a/b/issues/5` shape can't be read as one, and so `notgithub.com` isn't github.com.
 */
const GITHUB_ITEM_URL =
  /(?:^|[^\w.-])((?:https?:\/\/)?(?:www\.)?github\.com\/[^/\s]+\/[^/\s]+\/(?:pulls?|issues?|discussions?)\/\d+)/i;

/**
 * Only the URL form counts in a share. `parseItemInput` also accepts a bare `owner/repo#42` (kind
 * unknown → defaults to PR), but a share payload is free text where "a/b#1" is noise far more often
 * than a reference — so the URL is cut out first and only that substring is handed to the parser.
 */
function parseGitHubItem(text: string): ParsedItem | null {
  const m = text.match(GITHUB_ITEM_URL);
  return m ? parseItemInput(m[1]) : null;
}

/**
 * Boot-time hook (call ONCE before the router mounts): if this load is a share launch, rewrite the
 * URL to the resolved hash route — same pathname, query dropped — and return the launch.
 */
export function consumeShareLaunch(loc: Location = window.location, hist: History = window.history): ShareLaunch | null {
  const launch = resolveShareLaunch(loc.search);
  if (!launch) return null;
  hist.replaceState(null, "", `${loc.pathname}#${launch.to}`);
  return launch;
}
