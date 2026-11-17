# Strandly Harness

One opinionated Strands agent — runs locally or on Amazon Bedrock AgentCore, served as a CLI, an MCP server, or an AgentCore Runtime.

Strandly is an autonomous agent that helps build [Strands Agents](https://strandsagents.com), *built with Strands itself*. It is **one opinionated agent**: the model, tools, plugins, prompt, and context strategy are fixed in code. Every capability is gated on a secret and falls back to a local equivalent when it's absent — the identical code runs on your laptop and as a hosted runtime.

```
            ┌──────────── one agent (build_agent) ────────────┐
 CLI  ─────▶│  Opus 4.8 · bash/file_editor/think/spawn · MCP  │
 MCP  ─────▶│  plugins: skills · goal loop · todo · events    │──▶ sandbox  (local | AgentCore CI)
 AgentCore ▶│  global prompt + per-turn event context         │──▶ session  (file  | AgentCore Memory)
            └─────────────────────────────────────────────────┘
```

## Overview

- **Opinionated, not configurable.** No config schema, no tool loader, no knobs. The model, tools, plugins, prompt, and context strategy are constants in code — one source of truth for what the agent does. You change behavior by editing code, not by wiring options.
- **Local or AgentCore, same code.** Every capability is gated on a secret and falls back to a local equivalent when it's absent — so the identical code runs on your laptop and as a hosted runtime.
- **One agent, three surfaces.** CLI, MCP server, and AgentCore Runtime are all the same `build_agent` factory behind one normalized event stream.
- **Verifies its own work.** A tool-wielding actor-critic re-runs the tests and reads the files before calling a task done — it doesn't trust the transcript.

## Quick Start

Requires **Python 3.10+** and **AWS Bedrock credentials** (the model is Claude Opus 4.8 on Bedrock).

```bash
pip install -e ".[all]"
```

With no other configuration Strandly runs fully locally — a local sandbox, a file session under `./sessions`, and a file offloader under `./artifacts`:

```bash
strandly run "explain what this repo does"        # one-shot, streams to the terminal
strandly chat                                     # interactive REPL (Ctrl-D to exit)
strandly run --hitl "delete the stale branches"   # approve/interrupt each tool call
```

Serve the same agent other ways:

```bash
strandly serve mcp          # expose as an MCP `ask_agent` tool over stdio
strandly serve agentcore    # run as a Bedrock AgentCore Runtime entrypoint
```

## Configuration

A capability is **off until its secret appears**. Drop secrets in a `.env` in your working directory (or load them from Secrets Manager via `STRANDLY_SECRETS_ARN`):

```bash
# .env
AWS_REGION=us-west-2
STRANDLY_GITHUB_TOKEN=ghp_…
```

| Secret / env var | Effect when set |
|---|---|
| `STRANDLY_GITHUB_TOKEN` | Enables the `use_github` tool and unlocks **full GraphQL enrichment** in the always-on `GitHubContextInjector` plugin. The injector runs even *without* a token — it falls back to the anonymous REST API for public issues/PRs (discussions need a token). |
| `STRANDLY_SEARCH_MCP_URL` (+ `STRANDLY_SEARCH_MCP_TOKEN`) | Adds the web-search MCP. |
| `AGENTCORE_CODE_INTERPRETER_ID` | Uses the managed AgentCore sandbox (else local). |
| `AGENTCORE_MEMORY_ID` | Uses the AgentCore Memory session (else a file session). |
| `STRANDLY_KB_ID` + `STRANDLY_KB_DATA_SOURCE_ID` | Enables long-term memory (`search_memory`/`add_memory`). |
| `AWS_PROFILE` / `AWS_REGION` | The shared boto3 session (else ambient credentials). |

The strands-agents documentation MCP is **always on** (no secret); it runs via `uvx strands-agents-mcp-server`, so install [`uv`](https://docs.astral.sh/uv/).

## Deploying

For a deployed agent, `provision` creates the AWS backends once and bundles their ids into a Secrets Manager secret; the runtime then needs only `STRANDLY_SECRETS_ARN`:

```bash
eval "$(strandly provision --name strandly --region us-west-2)"
```

The CDK app under [`infra/`](./infra/) provisions the full deployed footprint (data, backend, ingress, monitoring, dashboard, OIDC federation for GitHub Actions).

Two repo workflows drive the deployed agent keylessly via that OIDC federation (see [`.github/workflows/`](../.github/workflows/)):

- **`strandly-deploy.yml`** — provisions + deploys the AgentCore runtime when `strandly-harness/` changes on `main` (and on manual dispatch), using the privileged *deploy* role.
- **`strandly-invoke.yml`** — invokes the *deployed* runtime with a prompt (manual dispatch, reusable `workflow_call`, or a maintainer `@strandly` mention on an issue/PR), using the minimal *invoke* role that can never redeploy.

## Package Layout

| Path | What it is |
|---|---|
| [`src/strandly_harness/`](./src/strandly_harness/) | The harness: `build_agent` factory, tools, plugins, skills, memory, serving surfaces |
| [`infra/`](./infra/) | CDK app for the deployed footprint (AgentCore, DynamoDB, Lambda pollers, OIDC, dashboard) |
| [`dashboard/`](./dashboard/) | Maintainer dashboard (SPA + read-only API Lambda) |
| [`tests/`](./tests/) | Full test suite — AWS/network-free via a `FakeModel` |
| [`AGENTS.md`](./AGENTS.md) | The terse design source of truth: architecture + conventions |

## Development

```bash
pip install -e ".[dev]"     # core + dev/test deps
pytest                      # full suite — AWS/network-free via a FakeModel
ruff check .                # lint — the merge gate
```

New behavior gets a test; ruff must pass. See [`AGENTS.md`](./AGENTS.md) for architecture and conventions.

## License

This project is licensed under the Apache-2.0 License - see the [LICENSE](../LICENSE) file for details.
