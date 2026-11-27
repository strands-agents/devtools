---
title: "Deploy your own"
description: "Stand up Strands Factory in your AWS account: write a profile, provision the backends and sandbox, deploy the agent runtime, then add the portal and ingresses."
---

# Deploy your own

This page is for operators who want their own Strands Factory deployment in their own AWS account. You write one profile file, deploy the CDK stacks in three rounds, and then decide who may use the agent. Budget an afternoon for the first deployment. For what the pieces do, see [Architecture](architecture.md).

## Prerequisites

- A dedicated AWS account, in a region where Amazon Bedrock AgentCore Runtime and your chosen Bedrock model are available. A deployment's grants are broad inside its account; see [Security model](security.md).
- AWS credentials allowed to deploy CloudFormation, IAM, VPC, EC2, Lambda, DynamoDB, S3, Cognito, and AgentCore resources.
- Python 3.10+, [uv](https://docs.astral.sh/uv/), Node.js (the CDK CLI runs through `npx aws-cdk@2` if `cdk` is not on your path), and Docker with arm64 builds enabled for the sandbox image.
- A GitHub account for the agent, if it should comment or open pull requests.
- The account and region bootstrapped for CDK once: `npx --yes aws-cdk@2 bootstrap aws://111111111111/us-west-2`.

Deployments run from a workspace that the installed package writes for your profile, not from the checkout. The packages are not published to PyPI; build the wheels from a checkout:

```bash
git clone https://github.com/strands-agents/devtools.git
cd devtools/strands-factory
uv sync --locked --all-extras
python build.py web                        # builds the packaged portal assets (needs Node.js)
rm -rf dist                                # keep exactly one wheel per package
uv build --all-packages --wheel -o dist    # the strands-factory and strandly-mcp wheels
```

## Write the profile

A profile is a JSON file with two independent halves: `identity` (who the agent is and what it may touch) and `deployment` (where it runs). A GitHub login is never an AWS account; nothing is inferred from one to the other.

```json
{
  "identity": {
    "display_name": "Example agent",
    "github_login": "example-agent",
    "mention_handle": "example-agent",
    "managed_repositories": ["example-org/example-repo"],
    "allowed_authors": ["example-maintainer"],
    "allowed_orgs": []
  },
  "deployment": {
    "name": "example-agent",
    "environment": "dev",
    "account": "111111111111",
    "region": "us-west-2",
    "secret_references": {
      "config": "example-agent/dev/config"
    }
  },
  "context": {
    "sandbox_bedrock_grants": "false"
  },
  "skill_packs": [],
  "deployment_extensions": [],
  "client": {
    "command": "strandly-example",
    "portal_url": "https://portal.example.com",
    "default_org": "example-org"
  }
}
```

Replace every value. To start from a file, copy [`infra/example-profile.json`](../infra/example-profile.json). The rules that trip people up:

- **Names.** Resources and stacks derive from `{name}-{environment}`; stack ids look like `Example-agent-Data-dev`. `name` is lowercase letters, digits, and hyphens. `environment` is lowercase letters and digits only. Both start with a letter and fit in 32 characters together. Neither the environment nor a later name segment may be `infra`, `managed`, or `sandbox`.
- **Empty lists grant nothing.** With empty `managed_repositories` the agent cannot write to GitHub and the poller answers no mentions. With empty `allowed_authors`, `allowed_orgs`, and no people directory rows, nobody can invoke it from GitHub.
- **References, not values.** `secret_references` holds references to secrets. Secret values never go in the profile.
- **The config secret on a first deploy.** The Backend stack creates the config secret as `{name}/{environment}/config`, and Secrets Manager appends a random suffix to its ARN, so the ARN does not exist yet. Set `secret_references.config` to the name (`example-agent/dev/config` here) for round 1, then replace it with the full ARN that round 1 prints as `STRANDLY_SECRETS_ARN` (also the Backend stack's `SecretArn` output) and materialize a fresh workspace. IAM grants in later rounds need the full ARN.
- **`context`** carries CDK context switches (see [Stack switches](#stack-switches)).

From the checkout, install the wheels into their own environment and materialize a workspace from the profile. The workspace's `infra/cdk.json` carries the profile's coordinates, which the CDK app requires:

```bash
uv venv ~/strands-factory-deploy && . ~/strands-factory-deploy/bin/activate
set -- dist/strands_factory-*.whl         # the extra attaches to a single wheel path
uv pip install "$1[deploy]" dist/strandly_mcp-*.whl
strands-factory-package materialize --profile /absolute/path/to/profile.json --directory ~/strands-factory-workspace
export STRANDLY_DEPLOYMENT_DIR=~/strands-factory-workspace
cd ~/strands-factory-workspace
```

`strands-factory provision` and `strands-factory deploy` use the workspace named by `STRANDLY_DEPLOYMENT_DIR`; run the commands below from inside it, in the activated environment. The destination must not exist yet. `--name`, `--env` or `--stage`, and `--region` flags must match the profile; a mismatch fails before any AWS call.

## Deploy

The stacks deploy in three rounds because later stacks need earlier stacks' outputs, passed as CDK context rather than cross-stack imports.

![Deploy order. Round one, strands-factory provision, deploys the Data stack (run ledger, people, and dedup tables, the session bucket and its KMS key), the Backend stack (config secret, AgentCore Memory), and the Sandbox stack (VPC, EC2 capacity provider, image, sandbox runtime). Its IDs feed round two, strands-factory deploy, which deploys the AgentRuntime stack (the agent runtime on a microVM and its execution role). The runtime ARN feeds round three, cdk deploy: RuntimeIam (the agent role's grants), Ingress (GitHub mention poller), Scheduler (jobs table, invoker), Slack (events API, when configured), Dashboard (portal, Cognito, API), and the Monitoring, Audit, Cost, and WebSearch stacks.](assets/diagrams/deploy-topology.svg)

*Each round deploys the stacks that need the previous round's outputs.*

### Round 1: backends and sandbox

```bash
strands-factory provision --name example-agent --env dev --region us-west-2 > provision-exports.sh
. ./provision-exports.sh
```

This deploys the Data, Backend, and Sandbox stacks and prints `export` lines for the IDs the next round needs: the config secret ARN, the sandbox runtime ARN and capacity provider, the sandbox TTL schedule group and role, and the agent's subnets and security group. Write them to a file and source it; `eval "$(...)"` hides a failed deploy. Read the Memory id from the Backend stack's `MemoryId` output:

```bash
MEMORY_ID=$(aws cloudformation describe-stacks --stack-name Example-agent-Backend-dev \
  --query "Stacks[0].Outputs[?OutputKey=='MemoryId'].OutputValue" --output text)
```

The config secret now exists. Copy `$STRANDLY_SECRETS_ARN` into the profile's `secret_references.config` so later workspaces carry the full ARN, then store the agent's credentials in the secret (see [Configure integrations](#configure-integrations)). The commands below pass the ARN explicitly, so the current workspace keeps working.

### Round 2: the agent runtime

```bash
strands-factory deploy --stage dev --name example-agent --region us-west-2 > agent-runtime.out
RUNTIME_ARN=$(sed -n 's/^runtime_arn=//p' agent-runtime.out)
EXEC_ROLE=$(sed -n 's/^exec_role=//p' agent-runtime.out)
```

`strands-factory deploy` builds the arm64 code bundle and deploys the AgentRuntime stack on the AgentCore microVM. It reads round 1's IDs from your environment, else from the deployed stack outputs, and prints `runtime_arn`, `runtime_id`, and `exec_role`.

### Round 3: permissions, ingresses, and the portal

Deploy the RuntimeIam stack first. It grants the agent's execution role the session bucket, Memory, ledger, and sandbox permissions; the agent cannot work without it.

```bash
cd "$STRANDLY_DEPLOYMENT_DIR/infra"
npx --yes aws-cdk@2 deploy Example-agent-RuntimeIam-dev \
  -c env=dev -c name=example-agent -c region=us-west-2 \
  -c exec_role_name="$EXEC_ROLE" -c memory_id="$MEMORY_ID" -c secret_arn="$STRANDLY_SECRETS_ARN" \
  -c sandbox_runtime_arn="$STRANDLY_SANDBOX_RUNTIME_ARN" \
  -c sandbox_capacity_provider_id="$STRANDLY_SANDBOX_CAPACITY_PROVIDER_ID" \
  -c sandbox_ttl_schedule_group="$STRANDLY_SANDBOX_TTL_SCHEDULE_GROUP" \
  -c sandbox_ttl_role_arn="$STRANDLY_SANDBOX_TTL_ROLE_ARN" \
  -c agent_subnet_ids="$STRANDLY_AGENT_SUBNET_IDS" \
  -c agent_security_group_id="$STRANDLY_AGENT_SECURITY_GROUP_ID"
```

Then build the poller Lambda asset and deploy the operational stacks and the portal with `-c runtime_arn=<AgentRuntimeArn>` plus the same coordinates:

```bash
cd "$STRANDLY_DEPLOYMENT_DIR"
infra/scripts/build-poller-package.sh --local infra/build/poller
cd infra
npx --yes aws-cdk@2 deploy Example-agent-Ingress-dev Example-agent-Scheduler-dev Example-agent-Monitoring-dev \
  -c env=dev -c name=example-agent -c region=us-west-2 \
  -c runtime_arn="$RUNTIME_ARN" -c secret_arn="$STRANDLY_SECRETS_ARN" \
  -c mention_handle=example-agent -c sandbox_runtime_arn="$STRANDLY_SANDBOX_RUNTIME_ARN" \
  -c exec_role_name="$EXEC_ROLE"
npx --yes aws-cdk@2 deploy Example-agent-Dashboard-dev -c env=dev -c name=example-agent -c region=us-west-2 \
  -c runtime_arn="$RUNTIME_ARN" -c memory_id="$MEMORY_ID"
```

The Dashboard stack is a reader of the deployment: redeploying it never touches the agent, so ship portal changes on their own. Its `PortalURL` output is the `client.portal_url` for your CLI.

Merging code never deploys. Each round is an explicit action, and running two deploys of one environment at once corrupts CloudFormation state.

## Configure integrations

Integrations turn on when their credential is in the config secret. The Backend stack keeps its own keys current and never overwrites keys you add, so add these by hand in Secrets Manager:

| Key | Turns on |
|---|---|
| `STRANDLY_GITHUB_TOKEN` | `use_github` and the mention poller. Use a token for the agent's own GitHub account |
| `STRANDLY_SANDBOX_GIT_TOKEN` | Native `git push` from the sandbox. Use a narrower token than the agent's |
| `STRANDLY_SEARCH_MCP_URL`, `STRANDLY_SEARCH_MCP_TOKEN` | Web search (the WebSearch stack provides one) |

`use_github` caps writes at 50 in a rolling 24 hours, counted from the bot's public events, for repositories granted by an `owner/repo-*` entry of `managed_repositories` and for writes with no repository, such as gists. Writes under a bare-owner entry are not counted or capped. Set `STRANDLY_GITHUB_THROTTLE_LIMIT` in the config secret to change the cap, or `STRANDLY_GITHUB_THROTTLE_ENABLED` to `false` to turn it off.

`strands-factory provision --github-token "$TOKEN"` writes `STRANDLY_GITHUB_TOKEN` into the config secret after its deploy. Never pass a token as CDK context: CDK writes context values into the template and `cdk.out` in plaintext, so the app rejects `-c github_token`.

The runtime role grants Amazon Bedrock model invocation (`bedrock:InvokeModel*`) and Bedrock Mantle inference (`bedrock-mantle:CreateInference`), so `bedrock/` and `bedrock-mantle/` models work wherever the account has access to them. A [chat model](architecture.md#models) on any other provider gets no credentials from the deployment, and its first model call fails.

Slack uses its own secret with `bot_token`, `signing_secret`, and optionally `allowed_users`. Pass its ARN as `-c slack_secret_arn=<arn>` to create the Slack stack, then point your Slack app's event subscription at the stack's `/slack/events` URL. The app needs `im:history`, `im:read`, `im:write`, `chat:write`, and `reactions:write`, and the `message.im` event; `users:read` is optional and only improves names in prompts. Without it, prompts show Slack user ids.

A home channel ([how it behaves](using-strands-factory.md#home-channel)) needs all four of these, and stays off if any is missing:

| Setting | Where | Value |
|---|---|---|
| `slack_home_channel_enabled` | CDK context | `true` |
| `home_channel` | Slack secret | The channel id (`C...` or `G...`). The Slack Lambdas read it |
| `STRANDLY_SLACK_HOME_CHANNEL` | Config secret | The same channel id. The agent reads it |
| Scopes and events | Slack app | For a private channel, `groups:history` and the `message.groups` event; for a public one, `channels:history` and `message.channels`. Then reinstall the app and invite the bot to the channel |

With the switch on, the Slack ingress may read and the alarm notifier may write only the run-ledger rows whose key starts with `slack-thread#`. Those rows link each notification thread to its details and originating session.

## Decide who can use it

Four independent lists decide access. Keep each one short.

| Who | Where you set it |
|---|---|
| Portal, CLI, ACP, and MCP users | Cognito users in the Dashboard stack's pool, each bound to a person in the people directory |
| GitHub and Slack callers | The people directory, edited with `strands-factory people` |
| Where the agent writes | `identity.managed_repositories` in the profile |
| GitHub Actions workflows | The `actions_*` keys in [Stack switches](#stack-switches) |

Once per person, create a Cognito user, which signs in with MFA ([Sign-in](security.md#sign-in)), and bind it to their row with `--cognito`. Self sign-up is off. Do not bind a login that several people share: an unbound login gets team memory only ([Portal and CLI users](security.md#portal-and-cli-users)).

```bash
aws cognito-idp admin-create-user --user-pool-id <CognitoUserPoolId> --username you@example.com
export STRANDLY_PEOPLE_TABLE=example-agent-dev-people   # the Data stack's PeopleTableName output
strands-factory people --dashboard-stack Example-agent-Dashboard-dev add example-maintainer \
  --name "Example Maintainer" --role maintainer --github example-maintainer --slack U0123ABC \
  --cognito you@example.com
strands-factory people list
```

`--cognito` looks the user up in the pool (`--user-pool-id`, `STRANDLY_COGNITO_USER_POOL_ID`, or the Dashboard stack's `CognitoUserPoolId` output) and stores its `sub`; `--cognito-sub` stores a `sub` directly. One Cognito user binds to at most one person. `people add` replaces the row but keeps its binding unless you pass `--cognito`, `--cognito-sub`, or `--unbind-cognito`. Deploy the Data stack, which adds the `by_cognito` index, before the Dashboard stack: until the index exists, the dashboard API finds no person and runs use team memory.

The ingresses cache lookups for five minutes, so edits apply within minutes and need no redeploy. The agent has no access to this table.

To offboard someone, follow [Offboard a person](security.md#offboard-a-person).

To hand your team a CLI, run `strands-factory cli build --profile profile.json --out dist` and share the wheel ([Quickstart](quickstart.md#build-a-client-cli-for-your-team)).

A user without a TOTP authenticator ([Sign-in](security.md#sign-in)) registers one at their next Hosted UI sign-in; a CLI that is already signed in keeps refreshing until its refresh token expires.

## Stack switches

Set these in the profile's `context` or pass them with `-c key=value`.

| Key | Default | Effect |
|---|---|---|
| `with_kb` | `false` | Keeps the retired Bedrock knowledge base in the Backend stack. A deployment that already has it sets `true` so its data survives |
| `ci_bedrock_role` | `false` | Attaches a Bedrock execution role to the Code Interpreter. The agent does not use the Code Interpreter. A deployment that already has the role sets `true` to keep it |
| `monitoring_enabled` | `true` | Creates the Monitoring stack's alarms |
| `audit_enabled` | `true` | Creates the GitHub write-audit job; needs the config secret |
| `websearch_enabled` | `true` | Creates the WebSearch gateway |
| `schedules_enabled` | `true` | Enables scheduled jobs |
| `schedules_max_active` | `100` | Active scheduled jobs the team may hold across every surface. The stacks pass it on as `STRANDLY_SCHEDULES_MAX_ACTIVE` |
| `schedule_expression` | `rate(5 minutes)` | How often the mention poller runs |
| `sandbox_bedrock_grants` | `true` | Lets sandbox code call Bedrock, for end-to-end tests of agents |
| `slack_home_channel_enabled` | `false` | The Slack home channel: notifications, alarms, and tagged conversations in one channel. See [Configure integrations](#configure-integrations) |
| `alarm_email` | unset | Subscribes an email address to alarms |
| `actions_enabled` | `true` | Creates the GitHub Actions ingress (`/api/actions/invoke`) on the Dashboard API, once the runtime exists |
| `actions_audience` | `strandly` | The OIDC audience the ingress accepts |
| `actions_repositories` | empty (every caller denied) | Comma-separated `owner/repo` globs whose workflows may call the agent |
| `actions_default_branch` | `main` | The branch a calling workflow must run on and come from |
| `actions_workflows` | unset | Comma-separated globs over `owner/repo/.github/workflows/<file>`; unset allows any workflow of an allowed repository |
| `traces_sampler` | `always_on` | The agent's OpenTelemetry sampler (`OTEL_TRACES_SAMPLER`): `always_on`, `always_off`, `traceidratio`, or a `parentbased_` variant. See [Traces](operations.md#traces) |
| `traces_sampler_arg` | unset | The sampled fraction (`OTEL_TRACES_SAMPLER_ARG`, `0` to `1`) that the `traceidratio` samplers need |
| `actions_schedule_repositories` | unset | Comma-separated repositories whose workflow runs may create or change scheduled jobs |
| `dev_callbacks` | `true` when `env` is `dev`, else `false` | Adds `http://localhost:5173` and `http://localhost:5174` to the portal's sign-in redirects and the Dashboard API's CORS origins, so `npm run dev` can sign in against this deployment. See [Sign-in](security.md#sign-in) before you turn it on outside `dev` |

## Extend it

Two hooks extend a deployment without forking it. Both list `module:callable` references and are selected explicitly in the profile; installing a package does not activate it.

- `skill_packs`: each callable returns a directory of skill folders. A name that collides with a built-in skill fails the build instead of shadowing it.
- `deployment_extensions`: each callable receives `(app, naming)` and attaches resources to the existing stacks.

## Troubleshooting

**`New deployment requires explicit profile inputs: ...`**: the CDK app got no profile coordinates, usually because it ran from a checkout instead of a materialized workspace. Set `STRANDLY_DEPLOYMENT_DIR` to the workspace, or pass `-c name`, `-c env`, `-c account`, `-c region`, and `-c secret_arn`.

**`CDK context conflicts with profile coordinates`**: a flag disagrees with the profile. Fix the flag; the profile wins.

**The runtime reports READY but every run fails on storage or Memory**: the RuntimeIam stack is missing or was deployed with a different `exec_role_name`. Deploy it with the `exec_role` that `strands-factory deploy` printed.

**`strandly-example login` opens a Cognito page that says `redirect_mismatch`**: the CLI wheel predates the `http://localhost:8765/callback` redirect and tries `http://localhost:5173/`, which the app client registers only with `dev_callbacks` on. Rebuild the wheel with `strands-factory cli build` and reinstall it. New wheels also work against older Dashboard stacks, so hand them out before you deploy the Dashboard stack. To keep old wheels working meanwhile, deploy with `-c dev_callbacks=true`, then deploy again without it once they are replaced.

**Mentions get no response**: check that the Ingress stack exists, `STRANDLY_GITHUB_TOKEN` is in the config secret, and the author is allowed. The poller logs `unauthorized mention author` for denied callers.

For runtime health, alarms, and sandbox problems, see [Operations](operations.md).
