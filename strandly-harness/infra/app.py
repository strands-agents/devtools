#!/usr/bin/env python3
"""Unified CDK app for Strandly's AWS infrastructure.

One app, env-parameterized (``-c env=dev|prod``), stands up every AWS backend the harness uses —
**except** the AgentCore Runtime itself, which the bedrock-agentcore starter toolkit owns
(``strandly deploy``). Stacks:

- **Backend** — AgentCore Memory + Code Interpreter, the S3-Vectors KB, and the config secret
  (replaces the imperative ``strandly provision``).
- **Data** — the run-ledger + dedup DynamoDB tables (the stateful core; the other stacks reference
  these by deterministic name, not a cross-stack import — see below).
- **Dashboard** — Cognito + HTTP API + read Lambda + S3/CloudFront SPA (references the run-ledger by name).
- **Ingress** — the ``@mention`` poller Lambda + EventBridge schedule (references the dedup table by name).
- **Scheduler** — time-triggered self-invocations: one generic invoker Lambda + one EventBridge
  schedule per job in ``strandly_harness.ops.lambdas.scheduled.jobs`` (gated on ``-c runtime_arn=…`` like Ingress).
- **RuntimeIam** — supplemental data-plane policy on the toolkit-created runtime exec role
  (opt-in; only when ``-c exec_role_name=…`` is supplied, i.e. after ``strandly deploy``).
- **Monitoring** (#356) — operational CloudWatch alarms (failure-rate, poller-silent, ledger,
  stuck-runs, throttle) + the stuck-run detector Lambda + an SNS alert topic.
- **Cost** (#356) — AWS Cost Anomaly Detection over real billing data (tag-scoped); gated on
  ``-c alarm_email=…``. No fabricated token-derived dollar metric.
- **Audit** (#356) — the independent, out-of-band GitHub write-audit Lambda + schedule + SNS;
  gated on ``-c audit_allowed_owners=…`` (the stack rejects an empty allow-list).
- **Oidc** — GitHub Actions OIDC provider + two roles: a privileged *deploy* role (locked to the
  repo's protected refs) and a minimal *invoke* role (``InvokeAgentRuntime`` + poll). Role ARNs are
  stack outputs you set as repo secrets — replaces the imperative ``setup-aws-oidc.sh``.

Deploy order: ``cdk deploy '*-Backend-*' '*-Data-*'`` → ``strandly deploy`` (toolkit) →
``cdk deploy`` the rest with the resolved runtime ARN / exec role name in context.

Context knobs (all via ``-c key=value``):
  env, name, account, region, with_kb, github_token, cognito_domain_prefix,
  runtime_arn, memory_id, actor_id, mention_handle, allowed_authors, skip_repo, secret_arn,
  schedule_expression, schedule_enabled, schedules_enabled, exec_role_name, kb_id,
  run_ledger_table, poller_asset,
  github_repo, deploy_subjects, invoke_subjects, oidc_provider_arn, deploy_policy,
  alarm_email, monitoring_enabled, stuck_run_minutes, audit_allowed_owners, audit_schedule,
  audit_lookback_hours, cost_threshold
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from stacks.audit_stack import AuditStack
from stacks.backend_stack import BackendStack
from stacks.common import Naming
from stacks.cost_stack import CostStack
from stacks.dashboard_stack import DashboardStack
from stacks.data_stack import DataStack
from stacks.ingress_stack import IngressStack
from stacks.monitoring_stack import MonitoringStack
from stacks.oidc_stack import OidcStack
from stacks.runtime_iam_stack import RuntimeIamStack
from stacks.scheduler_stack import SchedulerStack

app = cdk.App()

# Cost-allocation tags applied to every resource in every stack, so AWS Cost Anomaly
# Detection / Cost Explorer can segment this deployment as a cost center (see CostStack).
# NOTE: the `app` tag must be *activated* once in the Billing console to become usable as a
# cost-allocation dimension — an account-global step CloudFormation cannot perform.


def ctx(key: str, default: str | None = None) -> str | None:
    val = app.node.try_get_context(key)
    return val if val is not None else default


def ctx_bool(key: str, default: bool) -> bool:
    val = app.node.try_get_context(key)
    if val is None:
        return default
    return str(val).lower() not in ("false", "0", "no")


def ctx_list(key: str) -> list[str] | None:
    """Comma-separated context value -> list (for the OIDC ``sub`` claim knobs)."""
    val = ctx(key)
    if not val:
        return None
    return [item.strip() for item in str(val).split(",") if item.strip()]


naming = Naming(name=ctx("name", "strandly") or "strandly", env=ctx("env", "dev") or "dev")
env = cdk.Environment(account=ctx("account"), region=ctx("region"))

extra_secrets: dict[str, str] = {}
if ctx("github_token"):
    extra_secrets["STRANDLY_GITHUB_TOKEN"] = ctx("github_token")  # type: ignore[assignment]

# OIDC federation for GitHub Actions: a privileged deploy role (locked to protected refs) + a
# minimal invoke role. Account-global provider — pass `-c oidc_provider_arn=…` to reuse an existing
# one (e.g. a second env). Has working defaults, so it's always synthesized; deploy it on its own.
OidcStack(
    app,
    naming.stack("Oidc"),
    naming=naming,
    github_repo=ctx("github_repo", "strands-agents/devtools") or "strands-agents/devtools",
    deploy_subjects=ctx_list("deploy_subjects"),
    invoke_subjects=ctx_list("invoke_subjects"),
    oidc_provider_arn=ctx("oidc_provider_arn"),
    runtime_arn=ctx("runtime_arn"),
    memory_id=ctx("memory_id"),
    deploy_policy=ctx("deploy_policy", "scoped") or "scoped",
    env=env,
)

data = DataStack(app, naming.stack("Data"), naming=naming, env=env)

backend = BackendStack(
    app,
    naming.stack("Backend"),
    naming=naming,
    with_kb=ctx_bool("with_kb", True),
    extra_secrets=extra_secrets or None,
    # Opt-in: attach a scoped, ABAC-tag-gated execution role to the Code Interpreter so the agent
    # can invoke Bedrock + manage ManagedBy=strandly test resources (for e2e-testing Strands).
    # Off by default — the sandbox is credential-free unless this is set. See the e2e-test skill.
    ci_bedrock_role=ctx_bool("ci_bedrock_role", False),
    env=env,
)

# Dashboard reads the run-ledger table by its (deterministic) name — NOT by importing Data's live
# ITable. A cross-stack import emits an Fn::ImportValue + a CloudFormation export, and an export
# can't be modified while imported, which deadlocks a re-deploy of Data. Deriving from `naming`
# keeps the lifecycle boundary (Data owns the table) without the export coupling. See DashboardStack.
dashboard = DashboardStack(
    app,
    naming.stack("Dashboard"),
    naming=naming,
    cognito_domain_prefix=ctx("cognito_domain_prefix"),
    # When a runtime arn is known (post `strandly deploy`), wire up the dashboard's chat tab:
    # the read Lambda gets scoped InvokeAgentRuntime + STRANDLY_RUNTIME_ARN. Absent it, chat 503s.
    runtime_arn=ctx("runtime_arn"),
    # When a memory id is known (the Backend stack's MemoryId output), the dashboard reads each
    # session's verbatim transcript from AgentCore Memory (scoped ListEvents). Absent it, the
    # transcript falls back to the ledger reconstruction. `actor_id` overrides the default actor.
    memory_id=ctx("memory_id"),
    actor_id=ctx("actor_id"),
    env=env,
)

# Ingress needs a deployed runtime to dispatch to — only synthesize it once a runtime ARN is known.
# It reads the dedup table by name (same no-import rationale as the dashboard above).
runtime_arn = ctx("runtime_arn")
if runtime_arn:
    ingress = IngressStack(
        app,
        naming.stack("Ingress"),
        naming=naming,
        runtime_arn=runtime_arn,
        mention_handle=ctx("mention_handle", "") or "",
        allowed_authors=ctx("allowed_authors", "") or "",
        skip_repo=ctx("skip_repo"),
        secret_arn=ctx("secret_arn"),
        schedule_expression=ctx("schedule_expression", "rate(5 minutes)") or "rate(5 minutes)",
        schedule_enabled=ctx_bool("schedule_enabled", True),
        poller_asset=ctx("poller_asset"),
        env=env,
    )

    # Scheduler: time-triggered self-invocations (daily review, …). Like Ingress it needs a runtime
    # to dispatch to, and reuses the same prebuilt Lambda asset.
    SchedulerStack(
        app,
        naming.stack("Scheduler"),
        naming=naming,
        runtime_arn=runtime_arn,
        secret_arn=ctx("secret_arn"),
        poller_asset=ctx("poller_asset"),
        all_enabled=ctx_bool("schedules_enabled", True),
        env=env,
    )

# RuntimeIam attaches to the toolkit-created exec role — only after `strandly deploy` names it.
exec_role_name = ctx("exec_role_name")
if exec_role_name:
    RuntimeIamStack(
        app,
        naming.stack("RuntimeIam"),
        naming=naming,
        exec_role_name=exec_role_name,
        kb_id=ctx("kb_id"),
        run_ledger_table=ctx("run_ledger_table") or naming.run_ledger_table,
        config_secret_arn=ctx("secret_arn"),
        env=env,
    )


# Cost-allocation tags (applied last so every construct above is tagged).
cdk.Tags.of(app).add("app", "strandly")
cdk.Tags.of(app).add("env", naming.env)

# --- Monitoring (#356): operational alarms + the stuck-run detector ---------------------------
# The poller package is reused for the stuck-run Lambda; gate on it being built (like Ingress).
_poller_asset = ctx("poller_asset")
_asset_dir = Path(_poller_asset) if _poller_asset else (
    Path(__file__).resolve().parent / "build" / "poller"
)
_asset_available = _asset_dir.is_dir()
alarm_email = ctx("alarm_email")

if ctx_bool("monitoring_enabled", True) and _asset_available:
    monitoring = MonitoringStack(
        app,
        naming.stack("Monitoring"),
        naming=naming,
        run_ledger_table=data.run_ledger,
        secret_arn=ctx("secret_arn"),
        alarm_email=alarm_email,
        poller_asset=_poller_asset,
        stuck_run_minutes=int(ctx("stuck_run_minutes", "30") or "30"),
        schedule_enabled=ctx_bool("schedule_enabled", True),
        env=env,
    )
    monitoring.add_dependency(data)
elif ctx_bool("monitoring_enabled", True):
    print(f"⚠ MonitoringStack skipped: poller asset not built at {_asset_dir} "
          "(run infra/scripts/build-poller-package.sh --local infra/build/poller).")

# --- Audit (#356): the independent, out-of-band GitHub write-audit job ------------------------
# Synthesized only when an allow-list is supplied (the stack itself rejects an empty one).
audit_owners = ctx("audit_allowed_owners")
if audit_owners and _asset_available:
    AuditStack(
        app,
        naming.stack("Audit"),
        naming=naming,
        allowed_owners=audit_owners,
        secret_arn=ctx("secret_arn"),
        lookback_hours=int(ctx("audit_lookback_hours", "24") or "24"),
        schedule_expression=ctx("audit_schedule", "rate(6 hours)") or "rate(6 hours)",
        schedule_enabled=ctx_bool("schedule_enabled", True),
        alarm_email=alarm_email,
        poller_asset=_poller_asset,
        env=env,
    )
elif audit_owners:
    print(f"⚠ AuditStack skipped: poller asset not built at {_asset_dir}.")

# --- Cost (#356): AWS Cost Anomaly Detection (real billing data; no token-derived $ metric) ---
# Synthesized only with an alert destination (a subscription needs somewhere to send anomalies).
if alarm_email:
    CostStack(
        app,
        naming.stack("Cost"),
        naming=naming,
        alarm_email=alarm_email,
        threshold_dollars=float(ctx("cost_threshold", "50") or "50"),
        env=env,
    )

app.synth()
