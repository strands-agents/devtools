# Strandly infrastructure (CDK)

One env-parameterized AWS CDK app (Python) that owns **every AWS backend Strandly uses except the
AgentCore Runtime itself** — the [bedrock-agentcore starter toolkit](../docs/deployment.md) owns the
runtime (via `strandly deploy`), because it owns the cloud build → ECR → image pipeline.

This package is intentionally isolated from the harness: it has its own venv
([`requirements.txt`](./requirements.txt)) so the harness test/lint gate never installs `aws_cdk`,
and it cannot import `strandly_harness` (that would pull in the Strands SDK). A few fixed values are
hand-mirrored into [`stacks/common.py`](./stacks/common.py) and guarded against drift by
`tests/test_infra_constants_sync.py`.

## Stacks

| Stack id (`<Name>-<Kind>-<env>`) | What it creates | Notes |
|---|---|---|
| `Strandly-Backend-<env>` | AgentCore Memory + Code Interpreter, S3-Vectors KB (+ CUSTOM data source + role), config secret | replaces the old imperative `strandly provision` |
| `Strandly-Data-<env>` | run-ledger + dedup DynamoDB tables | the stateful core; Dashboard + Ingress **import** these, so deleting them never drops data |
| `Strandly-Dashboard-<env>` | Cognito + HTTP API + read Lambda + S3/CloudFront SPA | imports the run-ledger table from Data |
| `Strandly-Ingress-<env>` | `@mention` poller Lambda + EventBridge schedule | only synthesized when `-c runtime_arn=…` is given; imports the dedup table |
| `Strandly-Scheduler-<env>` | one generic invoker Lambda + one EventBridge schedule per job in `scheduled/jobs.py` | only synthesized when `-c runtime_arn=…` is given; reuses the poller Lambda asset. See [docs/scheduled.md](../docs/scheduled.md) |
| `Strandly-RuntimeIam-<env>` | supplemental data-plane policy on the toolkit-created runtime exec role | only synthesized when `-c exec_role_name=…` is given |
| `Strandly-Oidc-<env>` | GitHub OIDC provider + a privileged **deploy** role and a minimal **invoke** role | role ARNs are stack outputs → set as repo secrets; replaces `setup-aws-oidc.sh`. See [docs/oidc.md](../docs/oidc.md) |

## Deploy order

The runtime (toolkit) and the CDK app interleave, because Ingress/RuntimeIam need values that only
exist after the runtime is deployed:

```
1. cdk deploy 'Strandly-Backend-<env>' 'Strandly-Data-<env>'   # or: strandly provision
2. strandly deploy ...                                          # toolkit builds the runtime
3. cdk deploy 'Strandly-Dashboard-<env>'                        # imports run-ledger
   cdk deploy 'Strandly-Ingress-<env>' -c runtime_arn=<arn> ...
   cdk deploy 'Strandly-RuntimeIam-<env>' -c exec_role_name=<role> ...
```

`strandly provision` (see [docs/provisioning.md](../docs/provisioning.md)) is a thin wrapper that
runs step 1 for you and prints the secret ARN.

## Setup

```bash
cd infra
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cdk deploy 'Strandly-Backend-dev' 'Strandly-Data-dev' -c env=dev -c region=us-west-2
```

The Ingress stack needs the poller Lambda asset built first (arm64 wheels), from the **repo root**:

```bash
infra/scripts/build-poller-package.sh --local infra/build/poller
```

## Context knobs (`-c key=value`)

| Key | Default | Used by |
|---|---|---|
| `env` | `dev` | all — the isolation suffix (`dev`/`prod`/…) |
| `name` | `strandly` | all — resource name prefix |
| `account` / `region` | ambient | the CDK env target |
| `with_kb` | `true` | Backend — set `false` to skip the long-term-memory KB |
| `github_token` | — | Backend — folded into the config secret |
| `ci_bedrock_role` | `false` | Backend — attach a scoped, ABAC-tag-gated execution role to the Code Interpreter so the sandbox can invoke Bedrock + manage `ManagedBy=strandly` test resources (for e2e testing). Off = credential-free sandbox. See [docs/configuration.md](../docs/configuration.md#sandbox-aws-credentials-for-e2e-testing) |
| `cognito_domain_prefix` | `<name>-<env>-dashboard-<account>` | Dashboard |
| `runtime_arn` | — | **gates** Ingress + Scheduler synthesis; the runtime they dispatch to |
| `mention_handle` / `allowed_authors` / `skip_repo` | — | Ingress |
| `secret_arn` | — | Ingress + Scheduler — lets the dispatched run read the config secret |
| `schedule_expression` / `schedule_enabled` | `rate(5 minutes)` / `true` | Ingress |
| `schedules_enabled` | `true` | Scheduler — set `false` to deploy every job schedule paused |
| `poller_asset` | `infra/build/poller` | Ingress + Scheduler — the built Lambda asset dir |
| `exec_role_name` | — | **gates** RuntimeIam synthesis; the toolkit-created exec role |
| `kb_id` / `run_ledger_table` | — | RuntimeIam — scopes the data-plane grants |
| `github_repo` | `mkmeral/strandly-harness` | Oidc — the `owner/repo` the trust policies pin to |
| `deploy_subjects` / `invoke_subjects` | main (+ `production` for deploy) | Oidc — comma-separated `sub` claim patterns the roles trust |
| `oidc_provider_arn` | — | Oidc — import an existing account-global provider instead of creating one |
| `deploy_policy` | `scoped` | Oidc — `admin` swaps the curated deploy policy for `AdministratorAccess` |

## Environments

`-c env=prod` stands up a completely separate set of resources (names derive from `<name>-<env>`,
stack ids carry the env), so dev and prod never collide. In `prod` the config secret and both
DynamoDB tables get a `RETAIN` removal policy; in other envs they're `DESTROY` (disposable).

## Tests

```bash
# from infra/, in the CDK venv:
pip install -r requirements.txt pytest
pytest tests/
```

`infra/tests/` runs under the CDK venv (it imports `aws_cdk`), so it is **not** part of the harness
`pytest` run. It synthesizes each stack and asserts the resources/wiring. The harness-side guard
(`tests/test_infra_constants_sync.py`) separately checks that the mirrored constants haven't drifted.
