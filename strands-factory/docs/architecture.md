---
title: "Architecture"
description: "How Strands Factory splits work between an agent runtime and a sandbox runtime, how a request travels from a surface to a reply, and where state and trust live."
---

# Architecture

This page explains how Strands Factory is built and why: the two runtimes, the path a request takes, where state lives, and which boundary holds each credential. Read it before you change the code or decide whether to trust a deployment with write access. For setup steps, see [Deploy your own](deploy.md).

## One agent, many surfaces

Strands Factory builds one Strands agent per deployment. Strandly, the Strands team's agent, is one such deployment. Every surface (portal, CLI, ACP, MCP, Slack, GitHub, schedules) ends up in the same agent factory, `core.agent.assemble_agent`, which wraps `strands_harness.create_harness`. The model, tools, plugins, prompt, and context strategy are constants in code, not options.

We chose that over a configuration schema because a maintainer agent's behavior should be reviewable in one place. The cost is flexibility: to change what the agent does, you change code. Deployments differ only in identity (who it is, where it writes, who may call it) and in which integrations have credentials. A feature turns on when its credential is present, and the same code runs locally with fallbacks: a local shell instead of the sandbox runtime, files instead of S3.

## System overview

![System overview. People reach the agent from the portal, CLI, ACP, and MCP clients, Slack DMs, and GitHub mentions, and EventBridge schedules start runs on their own. Every request passes an ingress that authorizes the caller and invokes one agent runtime on an AgentCore microVM in your AWS account. The agent keeps state in S3, DynamoDB, and AgentCore Memory, sends every command to a separate sandbox runtime on its own EC2 instance per session, and posts comments and pull requests to GitHub, while the sandbox clones and pushes with git.](assets/diagrams/system-overview.svg)

*Every surface goes through an ingress to one agent runtime; the sandbox runs the commands.*

Both runtimes are Amazon Bedrock AgentCore Runtimes.

| Runtime | Runs on | Holds | Does not hold |
|---|---|---|---|
| Agent | AgentCore microVM | Model access, the GitHub write token, the config secret, session storage, memory | A shell |
| Sandbox | AgentCore Runtime instances on EC2 (arm64), one persistent EBS workspace per session | A narrower git credential, egress to the internet, Bedrock while `sandbox_bedrock_grants` is on | Secrets, sessions, memory, the people directory |

The agent's `shell`, `read`, `write`, and `edit` tools call the sandbox with `InvokeAgentRuntimeCommand`. The agent decides; the sandbox executes. Model-generated commands never run where the agent's credentials are. See [Operations](operations.md#the-sandbox) for the sandbox's lifecycle and limits.

![Inside an agent run. The agent runtime, on an AgentCore microVM, runs the Strands harness agent loop with its plugins (environment, skills, goal check). The loop calls the model on Amazon Bedrock, delegates to subagents that share the same sandbox, and calls two kinds of tools. File and shell tools (shell, read, write, edit) send commands to the sandbox runtime, an EC2 instance with a persistent workspace at /mnt/workspace, which clones and pushes with git. Host tools (use_github, use_slack, schedule, MCP tools) run in the agent runtime and write to GitHub as the bot. The run saves its session snapshot to S3 and its transcript to AgentCore Memory. The sandbox holds no secrets or agent token, and calls Bedrock only while sandbox_bedrock_grants is on.](assets/diagrams/agent-runtime.svg)

*The agent holds the credentials and decides; every command it runs crosses into the sandbox, which holds none of them.*

## A request, end to end

Every surface follows the same path. Surfaces differ only in how the ingress authorizes the caller (step 1), which session id it picks (step 2), and where the result goes: GitHub for mentions, the Slack thread for DMs, and a poll for the portal and CLI.

![A request, end to end. A request from the portal, CLI, Slack, or a GitHub mention reaches its ingress, which checks the caller and invokes the agent runtime with a session id. The agent loads the session and sends shell and file operations to the sandbox runtime, which clones and pushes with git and returns output. The agent then comments or opens a pull request on GitHub, replies in the Slack thread for Slack runs, and portal and CLI clients poll the API for the final answer.](assets/diagrams/request-flow.svg)

*The ingress authorizes, the agent decides, the sandbox executes, and the result goes back to where the request came from.*

1. The ingress authorizes the caller. The poller skips mentions outside the managed repositories, then checks the static allow-list, then the people directory, then org membership; any one grants access and every error denies. Slack checks its allow-list and the directory. The portal API checks the Cognito JWT.
2. The ingress invokes the agent runtime with a session id derived from the source: one per GitHub item, one per Slack thread, the chosen session for portal and CLI, and the job's session mode for schedules. The session snapshot, context stash, Memory transcript, agent runtime session, and sandbox session all derive from that id; the sandbox session id is a digest of it.
3. The agent loads the session snapshot, composes the prompt (global prompt, the capability blocks for the tools that registered, an optional role), and runs. Plugins add per-turn context, serve skills, and run a goal check that can send the agent back for another pass.
4. Tool calls that touch files or run commands go to the sandbox. On first use in a session, the agent wakes the sandbox and pushes every skill folder into the workspace.
5. GitHub writes go through `use_github`, which checks the target owner against the managed repositories and throttles mutations. Slack replies go through `use_slack`, which exists only in Slack-triggered runs.
6. The run's start and end land in the run ledger. The portal and CLI read sessions from there.

A GitHub item's session id is `gh-<owner>-<repo>-<h8>-<kind>-<number>`, lowercased, where `h8` is the first 8 hex characters of the SHA-256 of the lowercased `owner/repo`. The digest keeps repositories whose names hyphenate alike, such as `acme/labs-tool` and `acme-labs/tool`, in separate sessions. An item whose session predates `h8` starts a fresh session.

## Where state lives

| State | Store | Failure behavior |
|---|---|---|
| Session snapshot (managed conversation) and context stash | S3 session bucket, KMS-encrypted | Fails closed: a deployed run never falls back to local files |
| Verbatim transcript, one event per message, plus the context injected into each ask | AgentCore Memory; an event over Memory's 100 KB limit keeps a preview there and its full payload in the session bucket | Fails open |
| Long-term memory (team facts and preferences, personal facts per person, shared between their Slack DMs and the portal) | AgentCore Memory namespaces per actor | Fails open |
| Run ledger, work products, dedup, mention log | DynamoDB | Ledger writes alarm on failure |
| People directory | DynamoDB, read only by the ingresses | Fails closed |
| Schedules and revisions | DynamoDB, plus one EventBridge schedule per job | The table wins at fire time. Every job belongs to the team, and a run can schedule more work ([Schedule work](using-strands-factory.md#schedule-work)) |

The snapshot holds what the context manager kept: old turns are summarized, and large tool results and oversized user messages (a text block over 50,000 tokens, or `STRANDLY_USER_TEXT_OFFLOAD_TOKENS`) are cut to previews. The originals stay in the context stash, where `retrieve_context` reads them. The transcript is the full record, which is why the portal reads it first.

## Trust boundaries

Strands Factory reduces privilege at each hop. It does not make prompt injection impossible. Review who can invoke a deployment and which repositories it may write to before you give it a write token.

- **Who may call** is decided outside the agent. The ingress Lambdas, the dashboard API among them, hold the only read grant on the people directory (`dynamodb:Query` on its indexes), and the table's name is not in the agent's config. The dashboard API uses it only to find the caller's person ([Portal and CLI users](security.md#portal-and-cli-users)). Nothing the model runs can read or edit the allow-lists.
- **Where it may write** is the managed repositories list, enforced per call in `use_github`: a REST write by its path, a GraphQL write only through a listed mutation whose target resolves to a managed repository, and an unresolved target is refused. Every write is attributed to the bot account. A fork of a managed repository lands only in the bot account.
- **What runs commands** is the sandbox, whose role has no secret, session, ledger, or memory permissions and no copy of the agent's GitHub token.
- **What it can tell Slack** is limited to the thread that started the run.
- **What happened** is checked by an independent audit job that asks GitHub what the bot's account did and alarms on writes outside the allowed owners.

Sandbox egress is open, because the job needs `git clone`, `pip`, and `npm`. If you need egress control, put a proxy or firewall on the sandbox's NAT route. [Security model](security.md) explains which grants are broad on purpose and what you own.

## Code map

Paths are relative to this folder.

| Path | Contents |
|---|---|
| `harness/src/strandly/core/` | Agent factory, config, prompt, event contract |
| `harness/src/strandly/tools/` | `use_github`, `use_slack`, `schedule`, `subagent` |
| `harness/src/strandly/plugins/` | Per-turn context, goal check, tool metrics |
| `harness/src/strandly/memory/` | Session store, transcript, long-term memory, actor scopes |
| `harness/src/strandly/sandbox/` | Sandbox selection, the runtime client, session TTL |
| `harness/src/strandly/serve/` | CLI, MCP, AgentCore entrypoint, deploy and provision commands |
| `harness/src/strandly/ops/` | Ingress, scheduler, and monitoring Lambdas (stdlib and boto3 only) |
| `harness/src/strandly/skills/` | Packaged skills |
| `cli/src/strandly_cli/` | Client CLI and `strands-factory cli build` (no harness imports) |
| `cli/strandly-mcp/` | Thin MCP client library and package |
| `dashboard/` | Portal (`web-next`) and its read API |
| `infra/` | CDK app and stacks |
| `sandbox/` | Sandbox image |

## Models

The top agent runs the [harness model string](https://strandsagents.com/docs/user-guide/harness/quickstart/) in `STRANDLY_MODEL` at the effort in `STRANDLY_EFFORT`. Unset, it runs the harness default model, Claude Opus 5.5 (`bedrock/global.anthropic.claude-opus-5-5`), at effort `medium`. The harness and the SDK's model info set thinking, prompt caching, the output budget and the context window. The harness builds a `bedrock/` Claude model on Anthropic's Bedrock client (`InvokeModel` and the Messages API) as `AnthropicModel`; other Bedrock models run on Converse. Set both keys in the deployment's config secret to change them, for example `bedrock/global.anthropic.claude-fable-5-2` at `high`. `STRANDLY_EFFORT` takes the harness levels (`auto`, `off`, `low`, `medium`, `high`, `xhigh`, `max`); a model without reasoning levels needs `auto` or `off`. A portal chat may name its own `model` and `effort` for a turn instead. The runtime passes them to the harness model resolution, the same one that builds `STRANDLY_MODEL`, before the run starts: it parses `<provider>/<model id>`, refuses an unknown provider or an effort the model does not offer, and reads credentials and endpoints from the runtime's environment, never from the string. A refused choice ends the request with the harness's reason and starts no run. Strands Factory keeps no list of models; it only refuses a `model` longer than 256 characters or an `effort` longer than 32, and either with a control character. What runs in practice is what the runtime role may call ([model grants](deploy.md#configure-integrations)). The run ledger records each run's `model` and `effort`, and the `default` subagent tier follows the run's model.

Subagents pick a tier through the `subagent` tool:

| Subagent tier | Model |
|---|---|
| `default` | The top agent's model and effort |
| `fast` | Claude Haiku 5.5, thinking off |
| `advanced` | Claude Fable 5.2, effort max |

The `web_fetch` summarizer runs on Claude Haiku 5.5. The source of truth is `EFFORT_DEFAULT`, `MODEL_TIERS` and `WEB_FETCH_MODEL` in `harness/src/strandly/core/constants.py`, and the harness's `DEFAULT_MODEL`.
