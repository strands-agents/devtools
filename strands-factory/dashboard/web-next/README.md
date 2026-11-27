# Strands Maintainer Portal (web-next)

The maintainer's workspace with the Strandly agent: **Today · Strandly · Issues · PRs · Schedules · Settings**.
React 18 + Vite + TypeScript (strict) + Mantine v7, mobile-first, installable as a PWA.

It is a standalone npm package under `dashboard/web-next/` (not wired into pytest / ruff / CDK
beyond the Dashboard stack's deploy). It is the only dashboard UI; see `dashboard/README.md` for the
API and hosting.

## Quick start

```bash
cd dashboard/web-next
npm ci
npm run dev          # http://localhost:5174 — MOCK data by default, no backend needed
npm run build        # tsc -b (strict) + vite build → dist/ (incl. sw.js + manifest.webmanifest)
npm test             # vitest — unit tests live next to the code (*.test.ts)
npm run preview      # serve the production build (the service worker only registers on a build)
```

The check is `npm ci && npm run build && npm test` from a clean clone, so keep `package-lock.json` in sync.

## Information architecture

Routes are hash-based (`HashRouter`) so deep links survive a hard refresh on static hosting. Build
paths with `src/lib/links.ts` — never by hand.

| Tab | Route | What it is |
|---|---|---|
| **Today** | `#/today` | Everything that needs *you*, newest first: mentions, your PRs, Strandly's PRs, assigned, review-requested. One org-scoped GitHub search, deduped by `repo#number` (reasons become chips), quick-filter row. |
| **Strandly** | `#/strandly`, `#/strandly/:sessionId` | Sessions list (Mine / Portal / Items / All + text filter that also matches `@login`) and the chat. **Mine** = sessions carrying your login: `portal-<you>-…` by exact owner, plus any non-`gh-` id from another surface (Slack, CLI) with your login as a whole token. Paste a GitHub issue/PR URL (or `owner/repo#n`) into the filter box and a top row (or Enter) jumps to that item's `gh-…` session; the item's existing session is listed in any scope. New sessions are minted as `portal-<login>-<yyyymmdd>-<rand8>`. Mobile: composer-first FAB → bottom sheet; a URL in the draft routes the message to the item's session. |
| **Issues** | `#/issues`, `#/issues/:owner/:repo/:number` | Open issues across the scoped orgs with filters (All / Involved / Assigned / Mine / Mentioned / Untriaged), text search, repo facets; detail with Markdown body, comments, assign / labels / close-reopen / comment, **Open in Strandly**, "Strandly's take" card. |
| **PRs** | `#/pulls`, `#/pulls/:owner/:repo/:number` | Open PRs with filters (All / Mine / Review requested / Strandly / Involved / Mentioned), draft / decision / CI chips; detail with **Conversation · Files · Checks**, a mobile-friendly unified diff (own parser, syntax colors by file extension, wrap toggle, collapsible files, inline line comments), review drawer (Approve / Request changes / Comment), **Open in Strandly**. |
| **Schedules** | `#/schedules`, `#/schedules/:id` | Strandly's scheduled jobs (`/api/schedules*`, see [dashboard/README.md](../README.md#scheduled-jobs)): each job's schedule in words, next run, last run (linked to its session), creator, sync state and an enabled switch; **New schedule** / edit with a live preview of the next 5 runs (once at a date + time, or daily / weekly / every N hours / raw `cron`/`rate`, in any timezone), skill, session mode and an optional Slack / GitHub notify target. The job page has run now, pause / resume, delete, past runs and the revision history with revert. |
| **Settings** | `#/settings` | GitHub token, **Strandly context**, Scope (orgs, maintainers team), Appearance (dark / light / auto, install), Account (portal sign-in). |

Older bookmarked routes (`/chat/:id`, `/sessions`, `/reviews`, `/triage`, …) redirect to these tabs
(`src/App.tsx`).

### Layout

```
src/
  App.tsx                     routes + redirects
  components/
    AppShell.tsx              header · desktop rail · mobile bottom tab bar (safe-area aware)
    AgentChat.tsx, chatView.ts  the chat panel (windowed history, see below)
    FilterChips.tsx           the one quick-filter row (Today / Issues / PRs)
    NoTokenState.tsx          the one "add a GitHub token" empty state
    LabelPill.tsx             GitHub label pills (dot / filled)
    Section.tsx, StateBlock.tsx, Markdown.tsx
  features/
    today/   strandly/   issues/   pulls/   schedules/   settings/     page + data.ts (pure, tested) + CSS module
  github/   GitHubClient (viewer + generic graphql/rest seams + the human writes), token store, context
  api/      PortalApi (dashboard API: sessions, chat, work-product lookups, schedules) — live + mock
  lib/      links, scope, sessionId, userContext, format, labelColor, chat (stream protocol), …
```

Feature modules own their GitHub queries (`features/<area>/data.ts` on `client.graphql` /
`client.rest`); `src/github/client.ts` stays view-agnostic.

## Design notes

- **Dark-first, fewer lines.** Surfaces are separated by tone (`--portal-page` / `--portal-surface`
  / `--portal-surface-2` in `styles.css`), not borders: Card / Paper default to `withBorder={false}`
  (`theme.ts`). Accent is the `frog` palette; `primaryShade` differs per scheme so the accent reads
  on both dark and light. Don't add `withBorder` / `Divider` without a strong reason.
- **Mobile first (360px).** Bottom tab bar under the `sm` breakpoint; content pads for it
  (`--portal-bottom-nav-h` + safe-area). Tap targets ≥ 40px, text ≥ 13px, lists are cards not
  tables, no horizontal overflow except inside a diff body. The chat page is `100dvh` with a sticky
  safe-area composer; the PR review bar and the sessions FAB sit above the tab bar. `index.html`
  sets `interactive-widget=resizes-content` so Android's keyboard shrinks the layout viewport and
  fixed / `100dvh` surfaces (composer, bottom sheet) rise with it.
- **Back goes back.** Detail-page back buttons use `lib/useBack`: history back when react-router
  stamped an in-app entry behind us (`history.state.idx > 0`), else the section root — so PR →
  "Open in Strandly" → back lands on the PR, while a deep link / PWA launch still has somewhere to go.
- **A stored transcript renders like a live one.** The SDK stores one agent turn as several Memory
  messages (assistant text + `toolUse`; a user-role message of `toolResult`s; the follow-up).
  `chatView.foldToolTurns` folds them back into the single bubble a stream draws — results pair with
  their call by tool-use id (`groupBlocks`), never by position — on the first page and at every
  older-page seam (`prependPage`). A call with no stored result shows a dimmed dash, not a spinner.
- **Newest first, deduped by item.** Every list sorts by `updatedAt` desc; a GitHub item appears
  once with its reasons as chips.
- **Writes are the user's own words/decisions** (comment, review, assign, label, close) under the
  user's token. The portal never posts AI content with the user's token.

## Strandly context (`invocationContext.userContext`)

Settings → **Strandly context** is free text (≤ 1500 chars, `src/lib/userContext.ts`) stored in
`localStorage`. `AgentChat` stamps it into every chat turn as `invocationContext.userContext`,
next to `user` (your GitHub login) and `viewing` (the issue/PR you came from). The runtime already
renders the whole `invocationContext` as a labeled ambient block, so this is **purely additive — no
backend change**.

## Pagination & limits (the contract)

Owner's rule: *"be careful with pagination and limits, I don't want pages to bloat."* Nothing renders
an unbounded set; every list is a fixed page or a window of what was fetched.

| Surface | Fetch | Bound + how the UI says so |
|---|---|---|
| Today | one GraphQL query, 5 aliased `search(first: 40)` | caption "Showing the newest 40 for … — older items are on GitHub" when a bucket's `issueCount` exceeds the page; poll every 2 min, paused while hidden, ≥ 30 s between visibility refetches |
| Issues list | `search(first: 30, after:)`; rows `labels(first: 10)`, `assignees(first: 5)` | "Load more" (cursor), pages merged by `repo#number` |
| Issue detail | `comments(last: 50)` + `totalCount` + `pageInfo`; `labels(first: 20)`, `assignees(first: 10)`, repo `labels(first: 100)` | "Load N earlier comments" walks `before:` 50 at a time; a post-write refresh re-reads the newest page only and keeps loaded earlier pages |
| PRs list | `search(first: 30, after:)`; `labels(first: 10)`, `commits(last: 1)` for CI | "Load more" (cursor) |
| PR detail | `comments(last: 50)` + `reviews(last: 50)` (each `totalCount` + `pageInfo`); `latestReviews(first: 20)`, `reviewRequests(first: 10)`, `statusCheckRollup.contexts(first: 100)` | one "Load earlier · N not shown" walks both `before:` cursors (per connection, only while it has one) |
| PR files | REST `/pulls/:n/files?per_page=100` paged | capped at **300 files** with a "showing first 300" caption; files collapsed by default when > 20 |
| PR diff syntax colors | — (`features/pulls/highlight.ts`, lowlight `common` grammars already bundled for Markdown) | each side highlighted as one document, then split per line; a side over **200 K chars** or any line over **2 000 chars** stays plain; computed once per patch (`useMemo`), only for expanded files |
| Strandly sessions | `GET /api/sessions` — fixed server window (`SESSIONS_WINDOW` = 200 ledger rows → ≤ 200 summaries, no cursor) | renders **50** + "Show N more · M not shown"; filters run on the full set, the window on the result |
| Strandly chat | `GET /api/sessions/{id}?limit=50` → newest 50 + `next_cursor` + `history_boundary` | "Load older messages" (button + near-top auto-load, throttled 800 ms, stops after an error) prepends the page before the cursor with the reader's scroll position anchored; pages dedup by server message id; 409 `cursor_expired` → "History changed — reload". Server clamp 1–200; no params = the full transcript |
| Strandly's take / review card | `GET /api/workproducts?session_id=…&limit=20` (newest-first; handler clamps to 100) | newest draft only |
| Chat polling | `GET /api/chat?since=<seq>` deltas | never refetches the transcript |
| Tool rows in chat | — | collapsed preview reads ≤ 2 KB → 96 chars; expanded detail clipped at 32 KB with a size note; rows are `React.memo` |

No virtualization dependency: a 50–300 row window with memoized rows is fine; add one only with a
measured need.

## GitHub read cache (10 minutes)

Owner's rule: *"I don't want to see a loading screen every time I switch pages — cache 10–15 min
unless I specifically refresh."* `src/github/readCache.ts` is a read-through TTL cache owned by the
`LiveGitHubClient` (so a token change starts empty); it lives in memory only — nothing is persisted,
and a full page reload / PWA cold start fetches again.

| | |
|---|---|
| What is cached | every GraphQL **query** and REST **GET** through the client, keyed by document + variables (key-order-stable) or by path + `accept` header — Today's feed, Issues/PRs first pages and cursor pages, issue/PR detail, earlier-comment pages, PR file pages. A read whose answer depends on state the request doesn't name adds it to the key via `vary` — PR file pages are keyed by the head commit, so a diff fetched under an older head is never shown under a newer one |
| TTL / size | **`GITHUB_READ_TTL_MS` = 10 min**; at most 100 entries (oldest evicted first). Concurrent reads of one key share one request; errors are never cached |
| Bypass (`fresh: true`) | the **Refresh** button on Today, Issues, PRs, an issue and a PR; Today's 2-minute poll and tab-return refetch. After a fresh first page, that list's "Load more" pages are fetched fresh too (a cached page 2 under a just-refreshed page 1 would skip rows). Fresh results replace the cached copy |
| Invalidation | **every write through the client clears the whole cache** (comment, review, line comment, assign, label, close/reopen) — the quiet refetch after a write therefore always hits the network, and the lists reflect what you just did. Whole-cache on purpose: one label change touches the issue, every Issues search it appears in and the Today feed, and mapping writes to affected keys is exactly the bookkeeping that goes stale; writes are rare human actions, so the cost is one refetch per page visited afterwards |
| What the UI says | Today's "Updated x ago" is the time of the real fetch, not of the cache hit |
| Not cached | `viewer` (token validation, once per client), the dashboard API (`/api/*` — sessions, chat, work products) — those are live by design |

Rule for new reads: call `client.graphql` / `client.rest("GET", …)` and you are cached; pass
`{ fresh: true }` only from an explicit user refresh or a poll tick, and `{ vary }` when the same
request can legitimately answer differently (a PR's files ↔ its head commit). Never cache a mutation
(`isCacheableGraphql`), and never add a second cache layer in a feature.

## PWA + hosting

- `vite-plugin-pwa` generates `manifest.webmanifest` + a Workbox `sw.js` that precaches the built
  app shell. **`config.json` is excluded from the precache and the navigation fallback**, and no
  cross-origin request (dashboard API, api.github.com, Cognito) is ever cached — Workbox only handles
  same-origin precache entries. New builds **never reload a tab on their own** (`registerType:
  "prompt"`): the new worker waits, a persistent "Update available — Reload to update" notification
  offers the switch, and it also activates once every portal tab is closed. (autoUpdate would
  reload every open tab on activation and erase an unsent chat draft.)
- Icons (`public/icons/`): the strands-agents frog — `icon-192/512`, maskable variants,
  `apple-touch-icon`. `index.html` carries `theme-color`, `viewport-fit=cover` and the Apple
  web-app metas; Settings → Appearance exposes the browser's install prompt where available.
- **Share target** (`share_target` in the manifest — defined once in `src/lib/shareTarget.ts` — and
  `src/lib/shareLaunch.ts`): once installed on Android (Chrome; iOS Safari has no share-target
  support), the portal is a destination in the share sheet. Share a GitHub **issue / PR** link from any app
  (Slack: long-press the link → Share; the GitHub app or Chrome: Share) → **Strands** opens it on its
  detail page; a **discussion** link opens its `gh-…-disc-n` Strandly session; anything else lands on
  the Strandly tab with a "not a GitHub link" notice. The launch is a GET onto the app root with
  namespaced params (`?share_url&share_text&share_title` — Android usually puts the URL in `text`),
  resolved **before the router mounts** and rewritten to the `#/…` route so a reload can't replay it.
  No server change: the query sits outside the hash route, so static hosting serves `index.html`.
- **Why share, not click:** a PWA cannot claim taps on `github.com` links. Android App Links need an
  `assetlinks.json` served *from github.com* (GitHub's own app holds that), Chrome's `scope_extensions`
  needs github.com to publish an origin association, and the manifest `handle_links` member only
  covers links inside **our** scope. We set `handle_links: "preferred"` + `launch_handler:
  navigate-existing` so *portal* URLs (a `#/pulls/…` link pasted in Slack) open the installed app and
  reuse its window. A one-tap "Open in portal" from Slack would have to come from the Slack side
  (an unfurl button on GitHub links pointing at a portal URL).
- Hosting: static assets behind CloudFront from the portal's S3 bucket, `base: "./"`
  so asset URLs are relative, `HashRouter` so no SPA rewrite rule is needed. `sw.js` and
  `manifest.webmanifest` are served from the same bucket as `index.html`.
- **Deploy the API and the frontend together**: the chat pages history with
  `GET /api/sessions/{id}?limit&before` ([transcript pagination](../README.md#opt-in-session-transcript-pagination)). A handler that ignores the
  query returns the whole transcript, which the client renders without paging.

## Mock vs. live

The data source is chosen at load time: a runtime `/config.json` next to `index.html` (written by
CDK: `dataSource`, `apiBase`, Cognito fields) overrides the build-time env
(`.env.example` → `.env`, `VITE_DATA_SOURCE=mock|live`, `VITE_API_BASE=…`). **Default is `mock`.**
`live` without an `apiBase` falls back to mock with a red "misconfigured" banner (`src/api/index.ts`).

In `mock`, `MockApi` (`src/api/mock.ts`, fixtures in `fixtures.ts`) serves canned sessions, a
130-message paginated transcript for the fixture sessions, and a streamed canned reply to any chat
message — so the Strandly tab, windowing and "Load older" all work with no backend. Schedules are
kept in memory. Preview supports daily/weekly presets, rates and one-shots in the job's IANA timezone,
including DST transitions; the live API validates the full Scheduler expression syntax. Completed
one-shots stay completed after edits, even when given a future time: save, then explicitly Resume.
An optional browser regression exercises create/edit/preview/run/revert and completed-job edits:
with Playwright and Chromium installed separately, run `node scripts/schedules-browser.mjs` from
this directory (`PLAYWRIGHT_BROWSERS_PATH` may point to an existing browser installation).
It starts its own local Vite server and blocks all non-local requests. The GitHub side
in mock is a demo identity only: Today / Issues / PRs show their "add a GitHub token in Settings"
state (the mock client rejects `graphql`/`rest`). **A saved token always wins** — add one and the
GitHub tabs go live even while the dashboard API is still mock.

## Auth — browser-only GitHub token

The portal authenticates the **user** with a GitHub token pasted into **Settings → GitHub token**.

**HARD RULE: the token lives in the browser (`localStorage`) ONLY and is NEVER sent to the dashboard
API / server.** Every GitHub call that uses it goes browser → `https://api.github.com` directly.

The token is used for exactly three things:

1. **Identity** — the current user (login/name) from the GitHub `viewer` query; this is also what
   makes Today / Issues / PRs *yours* (`mentions:<me>`, `author:<me>`, `review-requested:<me>`, …)
   and what `portal-<login>-…` session ids carry.
2. **Reads** — the org-scoped searches behind Today / Issues / PRs, issue and PR detail, and the PR
   files (works for private repos, 5000/hr authenticated).
3. **Your own writes** — comments and reviews you typed yourself, assign, add/remove labels,
   close/reopen (your words and decisions, under your identity).

The token is **never** used to post AI-generated content; the agent posts through its own
credentials, and chatting with it (`/api/chat`) carries no GitHub token at all.

### Where the boundary is enforced

- `src/github/token.ts` — the only GitHub-token store (localStorage). `src/api/client.ts` does not
  import it, so the token has no path onto a dashboard-API request.
- `src/github/client.ts` — the only place the token is read for a network call; every target is
  `https://api.github.com` (GraphQL + REST). Feature queries build on its `graphql` / `rest` seams.
  Its read cache (`src/github/readCache.ts`) holds response bodies in memory only — never the token,
  never on disk.
- `src/auth/dashboardToken.ts` — the **separate** dashboard bearer seam (Cognito/JWT). `src/api/client.ts`
  attaches this and only this.
- `src/github/GitHubContext.tsx` — resolves `viewer` from the token and exposes the client + save/
  clear; `src/auth/auth.tsx` `useUser()` derives identity from it. The login (never the token) is
  the only identity that travels — into `invocationContext.user` on chat turns.

### Recommended scopes + XSS caveat

Use a **fine-grained PAT** scoped to the orgs in Settings → Scope, with the narrowest access that
lets you review: Pull requests (read + write), Issues (read + write), Metadata (read). `localStorage`
is readable by any script on the origin, so a successful XSS could leak the token — keep scopes
minimal and org-limited, and **Clear token** when done on a shared machine.

## Known limitations

- Issue and PR detail pages are single-column on desktop too.
- Inline review-thread comments (code comments) are not shown in the PR Conversation tab.
- The newest history page can START with a turn's tool-result message (its calls are on the page
  before); it renders as a user-side result row until "Load older" fetches the calls and the seam
  folds. A server-side page boundary that never splits a turn would remove that one frame.
- Portal-session chat titles are the generic "Conversation" (the route only has the id; the list has
  the description).
