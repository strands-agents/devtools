# AGENTS.md

Living guide for this repo. Source of truth for architecture + conventions.

## What this is

`strandly-harness` is **one opinionated Strands agent** — the model, tools, plugins, prompt, and
context strategy are **fixed in code**. It runs **locally or on Bedrock AgentCore** (capabilities
turn on when their secret is present, else local fallbacks), and is served as a **CLI, MCP server,
or AgentCore runtime**. Built on the Strands SDK, wiring its primitives directly. Flat package, one
`build_agent` factory, **no config schema** — just `Config` (secrets/.env) + constants.

## Commands

```bash
uv sync --locked --all-extras         # install EXACTLY the committed uv.lock (core + every extra)
pytest                                # full suite (no AWS/network; FakeModel)
ruff check .                          # lint — the gate
strandly run "..."                    # one-shot; also: chat | serve {agentcore,mcp} | provision
```

Dependencies are uv-managed: `pyproject.toml` is the single source of truth and `uv.lock` is
the tool-managed freeze (**never hand-edit uv.lock**). After a dep change run `uv lock` (or
`uv lock --upgrade-package <name>` to bump one dep); install with `uv sync --locked`, which
hard-fails if the lock is stale. CI + deploy install the same way, and the deploy workflow
regenerates `requirements.txt` from the lock (`uv export`) so the AgentCore image builds from
exact pinned versions.

Importing the package runs `otel.sanitize_otel_env()` (strips a stray `xray` from
`OTEL_PROPAGATORS` that would crash the OpenTelemetry import) — so no env var needs to be set, in
tests or at runtime. `conftest.py` imports the package up top for this reason.

## What's fixed vs. what's configured

**Fixed** (`constants.py`, the factory): model = **Claude Opus 4.8** on Bedrock (adaptive thinking,
1M context) for the top agent — subagents may select a fixed tier via `spawn(model=...)`:
`fast` (Haiku 4.5) or `advanced` (Fable 5), from the `MODEL_TIERS` registry (a Claude-family
subset, never free-form ids; Fable needs the account's `provider_data_share` retention mode); the tool set; the plugins (skills, event-context injection, goal loop, todo, context
offloader, AgentCore session persistence); the SDK `context_manager="agentic"` context strategy;
the global prompt.

**Configured** (`config.py`, from **AWS Secrets Manager** via `STRANDLY_SECRETS_ARN`, else `.env`,
under the process env). Capabilities are **gated on their secret**:

| Secret / env | Effect |
|---|---|
| `STRANDLY_GITHUB_TOKEN` | enables `use_github`; unlocks full GraphQL enrichment in the (always-on) `GitHubContextInjector` plugin; bootstraps the sandbox git client (credential store) so native `git clone`/`push` authenticates as the same identity |
| `STRANDLY_SEARCH_MCP_URL` (+ `STRANDLY_SEARCH_MCP_TOKEN`) | adds the web-search MCP |
| `AGENTCORE_CODE_INTERPRETER_ID` | use the managed sandbox (else local) |
| `AGENTCORE_MEMORY_ID` | use the AgentCore short-term session (else file) |
| `STRANDLY_RUN_LEDGER_TABLE` | write each deployed run through to a DynamoDB run-ledger (else in-memory poll only) — powers the dashboard |
| `AWS_PROFILE` / `AWS_REGION` | the shared boto session (else ambient creds) |

`provision` (CLI) creates the AgentCore Memory + Code Interpreter + a Secrets Manager secret
holding these values, so a deployed runtime just sets `STRANDLY_SECRETS_ARN`.

## Architecture rules

1. **`agent.py::build_agent(config, ctx, ...)` is the only place an `Agent` is built** — every
   surface and every subagent goes through it. It's `async` (skills are pushed into a non-local
   sandbox before the skills plugin loads). Fresh agent per request; state rehydrates from the
   session manager.
2. **Opinionated, not configurable.** Change behavior in code, don't add knobs. The only knobs are
   secrets (above).
3. **Prompt = global prompt + optional role.** `global_prompt.md` is the agent's only base prompt;
   a subagent supplies its own `role`. **Runtime/event context is injected per turn** by the
   `EventContext` plugin (env block + todos into the latest user message), like the memory manager
   injects recalls — not frozen into the static prompt.
4. **Short-term memory** (`memory/`): AgentCore Memory session when `AGENTCORE_MEMORY_ID` is set,
   else a file session. Either way bash + file_editor work against the sandbox.
5. **Sandbox** (`sandbox/`): AgentCore Code Interpreter when configured, else local.
6. **Tools** (`tools/build_tools`): `bash` + `file_editor` + `think` + `spawn` always; `use_github`
   gated (a GitHub token); MCP clients added (strands-agents docs always, web-search gated). `todo` +
   the `skill` tool come via plugins. No dedicated `read`/`grep`/`glob` — `file_editor.view` reads
   (line-numbered) and `bash` searches (`rg`/`grep`/`find`) inside the sandbox. **GitHub-thread
   enrichment is a *plugin*, not a tool** (see rule 6b): there is no `inject_github_context` tool —
   `use_github` already covers any URL the agent wants to fetch on demand mid-turn, so a context-fetch
   tool would just duplicate it.
6b. **GitHub URL context injector** (`plugins/github_threads/plugin.py`): `GitHubContextInjector`
   (**always registered — not gated on a token**, issue #346) **auto-enriches** GitHub issue/PR/discussion URLs — from the invoke
   payload (`ctx.event`/`invocation_state` `githubUrls`) and/or scraped from the latest user message
   — into the model's input **ephemerally**. It ports the TS `ContextInjector` vended plugin: TS
   uses middleware on `InvokeModelStage.Input`; Python has no middleware, so it uses **two hooks** —
   `BeforeModelCallEvent` appends the enriched block to the system prompt, `AfterModelCallEvent`
   strips that exact block back out so nothing persists into the durable conversation/session (the
   before-hook also self-cleans, so no accumulation even if the after-hook is skipped). Trigger
   `userTurn` (default) injects once per turn (not on tool-loop calls); the rendered block is cached
   per turn in `agent.state` so enrichment fetches at most once per turn. URLs are deduped + capped
   (max 5) and validated with `parse_github_url`; everything is fail-soft. The enrichment itself is
   the **pure, reused** `build_github_context(urls, token, graphql=…, rest=…)` from
   `plugins/github_threads/fetch.py`. **Token-optional:** with a token it enriches fully via GraphQL; with
   none it falls back to GitHub's anonymous REST API for public issues/PRs (discussions are
   GraphQL-only → a short "needs a token" note) — a token is used when present, never required.
8b. **Skills** (`skills/`): delivered by `SystemPromptSkills` (not the SDK `AgentSkills`
   tool-result mode). An *active* skill's full instructions stay **resident in the system prompt**
   every turn (rebuilt, not appended, on `BeforeModelCallEvent`); inactive skills show only
   name+description. The agent toggles via `skill(action=activate|deactivate|list, name=...)`; the
   active set lives in `agent.state`. Skills are still read through the sandbox (pushed in first
   for a non-local sandbox). Each skill dir MAY also hold an optional **`GOALS.md`** (sibling of
   `SKILL.md`) — **critic-facing** acceptance criteria, NOT injected into the actor's prompt. It's
   loaded at init and stashed in `agent.state`; the goal-loop critic pulls the *active* skills'
   goals (`active_skill_goals(agent)`) into an "Active skill goals" section so we can tell the
   critic exactly what to verify per active skill. Beyond the built-ins, the agent can register
   **dynamic skills from local folders** with `skill(action="load", path=...)` (e.g. a cloned
   repo's `skills/`/`.skills/` dir) — they join the same plugin (toggle, `<active_skills>`,
   GOALS.md → critic), persist via `agent.state` (`dynamic_paths`, re-loaded at `init_agent`,
   fail-soft on stale paths), refresh on re-load, and are removed with `unload`. Guardrail:
   dynamic skills can never shadow a built-in name.
7. **Subagents** (`tools/spawn.py`): `spawn(prompt, system_prompt=<file|text>)` builds a subagent
   through `build_agent` with that `system_prompt` layer; the **global prompt is always prepended**
   (via `compose`), so every subagent shares the harness identity. Bounded by depth so a leaf
   can't spawn.
8. **One agent, three surfaces** (`serve/`): `turn.run_turn` → `build_agent` →
   `agent.stream_async` → `events.translate`. **Interactive surfaces stream** (`repl` — CLI, HITL by
   default; `mcp_server` — MCP `ask_agent`): a waiting caller consumes the events. The **deployed
   `agentcore` runtime has two payload-selected modes, with no GitHub precondition**: (a) *stream* —
   `mode:"stream"` returns an async generator the SDK frames as SSE (`strandly chat --agentcore`);
   (b) *fire-and-forget* — long jobs (reviews, implementations) can't hold a connection open for
   hours, so it starts the run in a background task (marked `HEALTHY_BUSY`, held by a strong
   reference so it isn't GC'd), returns a task id immediately, and the **durable result channel is
   AgentCore Memory** (`strandly poll` reads it back with `ListEvents`; a status sentinel + settle
   heuristic decide completion). A GitHub context is optional — when present the agent *also*
   reports via `use_github`. `run_turn` reuses one live agent **per session** from an in-process
   cache (`serve/cache.py`, per-session lock) so a long-lived process doesn't rebuild over the same
   session manager and collapse history; sessionless invokes build fresh.

## Module map

```
src/strandly_harness/
  __init__.py       curated public API; sanitizes OTEL env on import (via otel_guard)
  otel_guard.py     sanitize_otel_env() — zero-dep leaf, safe before any strands/otel import
  core/             the agent itself
    agent.py        build_agent() — the one factory (+ HITL interventions)
    config.py       Config (Secrets Manager / .env) + capability gates + GitHubSettings
    constants.py    fixed model / thresholds / dirs / env-var names
    model.py        build_model(config, tier) — Bedrock Claude, adaptive thinking (MODEL_TIERS)
    context.py      RuntimeContext        events.py  HarnessEvent + translate()
    retries.py      bounded-retry helper
    prompt/         compose.py — global_prompt() + compose(role)   global_prompt.md
  tools/            build_tools + builtins (bash, file_editor) · github · spawn · todo
  plugins/          goal (hardened judge, reads active GOALS.md) · event_context · github_threads/ (plugin = injector hook, fetch = pure enrichment) · system_prompt_skills · agentcore_session
  sandbox/          select.build_sandbox (local|agentcore) · agentcore (Code Interpreter)
  memory/           session (file|agentcore) · knowledge_base (long-term KB) · offload
  mcp_clients.py    build_mcp_clients (strands-agents always; web-search gated)
  skills/           built-in skill content (SKILL.md + optional GOALS.md) + loader.build_skills_plugin
  serve/            turn.run_turn + cache · cli/ (main, repl) · agentcore_app · mcp_server · deploy · provisioning
  ops/              ★ STRANDS-FREE ZONE (stdlib + boto3 only; CI-enforced by tests/unit/ops/test_import_hygiene.py)
    runtime_client.py   InvokeAgentRuntime launch/poll — shared by CLI + every trigger
    ledger.py           durable run ledger (DynamoDB, fail-open)
    metrics.py          CloudWatch-EMF metrics (gated on STRANDLY_METRICS_NAMESPACE, fail-open)
    lambdas/            mention_poller/ (handler · sessions · dedup · audit) · scheduled/ (invoker · jobs) · stuck_runs
```

## Conventions

- Imports at module top; lazy only for optional/heavy deps (bedrock_agentcore, mcp, boto3,
  strands_tools) and the `build_agent`↔`spawn` cycle break.
- Tests stay AWS/network-free via `FakeModel` (`tests/conftest.py`). New behavior gets a test;
  ruff must pass.
- **No code here has run against a live model yet** — all tests use `FakeModel`. Live behavior
  (Bedrock call, MCP connect, AgentCore session/sandbox, provisioning) is unverified.
