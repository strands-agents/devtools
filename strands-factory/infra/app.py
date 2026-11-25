#!/usr/bin/env python3
"""The Strandly CDK app: every stack for one ``{name}-{env}`` deployment.

Stacks are selected by context (``-c env=...``, ``-c secret_arn=...`` and so on): Data, Backend and
Sandbox always synthesize; AgentRuntime, RuntimeIam and the trigger stacks (Ingress, Scheduler,
Slack, Dashboard, RuntimeAccess, Monitoring, Audit) need the provisioned ids and built assets they
reference. A materialized workspace's ``cdk.json`` (``strandly-package materialize``) supplies the
profile's identity and deployment coordinates. See docs/deploy.md for the order and context keys.
"""

from __future__ import annotations

import os
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_lambda as lambda_
from stacks.agent_runtime_stack import DEFAULT_TRACES_SAMPLER, AgentRuntimeStack
from stacks.audit_stack import AuditStack
from stacks.backend_stack import BackendStack
from stacks.common import STUCK_RUN_MINUTES, Naming
from stacks.cost_stack import CostStack
from stacks.dashboard_stack import ActionsIngress, DashboardStack
from stacks.data_stack import DataStack
from stacks.ingress_stack import IngressStack
from stacks.monitoring_stack import MonitoringStack
from stacks.oidc_stack import OidcStack
from stacks.provider_federation import load_provider_federation
from stacks.runtime_access_stack import RuntimeAccessStack
from stacks.runtime_iam_stack import RuntimeIamStack
from stacks.sandbox_stack import SandboxStack
from stacks.scheduler_stack import SchedulerStack
from stacks.slack_stack import SlackStack
from stacks.websearch_stack import WebSearchStack

from strandly.core.config import SLACK_HOME_CHANNEL_ENABLED, home_channel_enabled
from strandly.extensions import apply_deployment_extensions

app = cdk.App()

# Cost-allocation tags applied to every resource in every stack, so AWS Cost Anomaly
# Detection / Cost Explorer can segment this deployment as a cost center (see CostStack).
# NOTE: the `app` tag must be *activated* once in the Billing console to become usable as a
# cost-allocation dimension — an account-global step CloudFormation cannot perform.


def ctx(key: str, default: str | None = None) -> str | None:
    val = app.node.try_get_context(key)
    return val if val is not None else default


def ctx_bool(key: str, default: bool) -> bool:
    """``false``/``0``/``no`` (any case) are false; unset or EMPTY keeps ``default``; else true.

    Empty counts as unset because the workflow passes ``-c key="${VAR:-}"`` for optional values: an
    unset variable must not silently switch a default-off flag on (or a default-on one off).
    """
    val = app.node.try_get_context(key)
    if val is None or str(val).strip() == "":
        return default
    return str(val).lower() not in ("false", "0", "no")


def ctx_list(key: str) -> list[str] | None:
    """Comma-separated context value -> list (for the OIDC ``sub`` claim knobs)."""
    val = ctx(key)
    if not val:
        return None
    return [item.strip() for item in str(val).split(",") if item.strip()]


# Profile coordinates are a deployment boundary, not defaults silently replaceable by the
# legacy CLI's default name/stage flags. Explicit conflicting context fails before any synth.
profile_coordinates = app.node.try_get_context("profile_coordinates")
if profile_coordinates:
    if not isinstance(profile_coordinates, dict):
        raise ValueError("profile_coordinates must be a mapping")
    conflicts = [key for key, value in profile_coordinates.items() if ctx(key) != value]
    if conflicts:
        raise ValueError(f"CDK context conflicts with profile coordinates: {', '.join(conflicts)}")

# A new installation must never derive AWS coordinates or secret references from a GitHub
# identity, an ambient account, or this project's former private deployment. The compatibility
# profile explicitly opts into the legacy input contract used by existing protected workflows.
if not ctx_bool("legacy_scope", False):
    missing = [key for key in ("name", "env", "account", "region", "secret_arn") if not ctx(key)]
    if missing:
        raise ValueError(f"New deployment requires explicit profile inputs: {', '.join(missing)}")

naming = Naming(name=ctx("name", "strandly") or "strandly", env=ctx("env", "dev") or "dev", legacy_scope=ctx_bool("legacy_scope", False))

def sandbox_assume_roles() -> tuple[str, ...]:
    return tuple(item for item in (ctx_list("sandbox_assume_roles") or ()) if item != "none")


def runtime_invokers() -> tuple[str, ...]:
    return tuple(item for item in (ctx_list("runtime_invokers") or ()) if item != "none")


env = cdk.Environment(account=ctx("account"), region=ctx("region"))

# Tokens never enter CDK context: a context value lands in plaintext in the template and cdk.out.
# They go straight into the config secret (docs/deploy.md#configure-integrations).
if ctx("github_token"):
    raise ValueError(
        "-c github_token is not supported: it would put the token in plaintext in the "
        "CloudFormation template. Store STRANDLY_GITHUB_TOKEN in the config secret instead."
    )

# GitHub Actions OIDC: the privileged deploy role and the invoke-only role (agent runtime, `main`
# only). Account-global provider — pass `-c oidc_provider_arn=…` to reuse an existing
# one (e.g. a second env). Has working defaults, so it's always synthesized; deploy it on its own.
if ctx("github_repo"):
    OidcStack(
        app,
        naming.stack("Oidc"),
        naming=naming,
        github_repo=ctx("github_repo") or "",
        deploy_subjects=ctx_list("deploy_subjects"),
        invoke_subjects=ctx_list("invoke_subjects"),
        extra_invoke_repos=ctx_list("extra_invoke_repos"),
        oidc_provider_arn=ctx("oidc_provider_arn"),
        runtime_arn=ctx("runtime_arn"),
        deploy_policy=ctx("deploy_policy", "scoped") or "scoped",
        env=env,
    )

data = DataStack(app, naming.stack("Data"), naming=naming, env=env)

backend = BackendStack(
    app,
    naming.stack("Backend"),
    naming=naming,
    # The retired knowledge base (its resources are RETAIN). Existing deployments that still hold
    # it set -c with_kb=true until its data is migrated.
    with_kb=ctx_bool("with_kb", False),
    # The Code Interpreter's Bedrock/ABAC execution role. The harness has no Code Interpreter
    # backend (sandbox code gets Bedrock through sandbox_bedrock_grants), so it is opt-in.
    ci_bedrock_role=ctx_bool("ci_bedrock_role", False),
    env=env,
)

# Dashboard reads the run-ledger table by its (deterministic) name — NOT by importing Data's live
# ITable. A cross-stack import emits an Fn::ImportValue + a CloudFormation export, and an export
# can't be modified while imported, which deadlocks a re-deploy of Data. Deriving from `naming`
# keeps the lifecycle boundary (Data owns the table) without the export coupling. See DashboardStack.
global_schedules_enabled = ctx_bool("schedules_enabled", True)
# Active scheduled jobs the team holds across every surface (harness default 100 when unset).
_cap = ctx("schedules_max_active")
schedules_max_active = int(_cap) if _cap not in (None, "") else None
# GitHub Actions ingress (/api/actions/invoke, OIDC). Created with the runtime; see docs/deploy.md.
_actions_defaults = ActionsIngress()
actions_ingress = (
    ActionsIngress(
        audience=ctx("actions_audience") or _actions_defaults.audience,
        repositories=tuple(ctx_list("actions_repositories") or _actions_defaults.repositories),
        workflows=tuple(ctx_list("actions_workflows") or ()),
        default_branch=ctx("actions_default_branch") or _actions_defaults.default_branch,
    )
    if ctx_bool("actions_enabled", True)
    else None
)

dashboard = DashboardStack(
    app,
    naming.stack("Dashboard"),
    naming=naming,
    cognito_domain_prefix=ctx("cognito_domain_prefix"),
    portal_identity=app.node.try_get_context("portal_identity") or {},
    schedules_enabled=global_schedules_enabled,
    schedules_max_active=schedules_max_active,
    # The Vite dev ports as sign-in redirects and CORS origins, for `npm run dev` against this
    # deployment. Never on by default outside env=dev.
    dev_callbacks=ctx_bool("dev_callbacks", naming.env == "dev"),
    # When a runtime arn is known (post `strandly deploy`), wire up the dashboard's chat tab:
    # the read Lambda gets scoped InvokeAgentRuntime + STRANDLY_RUNTIME_ARN. Absent it, chat 503s.
    runtime_arn=ctx("runtime_arn"),
    # When a memory id is known (the Backend stack's MemoryId output), the dashboard reads each
    # session's verbatim transcript from AgentCore Memory (scoped ListEvents). Absent it, the
    # transcript falls back to the ledger reconstruction.
    memory_id=ctx("memory_id"),
    actions=actions_ingress,
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
        all_enabled=global_schedules_enabled,
        env=env,
    )

    invokers = runtime_invokers()
    if invokers:
        RuntimeAccessStack(
            app,
            naming.stack("RuntimeAccess"),
            runtime_arn=runtime_arn,
            invoker_arns=invokers,
            env=env,
        )

    # Slack DM ingress: push-based (Slack calls us). Gated on BOTH a deployed runtime to
    # dispatch to AND the externally-created Slack secret (bot token + signing secret) — same
    # conditional-synthesis pattern as Ingress/Scheduler above. Reuses the poller Lambda asset.
    slack_secret_arn = ctx("slack_secret_arn")
    if slack_secret_arn:
        slack_stack = SlackStack(
            app,
            naming.stack("Slack"),
            naming=naming,
            runtime_arn=runtime_arn,
            slack_secret_arn=slack_secret_arn,
            poller_asset=ctx("poller_asset"),
            alarm_topic=home_channel_enabled(ctx("slack_home_channel_enabled")) and ctx_bool("monitoring_enabled", True),
            home_channel=home_channel_enabled(ctx("slack_home_channel_enabled")),
            env=env,
        )

# RuntimeIam attaches to the agent runtime's exec role by name (the AgentRuntime stack's
# AgentExecRoleName output, a fixed name — Naming.agent_runtime_exec_role). The context value is a
# comma-separated list (both stacks below sort it); a deploy pipeline passes exactly that one role.
exec_role_names = ctx_list("exec_role_name") or []
if exec_role_names:
    RuntimeIamStack(
        app,
        naming.stack("RuntimeIam"),
        naming=naming,
        exec_role_names=exec_role_names,
        memory_id=ctx("memory_id"),
        sandbox_runtime_arn=ctx("sandbox_runtime_arn"),
        run_ledger_table=ctx("run_ledger_table") or naming.run_ledger_table,
        work_product_table=ctx("work_product_table") or naming.work_product_table,
        config_secret_arn=ctx("secret_arn"),
        # The session store (DataStack's sessions bucket) by its deterministic name — read/write
        # on its three prefixes + the session CMK. Account from the shared env (a token in an
        # env-agnostic synth, resolved at deploy).
        session_bucket=naming.session_bucket(data.account),
        env=env,
    )

# Sandbox: the sandbox AgentCore Runtime, its ARM64 image asset, and its near-powerless exec role.
# ALWAYS synthesized — no `if` gate and no context knob, which is the point of the change: the
# sandbox was the one resource whose existence depended on a human running `docker build` and then
# `strandly deploy-sandbox`, and whose ARN reached the agent by being copied into a repo variable.
# `cdk deploy` builds it now, and a deploy pipeline reads the ARN from this stack's output.
#
# NOTE: `cdk deploy` of this stack needs a working `docker` that can build linux/arm64 (AgentCore
# Runtime is arm64-only); on an x86 runner that means QEMU/binfmt. `cdk synth` does
# NOT: CDK stages the build context and computes the asset hash in-process, and only the deploy
# shells out to `docker build`. That is what lets an infra test synthesize this app with no
# docker and no AWS credentials (verified with `docker` replaced by a stub that exits 127 — it is
# never invoked).
SandboxStack(
    app,
    naming.stack("Sandbox"),
    naming=naming,
    # The build context is sandbox/ (Dockerfile.sandbox lives there); the stack stages only
    # the files that Dockerfile actually needs, so the asset hash ignores everything else.
    asset_directory=str(Path(__file__).resolve().parent.parent / "sandbox"),
    # Bedrock/ABAC test grants so the sandbox can e2e-test Strands against live Bedrock. On by
    # default; set false for a credential-free sandbox.
    bedrock_test_grants=ctx_bool("sandbox_bedrock_grants", True),
    # Opt-in SageMaker endpoint lifecycle for live SageMakerAIModel tests. Off by default because a
    # leaked GPU endpoint bills hourly. Context wins, then the deploy env's GitHub variable.
    sagemaker_test_grants=ctx_bool(
        "sandbox_sagemaker_grants",
        os.environ.get("STRANDLY_SANDBOX_SAGEMAKER_GRANTS", "").lower() == "true",
    ),
    # Exact cross-account role grants, selected explicitly by the deployment profile.
    assumable_role_arns=sandbox_assume_roles(),
    # Explicit JSON context overrides the deployment env, including {} to disable. Public IDs only;
    # provider setup and AWS outbound federation enablement are one-time manual prerequisites.
    provider_federation=load_provider_federation(
        app.node.try_get_context("sandbox_provider_federation"),
        os.environ.get("STRANDLY_SANDBOX_PROVIDER_FEDERATION"),
    ),
    # No platform version: the sandbox runs on AgentCore Runtime instances (the env's one VPC, which
    # this stack owns, + an EC2 capacity provider, a persistent EBS workspace per session).
    env=env,
)

# AgentRuntime: the AGENT runtime on the AgentCore microVM (the code-zip runtime + its exec role).
# No flag: it is synthesized whenever its inputs are in context — the Backend stack's
# SecretArn/MemoryId and the Sandbox stack's SandboxRuntimeArn + TTL ids — which every deploy step
# after provisioning passes (`strandly deploy` or a deploy pipeline). An env-agnostic synth has none
# of them and leaves it out, exactly as Ingress/Scheduler wait for a runtime ARN. With the inputs
# present the code bundle is REQUIRED (built by infra/scripts/build_agent_bundle.py, default
# infra/build/agent): a missing bundle fails the synth rather than silently dropping the agent
# runtime from the app. See docs/deploy.md.
_agent_inputs = {
    key: ctx(key) or ""
    for key in (
        "secret_arn",
        "memory_id",
        "sandbox_runtime_arn",
        "sandbox_capacity_provider_id",
        "sandbox_ttl_schedule_group",
        "sandbox_ttl_role_arn",
        # Read only by the retained Instances standby (-c agent_instances_standby=true).
        "agent_subnet_ids",
        "agent_security_group_id",
    )
}
# Gate on the three ids every post-provision step passes; once they are there the stack REQUIRES the
# Sandbox stack's three TTL ids too (it raises on an empty one rather than shipping an agent that
# cannot expire sandbox sessions). So every step that passes secret_arn + memory_id +
# sandbox_runtime_arn must pass all six — see tests/test_workflow_synth.py, which synthesizes every
# `cdk` invocation in the workflows.
if all(_agent_inputs[key] for key in ("secret_arn", "memory_id", "sandbox_runtime_arn")):
    _agent_asset = Path(
        ctx("agent_code_asset") or Path(__file__).resolve().parent / "build" / "agent"
    )
    if not (_agent_asset / "main.py").is_file():
        raise SystemExit(
            f"the AgentRuntime stack needs the agent bundle at {_agent_asset} (build it with "
            "`python infra/scripts/build_agent_bundle.py --out infra/build/agent --from-lock`, or "
            "pass -c agent_code_asset=<dir>)."
        )
    AgentRuntimeStack(
        app,
        naming.stack("AgentRuntime"),
        naming=naming,
        code_asset_dir=str(_agent_asset),
        retain_instances_standby=ctx_bool("agent_instances_standby", False),
        actions_schedule_repositories=",".join(ctx_list("actions_schedule_repositories") or ()),
        schedules_max_active=schedules_max_active,
        traces_sampler=ctx("traces_sampler") or DEFAULT_TRACES_SAMPLER,
        traces_sampler_arg=ctx("traces_sampler_arg") or "",
        **_agent_inputs,
        env=env,
    )

# WebSearchStack: the AgentCore Gateway's Web Search Tool connector is us-east-1-only at launch,
# regardless of where the rest of the deployment lives — always its own explicit environment, never
# the shared `env`. Like RuntimeIam, it grants the agent runtime's exec role, so it only synthesizes
# once `-c exec_role_name=…` is known. Disable with -c websearch_enabled=false.
if exec_role_names and ctx_bool("websearch_enabled", True):
    WebSearchStack(
        app,
        naming.stack("WebSearch"),
        naming=naming,
        exec_role_names=exec_role_names,
        env=cdk.Environment(account=ctx("account"), region="us-east-1"),
    )


# Cost-allocation tags (applied last so every construct above is tagged).
cdk.Tags.of(app).add("app", "strandly")
cdk.Tags.of(app).add("env", naming.env)

# --- Monitoring: operational alarms + the stuck-run detector ---------------------------------
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
        stuck_run_minutes=int(ctx("stuck_run_minutes") or STUCK_RUN_MINUTES),
        schedule_enabled=ctx_bool("schedule_enabled", True),
        # Runtime log retention + sandbox alarms (a deploy pipeline passes both ARNs to this step).
        runtime_arn=ctx("runtime_arn"),
        sandbox_runtime_arn=ctx("sandbox_runtime_arn"),
        env=env,
    )
    if "slack_stack" in globals():
        slack_stack.add_dependency(monitoring)
elif ctx_bool("monitoring_enabled", True):
    print(f"⚠ MonitoringStack skipped: poller asset not built at {_asset_dir} "
          "(run infra/scripts/build-poller-package.sh --local infra/build/poller).")

# --- Audit: the independent, out-of-band GitHub write-audit job ------------------------------
# Enabled by default (like Monitoring) once a secret is known (it reads the audit/github token
# from it) and the poller asset is built. `audit_allowed_owners` is now OPTIONAL — omitted, the
# audit reads the SAME allow-list the write guardrail enforces (Config.audit falls back to
# Config.github.allowed_owners, both sourced from the secret at runtime), so a plain `cdk deploy`
# with no extra context turns the audit on rather than silently skipping it (the actual bug that
# left this unset in prod — see infra/stacks/audit_stack.py's module docstring).
audit_owners = ctx("audit_allowed_owners")
secret_arn = ctx("secret_arn")
if ctx_bool("audit_enabled", True) and secret_arn and _asset_available:
    AuditStack(
        app,
        naming.stack("Audit"),
        naming=naming,
        allowed_owners=audit_owners,
        secret_arn=secret_arn,
        lookback_hours=int(ctx("audit_lookback_hours", "24") or "24"),
        schedule_expression=ctx("audit_schedule", "rate(6 hours)") or "rate(6 hours)",
        schedule_enabled=ctx_bool("schedule_enabled", True),
        alarm_email=alarm_email,
        poller_asset=_poller_asset,
        env=env,
    )
elif ctx_bool("audit_enabled", True) and not secret_arn:
    print("⚠ AuditStack skipped: no -c secret_arn=… (it reads the audit/github token from the secret).")
elif ctx_bool("audit_enabled", True):
    print(f"⚠ AuditStack skipped: poller asset not built at {_asset_dir}.")

# --- Cost: AWS Cost Anomaly Detection (real billing data; no token-derived $ metric) ---------
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


apply_deployment_extensions(tuple(ctx_list("deployment_extensions") or ()), app, naming)
# Runtime defaults are deployment metadata, below secret and process overrides.
profile_defaults = ctx("profile_defaults")
if profile_defaults:
    for node in app.node.find_all():
        if isinstance(node, lambda_.Function) and (getattr(node.node.default_child, "handler", "") or "").startswith("strandly"):
            node.add_environment("STRANDLY_PROFILE_DEFAULTS", profile_defaults)
        if node.__class__.__name__ == "CfnRuntime" and "AgentRuntime" in node.node.path:
            node.add_property_override("EnvironmentVariables.STRANDLY_PROFILE_DEFAULTS", profile_defaults)

# Explicit operator setting; process env overrides any secret/profile default.


home_enabled = "true" if home_channel_enabled(ctx("slack_home_channel_enabled")) else "false"
for node in app.node.find_all():
    if isinstance(node, lambda_.Function) and (getattr(node.node.default_child, "handler", "") or "").startswith("strandly"):
        node.add_environment(SLACK_HOME_CHANNEL_ENABLED, home_enabled)
    if node.__class__.__name__ == "CfnRuntime" and "AgentRuntime" in node.node.path:
        node.add_property_override(f"EnvironmentVariables.{SLACK_HOME_CHANNEL_ENABLED}", home_enabled)

app.synth()
