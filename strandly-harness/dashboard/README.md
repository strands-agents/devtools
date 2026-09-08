# Strandly Dashboard

A small, fully AWS-hosted dashboard for the deployed Strandly agent. It surfaces the runtime
telemetry that GitHub can't — durable invocation history, token/cost, traces — plus the agent's
GitHub activity. **Minimum-lovable cut: three tabs, Cognito-gated, no custom domain.**

```
Maintainer browser (SPA)
   │  Cognito JWT (OAuth2 Authorization-Code + PKCE)
   ▼
CloudFront ── S3 (single-file SPA + config.json)
   │
   ▼ Authorization: Bearer <access_jwt>
API Gateway (HTTP API, Cognito JWT authorizer)
   ▼
Lambda (read-only) ──► DynamoDB RunLedger ◄── written through by the AgentCore runtime
                       (deep-links out to CloudWatch GenAI / X-Ray for full traces)
```

## Why it needs a backend (and the dashboard for strands-coder didn't)

`strands-coder`'s state lives entirely in GitHub, so its dashboard is a zero-backend static page.
**Strandly runs on Bedrock AgentCore**, fire-and-forget, with run state in an in-memory, per-instance
store that's lost on recycle. So a browser-only page can show Strandly's GitHub activity but not its
runtime data. This dashboard closes that gap with a **durable run-ledger**: the runtime writes one
DynamoDB row per invocation (see `src/strandly_harness/serving/run_ledger.py`), gated on
`STRANDLY_RUN_LEDGER_TABLE` exactly like every other Strandly capability. Unset it and the runtime
behaves exactly as before.

## Tabs (MLP)

- **Overview** — runs today, success rate, active-now (over a recent window); a **health strip**
  (mention-poller liveness derived from the `…-poll-silent` alarm + every CloudWatch alarm's state,
  via `GET /api/health`, gated on `ALARM_NAME_PREFIX`); and **GitHub stats** (open PRs/issues
  involving the agent + an interactions-per-day sparkline from the public events feed — no token).
- **Runs** — durable invocation list, newest-first; click a row for a detail drawer (timeline,
  tokens in/out, duration, the PR/issue it targeted, and a deep-link to the CloudWatch/X-Ray trace).
  Each run links to **its session** (row + drawer), and runtime logs open in a **full-screen,
  searchable viewer** (live filter with match highlighting and level styling) instead of a side-tab blob.
- **Sessions** — recent runs grouped by session id (newest-active first), each with a description
  (the session's originating prompt), run count, tokens, last activity, and target. A **"New
  session"** button mints a fresh session id and opens an empty chat — the Strandly analog of
  strands-dashboard's "create an issue" (there an issue *is* a session; here a session *is* a
  conversation). It's entirely client-side: no row exists until the first message launches a run,
  which the runtime writes to the ledger + Memory under that exact id (see `agentcore.py` — the
  ledger's `session_id` *is* the payload's `sessionId`), after which it lists and resumes like any
  other. A **"Jump back
  in"** chip row at the top is your recently-opened sessions (persisted in `localStorage`, MRU) so
  you can resume the last conversations in one click — even ones that have scrolled out of the
  recent window. Open one to **continue the conversation**: a chat panel renders the session's
  transcript and lets you send a new message. The transcript is the **verbatim conversation from
  AgentCore Memory** when a memory id is wired (the real messages, including the agent's own mid-run
  narration), falling back to a ledger reconstruction (one ledger row = one turn's prompt + result)
  otherwise.
- **Activity** — `strandly-the-agent`'s PRs/issues across GitHub (public Search API; no token).

The SPA is an installable **PWA** (manifest + service worker — network-first for same-origin GETs,
cached-shell fallback offline; the API/Cognito/GitHub are never cached).

### Chat (continue a session)

The Sessions tab's chat **invokes the deployed AgentCore runtime** to continue a conversation, using
the runtime's native **fire-and-forget + poll** model (a chat turn can run minutes — well past API
Gateway's 30s integration cap — so we never hold the socket open): `POST /api/chat` launches a turn
on the session and returns a `taskId`; the SPA then polls `GET /api/chat?task_id=…&session_id=…`
until it completes. The run persists under the session's AgentCore Memory id (so the agent has the
prior context) and also lands in the run-ledger, so each chat turn shows up in the Runs tab too.

The chat panel's **transcript** is read from **AgentCore Memory** (`ListEvents`) when the read
Lambda has `AGENTCORE_MEMORY_ID` set — the actual user/assistant messages, with the agent's mid-run
narration shown in a muted style. It's gated independently of chat: pass `memory_id` to the CDK and
the Lambda gets `AGENTCORE_MEMORY_ID` (+ optional `STRANDLY_ACTOR_ID`) and a scoped
`bedrock-agentcore:ListEvents` grant on that one memory resource. Without it the transcript falls
back to the ledger reconstruction — no behavior change.

Chat is **gated on `STRANDLY_RUNTIME_ARN`** (set on the read Lambda by the CDK when a `runtime_arn`
is supplied; it also gets a scoped `bedrock-agentcore:InvokeAgentRuntime` grant). Without it the
chat routes return `503` and the dashboard degrades cleanly to read-only — every other tab is
unaffected.

Deferred to a follow-up: About/Skills tabs, token-by-token streaming (needs a Lambda Function URL —
API Gateway can't stream), an inline trace viewer, and a custom domain.

## Auth (Cognito, incognito-honest)

Login is **Cognito Hosted UI** via OAuth2 Authorization-Code + **PKCE** (public SPA client, no
secret). The SPA reads `config.json` (written at deploy time), redirects to the hosted UI, exchanges
the `?code` for tokens, stores them in `localStorage`, and calls the API with
`Authorization: Bearer <access_token>`; the API Gateway Cognito JWT authorizer validates it. The
access token is silently refreshed ~1 min before its 1 h expiry. **Incognito:** a fresh window has
no tokens and no Cognito session cookie, so you do a full sign-in each time — expected, secure
behavior (incognito persists nothing).

## Components

| Layer | Service |
|---|---|
| Hosting | S3 (private) + CloudFront (OAC) serving the single-file SPA + `config.json` |
| Auth | Cognito user pool + hosted UI; HTTP API Cognito JWT authorizer |
| API | API Gateway HTTP API + Lambda — reads (`GET /api/overview`, `/api/runs`, `/api/runs/{id}`, `/api/sessions`, `/api/sessions/{id}` — transcript from AgentCore Memory when `AGENTCORE_MEMORY_ID` is set, scoped `ListEvents`), `/api/health` — alarm states + poller liveness, gated on `ALARM_NAME_PREFIX`, scoped `DescribeAlarms`, chat (`POST`/`GET /api/chat`, gated on `STRANDLY_RUNTIME_ARN`), public `/api/config` |
| Durable runs | DynamoDB `RunLedger` (on-demand) with a `recent` GSI for newest-first listing — owned by the **Data** stack, imported here |
| Traces | CloudWatch GenAI Observability / X-Ray (deep-linked from the run drawer) |
| IaC | AWS CDK — the **Dashboard** stack of the unified [`infra/`](../infra) app |

## Layout

```
dashboard/
  web/index.html          single-file SPA (vanilla JS + Tailwind CDN, Cognito PKCE, 3 tabs)
  api/handler.py          Lambda: reads the run-ledger + chat (launch/poll the runtime) — pure router + AWS wiring split
```

The CDK app lives in the repo-root [`infra/`](../infra) directory (not under `dashboard/`). The
dashboard is the `DashboardStack` there; it builds the SPA + API Lambda from this directory via
`Code.from_asset`. The run-ledger DynamoDB table is **not** created by the dashboard — it lives in
the `DataStack` and is imported, so tearing the dashboard down never deletes the runtime's telemetry.

## Deploy

The run-ledger table comes from the **Data** stack, so deploy that first (`strandly provision` does,
or `cdk deploy 'Strandly-Data-dev'`). Then:

```bash
cd infra
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cdk deploy 'Strandly-Dashboard-dev' -c env=dev -c region=us-west-2 -c account=<acct> \
  -c cognito_domain_prefix=<globally-unique-prefix> \  # optional
  -c runtime_arn=<deployed-runtime-arn> \              # optional — enables the Sessions chat
  -c memory_id=<agentcore-memory-id>                   # optional — verbatim transcripts from Memory
```

Pass `runtime_arn` (the ARN from `strandly deploy`) to enable the **Sessions chat** tab: it adds
`STRANDLY_RUNTIME_ARN` to the read Lambda and a scoped `InvokeAgentRuntime` grant. Omit it and the
dashboard deploys read-only (chat routes return `503`).

Pass `memory_id` (the **Backend** stack's `MemoryId` output) to render each session's transcript as
the **verbatim AgentCore Memory conversation**: it adds `AGENTCORE_MEMORY_ID` (+ optional
`-c actor_id=<…>` → `STRANDLY_ACTOR_ID`, only if you overrode the default actor) and a scoped
`bedrock-agentcore:ListEvents` grant. Omit it and transcripts fall back to the ledger reconstruction.

Outputs: `DashboardURL` (the CloudFront URL), `ApiURL`, `CognitoHostedUI`, `CognitoClientId`,
`CognitoUserPoolId`, and **`RunLedgerTableName`**.

Then turn the ledger on for the runtime:

1. Redeploy/point the AgentCore runtime with `STRANDLY_RUN_LEDGER_TABLE=<RunLedgerTableName>` (and
   `AWS_REGION`). See `../docs/deployment.md`.
2. Grant its execution role `dynamodb:PutItem` on the table — deploy the **RuntimeIam** stack with
   `-c run_ledger_table=<RunLedgerTableName>` (it includes the `RunLedger` grant), instead of the
   old manual `put-role-policy`.
3. Create a maintainer in the Cognito user pool (self-sign-up is disabled):
   `aws cognito-idp admin-create-user --user-pool-id <CognitoUserPoolId> --username you@example.com`.

## Status

The harness-side change (run-ledger write-through + token capture) and the Lambda router are
covered by the hermetic test suite (`pytest`). The CDK app deploys cleanly to a dev account: the
stack stands up, the public `/api/config` route returns the Cognito config, and the authorized
routes return 401 without a JWT. The browser OAuth login flow (hosted-UI sign-in → token exchange →
authorized API calls) has not been exercised end-to-end yet.
