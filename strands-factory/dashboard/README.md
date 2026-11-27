# Strands Factory dashboard

The maintainer-facing side of a Strands Factory deployment: the **Maintainer Portal** (`web-next/`), the
**API** it and `strandly-mcp` call (`api/handler.py`, plus the scheduled-jobs routes in the harness's
`ops/lambdas/schedules_api`), and the Cognito user pool both sign in
with. All of it is one CDK stack, `Strandly-Dashboard-<env>` (`infra/stacks/dashboard_stack.py`).

```
Portal (browser, web-next)          strandly-mcp / `strands-factory login`
   │  Cognito Hosted UI, OAuth2 Authorization-Code + PKCE
   ▼
API Gateway HTTP API (Cognito JWT authorizer on every route)
   ▼                                      ▼ /api/schedules*
ReadApiFn (api/handler.py)              SchedulesApiFn (harness ops/lambdas/schedules_api)
   ├─► AgentCore Runtime      chat         ├─► ScheduleService: schedules table + jobs Scheduler group
   ├─► AgentCore Memory       transcripts  ├─► AgentCore Runtime   run now
   ├─► S3 sessions bucket     snapshots    └─► DynamoDB run-ledger past runs of a job
   ├─► DynamoDB run-ledger    sessions
   └─► DynamoDB work products "Strandly's take"
```

Operational views (alarm state, run counts, durations, tool errors, sandbox signals) are not in the
portal: they are on the `<name>-<env>-ops` CloudWatch dashboard (see
[Operations](../docs/operations.md#observability)), and per-run traces are in CloudWatch GenAI
Observability.

## Layout

```
dashboard/
  web-next/        the Maintainer Portal (React + Vite + Mantine); see web-next/README.md
  api/handler.py   the read Lambda: a pure route() + the AWS readers it is wired to
                   (/api/schedules* is a separate function: harness/src/strandly/ops/lambdas/schedules_api)
```

## API

| Route | Used by | What it does |
|---|---|---|
| `POST /api/chat` | portal, MCP | Launch a turn on the runtime for a session (detached; returns a `taskId`), or `{"action": "cancel"}`. An optional `model` (a harness model string) and `effort` go to the runtime's harness model resolution; a choice it refuses, or one that is not a short string, is a 400 with the reason |
| `GET /api/chat?task_id&session_id&since` | portal, MCP | Poll the run's live events from cursor `since` |
| `GET /api/chat?whoami=1` | portal, MCP, CLI | The person bound to the caller |
| `GET /api/sessions` | portal, MCP, internal site (invokes the Lambda directly) | Recent sessions from the run-ledger, newest-active first |
| `GET /api/sessions/{id}` | portal, MCP, internal site (invokes the Lambda directly) | A session's transcript (paginated with `limit`/`before`; full form without them) |
| `PATCH /api/sessions/{id}` | portal | `{"title": string \| null}`: rename the session; empty or `null` clears the title |
| `GET /api/workproducts?session_id=&kind=` | portal | The work products a session produced |
| `GET /api/schedules` | portal | Scheduled jobs with their next runs, plus the skills, session modes and limits the form needs |
| `POST /api/schedules` | portal | Create a job |
| `POST /api/schedules/preview` | portal | Validate a job spec: the errors a save would raise, else the next 5 runs |
| `GET /api/schedules/{id}` | portal | One job and its revisions (a deleted job: its revisions, `job: null`) |
| `PUT /api/schedules/{id}` | portal | Edit fields (`expected_version` required) |
| `DELETE /api/schedules/{id}` | portal | Delete the job and its schedule (revisions stay) |
| `POST /api/schedules/{id}/pause` · `/resume` · `/run` · `/revert` | portal | Pause, resume, run now, restore revision `rev` |
| `GET /api/schedules/{id}/runs?days=&limit=` | portal | Past runs: the run-ledger rows of the job's sessions |

Any other path returns 404.

**Chat.** A chat turn can run for minutes, past API Gateway's 30 s integration cap, so `POST
/api/chat` launches it as a background task on the runtime and returns; the client then polls
`GET /api/chat` and renders the buffered events (text deltas, tool calls and results, in arrival
order). Closing the tab never stops the agent. A message sent while a turn is running supersedes it:
the runtime cancels that run (it ends `cancelled`) and answers the new message. Chat needs
`STRANDLY_RUNTIME_ARN` on the Lambda (`-c runtime_arn=`); without it the chat routes return `503`.

**Titles.** Both session routes return `title` (`null` when untitled). A title is one run-ledger
row per session, `task_id = session-title#<sid>`, outside the `recent` GSI, so run listings never
show it. People rename a session in the portal (`PATCH`, 404 for a session the detail route would
not find); the agent sets its own session's title with the `set_session_title` tool and keeps a
title a person set unless asked. Titles are one line of at most 80 characters, control and format
characters removed. The Lambda's only ledger write is `PutItem` on keys with that prefix.

**Transcripts.** `GET /api/sessions/{id}` reads, per session:

1. the verbatim transcript, one **AgentCore Memory** event per message, under the actor named by
   `session/<sid>/memory-actor.json` in the sessions bucket (needs `AGENTCORE_MEMORY_ID`);
2. else the latest session **snapshot** (`session/<sid>/…/snapshot_latest.json`), the managed and
   compacted history;
3. else a reconstruction from the run-ledger (one row = one prompt + result).

The response's `source` is `"memory"` for any stored transcript and `"ledger"` for the fallback.
The Lambda carries a pinned boto3 layer (`infra/stacks/lambda_sdk.py`): the runtime's bundled SDK
drops the tool-call payloads from `ListEvents`.

## Scheduled jobs

The `/api/schedules*` routes are served by their own function, `SchedulesApiFn`
(`harness/src/strandly/ops/lambdas/schedules_api/handler.py`, packaged from `src/` with a layer
carrying the pinned SDK and `tzdata`), not by ReadApiFn: ReadApiFn is also invoked directly, around
the Cognito authorizer (the internal site), so a write path there would reach every principal that
may invoke it. Only API Gateway invokes `SchedulesApiFn`. They exist only when the stack gets a
runtime ARN, since the schedules table and jobs group live in the Scheduler stack.

- **Authentication.** Every schedules route requires a Cognito access token with the `openid`
  scope. The Lambda also checks `token_use`, the immutable UUID `sub`, and the exact configured
  pool issuer. Its role has no Cognito lookup permission. API Gateway authorizer context is
  trusted only because this function has no direct-invocation grant to internal site or the runtime;
  do not add such a grant or a function URL.
- **Writes** go through `ScheduleService` (see [Using Strands Factory](../docs/using-strands-factory.md#schedule-work)) as
  `Actor("portal:<Cognito sub>", "dashboard", issuer="<Cognito issuer>")`: dashboard limits
  (one-hour minimum interval, 20 000-char prompts), and any job may be changed. Emails and
  usernames are not owner identifiers. Legacy email actors are audit history, not identity aliases.
- **Concurrency.** Every job carries a `version`; `PUT`, pause, resume, revert and delete require
  `expected_version`. A stale version is `409` (`code: conflict`), even for a no-op pause/resume.
  Reverting a deleted job uses its latest revision as the expected version and preserves its creator.
- **Edit preview.** Send `id` when editing. Validation uses the existing status and rate phase.
  Editing a completed once job does not rearm it: save a future time, then explicitly resume it.
- **Errors** are `{"error", "code"}`: `400 invalid` (the service's validation message), `400
  bad_request` (malformed body, unknown fields), `403 forbidden`, `404 not_found`, `409 conflict`,
  `503 unconfigured`.
- **Global kill switch.** The CDK `schedules_enabled` context value is evaluated once and wired
  to both the Scheduler invoker and `SchedulesApiFn` as `STRANDLY_SCHEDULES_ENABLED`. Deploy both
  stacks with the same context to apply a global stop; changing one Lambda's environment alone
  does not propagate to another. An explicit false/invalid value in this variable or the legacy
  `STRANDLY_SCHEDULED_ENABLED` blocks new manual, scheduled and late-reconcile launches. The run
  route returns `503` / `code: disabled` without consuming a run claim; definitions remain editable.
- **Run now** launches through the runtime like a schedule fire (`last_run.trigger = manual`), with
  socket timeouts that fit the API's 29 s: a cold session takes longer to accept, so its run is
  reported `maybe-dispatched` (acceptance unknown) rather than failing the request. Do not retry
  an unknown acceptance; check its session first. A failed ledger write does not conceal the
  launch outcome. Internal runtime, synchronization and transaction errors are not returned verbatim.
- **Past runs.** The run ledger has no session index, so `/runs` Queries its `recent` GSI
  newest-first within `days` (default 30, at most 90), filtered to session ids starting
  `sched-<id>` and then matched exactly (`sched-<id>`, `-<YYYYMMDD>`, `-<YYYYMMDD-HHMMSS>`). It
  stops at `limit` matches (default 50) or after 5 000 ledger rows; `complete: false` says older
  matches may exist.

## Opt-in session transcript pagination

`GET /api/sessions/{id}?limit=50` loads the newest **eligible messages** in the accessible history;
follow `next_cursor` as `?limit=50&before=<opaque>` to load earlier pages. Messages within each
page are oldest-to-newest, so prepend older pages. A tool-only call/result is an eligible message;
blank/non-conversational events do not consume the limit. `limit` is an integer from 1 through
200 (default 50 when only `before` is supplied). No `limit` or `before` means the full response,
including full ledger turns (strandly-mcp reads that form).

Paginated responses always include:

* `session_id`, `runs`, `source` (`memory` or `ledger`). `runs` counts session ledger rows in the
  current window, not page messages or all historical runs.
* `messages`: at most `limit` entries, each with stable `id`, `role`, `text`, `timestamp`, and
  `tool_use`. Memory messages retain ordered `blocks`, but omit the duplicate flat `tools` and
  `tool_results` aliases. Ledger messages reconstruct prompt then error/result (error wins),
  with `failed: true` on errors; their timestamps are run start and finish respectively.
* `turns`: **metadata only**, including all visible runs' `task_id`, `status`, `started_at`, and
  `ended_at` on every page. This lets the UI attach to an active run even when loading old history.
  `prompt`, `result`, and `error` are deliberately absent, not duplicated outside the page.
* `next_cursor`: opaque string, or `null` at the beginning of the accessible history.
* `history_boundary`: `null` for a complete Memory scan, `memory_event_limit` when its scan stopped
  at 10,000 events with more history still unread, or `ledger_window` for the fallback's latest
  200 **global** ledger rows. A null cursor does not assert that older retained history exists
  nowhere else. The Memory ceiling can leave a **newer tail unread**, so a ceiling-marked page
  is newest only within the scanned prefix, not the full session.

Cursors bind the session, transcript source, and first message of the previous page. Equal
Memory timestamps are ordered by event identity then payload position; identical payloads remain
separate messages. Equal ledger start times are ordered by task id, with prompt before reply.
The cursor includes a digest of pending older identities to detect history gaps. New messages
appended after the boundary do not shift pagination; deletion/expiry, rolling out of the ledger
window, or insertion/reordering before the boundary produces **409** (`code: cursor_expired`)
and requires a fresh initial read, rather than silently skipping or repeating history. Treat
cursor contents as opaque, not an offset, and do not reuse them across sessions.

Malformed/invalid cursors and limits return **400**. Memory is preferred on the first page and
can fall back to ledger if empty/unavailable. Once a cursor selects a source it never switches:
a failed/unconfigured Memory read returns **503**, and an empty/changed older Memory page returns
**409**. A ledger read failure returns **503**. An unknown initial session returns **404**; a known
run without messages returns **200** with `messages: []`, its run metadata, and a null cursor.

This bounds response/rendering size, **not retrieval latency**: AgentCore `ListEvents` is
oldest-first with no reverse scan flag, so every Memory page request still reads forward through
the session up to the existing ceiling. Older ledger history outside the recent global window
is not fetched by this API.

## Auth

Cognito Hosted UI with OAuth2 Authorization-Code + PKCE on one public app client (no secret). The
portal reads its `config.json` (written by CDK at deploy time), signs in with a random OAuth
`state` it checks on the redirect, and calls the API with `Authorization: Bearer <access_token>`,
refreshed before its 1 h expiry. `strands-factory login` uses the same client with the loopback redirect
`http://localhost:8765/callback`. The pool requires TOTP MFA (users enroll an authenticator app on
their first sign-in) and refresh tokens last 30 days. The Vite dev origins `http://localhost:5173`
and `http://localhost:5174` are callback, logout and CORS origins only in development deployments
(`env=dev` or `-c dev_callbacks=true`). Self-sign-up is off; create maintainers with
`aws cognito-idp admin-create-user --user-pool-id <CognitoUserPoolId> --username you@example.com`.

The user pool is `RETAIN`ed, and the logical ids `UserPool`, `HostedUiDomain`, `WebClient`,
`ReadApi` and `Portal*` must not change: replacing them would wipe the maintainer accounts or
change the client id / API URL / portal URL that installed strandly-mcp copies have cached.

## Deploy

The Dashboard stack is part of round 3 in [Deploy your own](../docs/deploy.md#round-3-permissions-ingresses-and-the-portal).
It only reads the deployment, so redeploying it never touches the agent. By hand, from the
deployment workspace (after the Data stack exists):

```bash
(cd dashboard/web-next && npm ci && npm run build)
infra/scripts/build-poller-package.sh --local infra/build/poller
cd infra
npx --yes aws-cdk@2 deploy Example-agent-Dashboard-dev --exclusively \
  -c runtime_arn=<AgentRuntimeArn> -c memory_id=<MemoryId> \
  -c poller_asset="$PWD/build/poller" \
  [-c cognito_domain_prefix=<globally-unique-prefix>]   # first deploy only
```

`-c runtime_arn=` also instantiates the Ingress / Scheduler / Slack stacks, whose synth needs the
poller asset; `--exclusively` keeps them out of the deploy.

Outputs: `PortalURL`, `PortalBucketName`, `ApiURL`, `CognitoHostedUI`, `CognitoUserPoolId`,
`CognitoClientId`, `RunLedgerTableName`, `PeopleAdminFunctionArn`.
