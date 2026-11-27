---
title: "Security model"
description: "What a Strands Factory deployment can reach, what it stores and sends out of your account, what you own when you deploy it, and how to shut it off."
---

# Security model

This page is for operators deciding what to give a Strands Factory deployment and what to set up around it. To report a vulnerability, see [SECURITY.md](../SECURITY.md).

Anyone who can put text in front of the agent can try to make it use any token, permission, or data it holds. Strands Factory does not prevent prompt injection; it limits what an injected instruction can reach. Grant each deployment only the access its use needs.

## Boundaries

| Boundary | What the agent can do | Why it matters |
|---|---|---|
| AWS | Call Bedrock models and read AgentCore Identity tokens across its account | An injected instruction can use every grant the agent holds |
| GitHub | Write to the managed repositories, as the bot account | A bad push or a misleading comment in your repositories does the most damage |
| Slack | Answer DMs, and tags in the optional home channel, in the thread that started the run | Everything it reads is stored with the session, and everything it posts is visible to that conversation |
| Sandbox | Run every command the model writes, with open internet egress and, while `sandbox_bedrock_grants` is on, Bedrock access | Builds need `git`, `pip`, and `npm`; agent tests need a model |
| Portal and CLI | Let any signed-in user read and post into every session and change every schedule | Signed-in users are not isolated from each other ([Portal and CLI users](#portal-and-cli-users)) |

The sandbox runs in its own runtime, without the agent's GitHub token, config secret, memory, or session store ([Credentials and network](operations.md#credentials-and-network)). The Strands deployment keeps its sandbox Bedrock grants because it builds and tests agents; a deployment that only reviews code does not need them.

Cost monitoring catches only cost abuse. It does not catch a leaked GitHub token pushing code or private text posted to Slack. Branch protection and the Slack app's scopes bound those ([Deployer checklist](#deployer-checklist)).

## Portal and CLI users

The portal is not a boundary between team members. Cognito decides who may sign in ([Sign-in](#sign-in)), and every signed-in user shares the same sessions and schedules:

- A user's person is the people-directory row bound to their Cognito user ([Decide who can use it](deploy.md#decide-who-can-use-it)). The dashboard API reads it from the verified sign-in and never from the `X-Strandly-Person` header, which it ignores for requests through API Gateway. An unbound user gets team memory.
- Any user can list, read, rename, and post into every session, including Slack DMs and GitHub threads.
- Any user can edit, pause, run, or delete any schedule, because every job belongs to the team ([Schedule work](using-strands-factory.md#schedule-work)).
- Personal memory is a convenience layer, not a private store. One memory follows each person between their Slack DMs and their portal, CLI, and ACP runs: `slack-user-<UID>` when the people directory has their Slack id, else `person-<person_id>`. Only the Cognito user bound to a person reaches it from the portal, CLI, or ACP.
- A direct invoke of the dashboard API or schedules Lambda has no Cognito sign-in, so its `X-Strandly-Person` login names the person and that person's memory, and Cognito claims in its payload are trusted as that portal user. Treat `lambda:InvokeFunction` on those Lambdas like `InvokeAgentRuntime` on the runtime: either one lets the caller choose whom a run is for, so grant it only to principals you trust like the ingresses, such as a team site.

If your users need isolation from each other, do not give them portal access.

### Sign-in

One Cognito user pool serves the portal, CLI, ACP, and MCP.

- Every sign-in needs a password and a TOTP authenticator code.
- Cognito redirects a sign-in only to the portal or the CLI's `http://localhost:8765/callback`. The portal accepts a redirect only with the OAuth `state` and PKCE verifier it started.
- Refresh tokens last 30 days. Signing out of the portal or the CLI revokes them.
- [`dev_callbacks`](deploy.md#stack-switches) adds the Vite dev server ports to the redirects, so any local process on those ports can receive a sign-in redirect.
- The portal's Content Security Policy loads scripts only from the portal, connects only to its API, Cognito, and `api.github.com`, loads images only from the portal and GitHub, and forbids framing. The portal also sends HSTS and `nosniff`.

## What a deployment stores

A deployment stores what people send the agent and what it reads and writes while it works: repository code, Slack DMs, tool output, and anything someone pastes. Portal users read it through the portal, and anyone in your AWS account with access to these stores can read it directly.

| Data | Where | Kept by default |
|---|---|---|
| Session snapshot, the conversation a run resumes from | S3 session bucket, KMS-encrypted | 30 days idle (Slack, portal, MCP), 15 days (scheduled runs), 90 days (GitHub, CLI, ACP, and everything else). Overwritten versions: 3 days |
| Full tool results and oversized transcript events | The same bucket, under `context/` | The same number of days, counted from when each object was written |
| Transcript, one event per message | AgentCore Memory | 90 days |
| Long-term memory: team facts and preferences, and personal facts per person | AgentCore Memory | Until you delete it |
| Run ledger: each run's status, timing, token counts, and the first 4,000 characters of its prompt and result | DynamoDB | Until you delete it |
| Agent, sandbox, and Lambda logs, including prompts, tool output, and command text | CloudWatch Logs | 30 days |
| Sandbox workspace: clones and build output | One EBS volume per sandbox session | 5 days after last use |

Retention is set in code, not in the profile. Change `SESSION_RETENTION_DAYS`, `SESSION_NONCURRENT_DAYS`, or `MEMORY_EVENT_EXPIRY_DAYS` in `infra/stacks/common.py` and redeploy the Data and Backend stacks. Log retention lives in `infra/stacks/runtime_common.py`, and the sandbox deletion time in `harness/src/strandly/core/constants.py`.

## Where data leaves your account

| Destination | What it receives | When |
|---|---|---|
| The model in `STRANDLY_MODEL` | The prompt, the conversation, and tool output | Every model call |
| Anthropic, OpenAI, or Google Vertex AI | Whatever sandbox test code sends them | Only with `sandbox_provider_federation` set ([Direct model providers](operations.md#direct-model-providers)) |
| Web search | The search queries the agent writes | Only with `STRANDLY_SEARCH_MCP_URL` in the config secret. The WebSearch stack's gateway uses the AgentCore Web Search connector |
| strandsagents.com | Requests for Strands documentation pages and the integrations catalog | When the agent uses its `strands_` tools |
| Any website | Pages `web_fetch` requests, and anything sandbox code sends | Always, through the sandbox's NAT gateway |
| GitHub | Comments, reviews, issues, pull requests, and commits in the managed repositories | With `STRANDLY_GITHUB_TOKEN` set, and `STRANDLY_SANDBOX_GIT_TOKEN` for pushes from the sandbox |
| Slack | Replies in the thread that started the run, and home-channel posts | With the Slack stack deployed; the home channel only when it is on |

The default model is a Bedrock global inference profile, which can serve a request from any supported AWS Region. Pick a regional profile to keep inference in one geography ([Models](architecture.md#models)).

## Shared responsibility

| Area | Strands Factory provides | You own |
|---|---|---|
| AWS account | Stacks that assume they are alone in the account, and short-lived STS credentials for the agent and the sandbox | A dedicated account per deployment, and least privilege on every role the deployment creates or trusts |
| Cost and alarms | Monitoring alarms, Cost Anomaly Detection when `alarm_email` is set, and a write-audit job | AWS Budgets, someone who reads the alarms, and a plan for when they fire |
| AWS audit | Separate IAM roles for the agent, the sandbox, and each Lambda, so CloudTrail shows which part acted | CloudTrail, with alerts on actions that matter to you |
| GitHub | A write check on every call ([Trust boundaries](architecture.md#trust-boundaries)), a daily write cap ([Configure integrations](deploy.md#configure-integrations)), and bot attribution | The bot account, its token, branch protection, and token rotation |
| Slack | A signature check on every request, an allow-list that admits nobody when empty, and replies scoped to the thread | The app's scopes, the allow-list, and token rotation |
| Secrets | A config secret and a Slack secret, readable only by the agent and the Lambdas that need them | Creating, scoping, storing, and rotating every token you put in them |
| Who may call | Ingresses that authorize every caller before the agent runs. The agent cannot change who is allowed | The people directory, the author and workflow lists, and the Cognito users |
| Data | The retention defaults and the switches in [Where data leaves your account](#where-data-leaves-your-account) | Retention that fits your data, the model and inference profile, egress filtering, and which third parties may see your data |
| Fixes | Fixes in `main`, with no versioned support ([SECURITY.md](../SECURITY.md)) | Pulling `main` and redeploying |

The checklist below turns the right-hand column into steps.

## Deployer checklist

1. Deploy into a dedicated AWS account.
2. Configure every role for least privilege:
   - Turn off `sandbox_bedrock_grants` unless the deployment builds and tests agents.
   - Leave `sandbox_provider_federation` unset unless you test against those providers. If you set it, configure only the providers you need, and limit the federated identity at each to a dedicated project or workspace with the narrowest roles that work, never organization-wide access. The Google token requests the full `cloud-platform` scope, so the roles you grant the workload identity pool principal set its limit.
   - Keep `runtime_invokers` to the exact role ARNs that invoke the runtime.
   - If you deploy the GitHub OIDC stack (`-c github_repo`), pass `runtime_arn`. Its invoke role can only call `InvokeAgentRuntime`, trusts only workflows on `main`, and with `runtime_arn` reaches only that runtime. Keep `deploy_policy` at `scoped`, never `admin`, and narrow its resources in `infra/stacks/oidc_stack.py` to this deployment's resource names. The deploy role trusts the GitHub Environment named after your `env` context value; give that Environment required reviewers and limit it to the default branch.
   - Review every wildcard (`*`) resource in the roles you deploy.
3. Create an AWS Budget and turn on Cost Anomaly Detection. Set `alarm_email` and confirm the subscription.
4. Turn on CloudTrail and alert on IAM changes and other actions you care about.
5. Create a GitHub account for the bot with only the access an outside contributor has: not an organization member or a collaborator, and no admin or bypass rights. Prefer a fork and pull requests to write access. Give it a fine-grained token, never a classic token, with the fewest permissions it needs, and make the token's repository selection equal `managed_repositories`, so GitHub rejects any write the in-band check misses. If the bot needs more, such as triage rights on specific repositories, write down which rights, on which repositories, and why. Put the token in the config secret by hand.
6. Require branch protection or rulesets on every repository the agent writes to. Every branch and tag the bot's tokens can reach needs a pull request and a human reviewer's approval, and the bot cannot approve, bypass the rules, or edit protected workflow files. This is the control that limits a bad push.
7. Leave `STRANDLY_SANDBOX_GIT_TOKEN` unset unless the sandbox must push. If you set it, use a separate fine-grained token on selected repositories.
8. Give the Slack app only the scopes it needs ([Configure integrations](deploy.md#configure-integrations)), and keep `allowed_users` short.
9. Keep `managed_repositories`, the people directory, and the `actions_*` lists short. Set `actions_workflows` whenever you set `actions_repositories`.
10. Remove from `managed_repositories` and the allow-lists any owner the deployment no longer controls, for example a renamed or deleted user account. Session ids are keyed on the repository name, so whoever takes the name next inherits its sessions.
11. Keep `dev_callbacks` off in production ([Sign-in](#sign-in)).
12. Decide on egress. The sandbox reaches the internet through one NAT gateway.
13. Check the retention defaults in [What a deployment stores](#what-a-deployment-stores) against your data.

## Offboard a person

When someone leaves or loses access:

1. **Disable their directory row.** Run `strands-factory people add <id> --disabled ...` or `strands-factory people remove <id>`.
2. **Revoke their other grants.** Remove them from the Slack secret's `allowed_users`, `STRANDLY_MENTION_ALLOWED_AUTHORS`, and the allowed GitHub orgs, and disable their Cognito user with `aws cognito-idp admin-disable-user`.
3. **Review their jobs.** Scheduled jobs keep running after the person who created them leaves. On the portal's Schedules page, pause or delete the ones they created or changed that you no longer want.

## Shut it off

If you think a deployment is being misused, take these steps in order. Names below use the profile name `example-agent` and the environment `dev`.

1. **Cut GitHub access.** Revoke `STRANDLY_GITHUB_TOKEN` and `STRANDLY_SANDBOX_GIT_TOKEN` in GitHub, and remove the bot account from your repositories. The agent can no longer write, and the mention poller stops picking up mentions.
2. **Cut Slack access.** Revoke the app's bot token, or uninstall the app from your workspace.
3. **Stop new runs.** Pause the affected jobs on the portal's Schedules page. Set reserved concurrency to zero on the ingress Lambdas: `example-agent-dev-mention-poller`, `example-agent-dev-slack-ingress`, `example-agent-dev-scheduled-invoker`, and the API functions in the Dashboard stack, which serve the portal, CLI, ACP, MCP, and GitHub Actions.

   ```bash
   aws lambda put-function-concurrency --function-name example-agent-dev-mention-poller \
     --reserved-concurrent-executions 0
   ```

   `aws lambda delete-function-concurrency` turns a function back on.
4. **Stop the agent.** Delete the RuntimeIam stack with `aws cloudformation delete-stack --stack-name Example-agent-RuntimeIam-dev`. Without it the agent cannot read its config secret, session store, or Memory, or reach the sandbox. New runs fail when they load their session, and a run in progress fails at its next sandbox or session-store call. Deploy the stack again as in [Round 3](deploy.md#round-3-permissions-ingresses-and-the-portal) to bring the agent back.

Before you turn anything back on, rotate every token in the config secret and the Slack secret.
