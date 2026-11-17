"""Synth-level assertions for each stack — the contract a `cdk synth` should always satisfy.

Runs under the CDK venv (imports `aws_cdk`), so it is NOT part of the harness `pytest` gate. These
catch template-shape regressions a value-drift guard can't: a missing GSI, a wrong IAM action, a
broken cross-stack import, the wrong removal policy in prod, the gating of the conditional stacks.

    cd infra && pip install -r requirements.txt pytest && pytest tests/
"""

from __future__ import annotations

import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

# Make `stacks` importable when pytest runs from the infra/ dir or the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stacks.backend_stack import BackendStack  # noqa: E402
from stacks.common import MENTION_LOG_GSI, RUN_LEDGER_GSI, Naming  # noqa: E402
from stacks.dashboard_stack import DashboardStack  # noqa: E402
from stacks.data_stack import DataStack  # noqa: E402
from stacks.ingress_stack import IngressStack  # noqa: E402
from stacks.oidc_stack import OidcStack  # noqa: E402
from stacks.runtime_iam_stack import RuntimeIamStack  # noqa: E402
from stacks.scheduler_stack import SchedulerStack, load_jobs  # noqa: E402

_ENV = cdk.Environment(account="111111111111", region="us-west-2")
_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:111111111111:runtime/strandly-test"


def _naming(env: str = "dev") -> Naming:
    return Naming(name="strandly", env=env)


# ---- Data ------------------------------------------------------------------------------

def test_data_stack_three_tables_and_recent_gsis():
    app = cdk.App()
    stack = DataStack(app, "Strandly-Data-dev", naming=_naming(), env=_ENV)
    t = Template.from_stack(stack)
    t.resource_count_is("AWS::DynamoDB::Table", 3)
    # The run-ledger carries the "recent" GSI the dashboard queries.
    t.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "GlobalSecondaryIndexes": Match.array_with(
                [Match.object_like({"IndexName": RUN_LEDGER_GSI})]
            )
        },
    )
    # The mention log (Mentions tab) has the same newest-first GSI shape, keyed on seen_at, + TTL.
    t.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "TableName": "strandly-dev-mentionlog",
            "TimeToLiveSpecification": Match.object_like({"AttributeName": "ttl", "Enabled": True}),
            "GlobalSecondaryIndexes": Match.array_with(
                [
                    Match.object_like(
                        {
                            "IndexName": MENTION_LOG_GSI,
                            "KeySchema": [
                                {"AttributeName": "gsi_pk", "KeyType": "HASH"},
                                {"AttributeName": "seen_at", "KeyType": "RANGE"},
                            ],
                        }
                    )
                ]
            ),
        },
    )


def test_data_tables_retain_in_prod_destroy_in_dev():
    for env, policy in (("prod", "Retain"), ("dev", "Delete")):
        app = cdk.App()
        stack = DataStack(app, f"Strandly-Data-{env}", naming=_naming(env), env=_ENV)
        t = Template.from_stack(stack)
        for res in t.find_resources("AWS::DynamoDB::Table").values():
            assert res["DeletionPolicy"] == policy, f"{env}: expected {policy}"


# ---- Backend ---------------------------------------------------------------------------

def test_backend_stack_core_resources_and_secret():
    app = cdk.App()
    stack = BackendStack(app, "Strandly-Backend-dev", naming=_naming(), with_kb=True, env=_ENV)
    t = Template.from_stack(stack)
    t.resource_count_is("AWS::BedrockAgentCore::Memory", 1)
    t.resource_count_is("AWS::BedrockAgentCore::CodeInterpreterCustom", 1)
    t.resource_count_is("AWS::Bedrock::KnowledgeBase", 1)
    t.resource_count_is("AWS::Bedrock::DataSource", 1)
    t.resource_count_is("AWS::S3Vectors::VectorBucket", 1)
    t.resource_count_is("AWS::S3Vectors::Index", 1)
    t.resource_count_is("AWS::SecretsManager::Secret", 1)


def test_backend_data_source_is_custom():
    app = cdk.App()
    stack = BackendStack(app, "Strandly-Backend-dev", naming=_naming(), with_kb=True, env=_ENV)
    t = Template.from_stack(stack)
    t.has_resource_properties(
        "AWS::Bedrock::DataSource",
        {"DataSourceConfiguration": Match.object_like({"Type": "CUSTOM"})},
    )


def test_backend_no_kb_when_disabled():
    app = cdk.App()
    stack = BackendStack(app, "Strandly-Backend-dev", naming=_naming(), with_kb=False, env=_ENV)
    t = Template.from_stack(stack)
    t.resource_count_is("AWS::Bedrock::KnowledgeBase", 0)
    t.resource_count_is("AWS::S3Vectors::VectorBucket", 0)
    # Memory + Code Interpreter + secret still exist.
    t.resource_count_is("AWS::BedrockAgentCore::Memory", 1)
    t.resource_count_is("AWS::SecretsManager::Secret", 1)


def test_backend_prod_resources_tagged_infra():
    # Prod backends must carry ManagedBy=strandly-infra so the agent's ManagedBy=strandly grants
    # can never reach them. This tag is the load-bearing half of the self-protection boundary.
    app = cdk.App()
    stack = BackendStack(app, "Strandly-Backend-dev", naming=_naming(), with_kb=True, env=_ENV)
    t = Template.from_stack(stack)
    t.has_resource_properties(
        "AWS::BedrockAgentCore::Memory",
        {"Tags": Match.object_like({"ManagedBy": "strandly-infra"})},
    )
    t.has_resource_properties(
        "AWS::Bedrock::KnowledgeBase",
        {"Tags": Match.object_like({"ManagedBy": "strandly-infra"})},
    )


def test_backend_no_ci_role_by_default():
    # Default: the Code Interpreter is credential-free (no execution role, no CI role resource).
    app = cdk.App()
    stack = BackendStack(app, "Strandly-Backend-dev", naming=_naming(), with_kb=True, env=_ENV)
    t = Template.from_stack(stack)
    body = str(t.to_json())
    assert "CiExecutionRole" not in body
    ci = next(
        v for v in t.find_resources("AWS::BedrockAgentCore::CodeInterpreterCustom").values()
    )
    assert "ExecutionRoleArn" not in ci["Properties"]


def test_backend_ci_role_is_abac_and_invoke_only_when_opted_in():
    app = cdk.App()
    stack = BackendStack(
        app, "Strandly-Backend-dev", naming=_naming(), with_kb=True, ci_bedrock_role=True, env=_ENV
    )
    t = Template.from_stack(stack)
    body = str(t.to_json())
    # The CI now has an execution role.
    ci = next(
        v for v in t.find_resources("AWS::BedrockAgentCore::CodeInterpreterCustom").values()
    )
    assert "ExecutionRoleArn" in ci["Properties"]
    # ABAC: create gated on aws:RequestTag, operate gated on aws:ResourceTag.
    assert "aws:RequestTag/ManagedBy" in body
    assert "aws:ResourceTag/ManagedBy" in body
    # Model access is invoke-only — no control-plane create-model, and crucially no role creation.
    assert "iam:CreateRole" not in body
    assert "iam:CreatePolicy" not in body
    # Re-tag escalation is closed: an explicit Deny on (Un)TagResource of any strandly-infra
    # resource, so the agent can't re-tag prod into managed scope and then delete/read it.
    t.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": Match.object_like(
                {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Effect": "Deny",
                                    "Action": ["bedrock:TagResource", "bedrock:UntagResource"],
                                    "Condition": {
                                        "StringEquals": {
                                            "aws:ResourceTag/ManagedBy": "strandly-infra"
                                        }
                                    },
                                }
                            )
                        ]
                    )
                }
            )
        },
    )
    # PassRole is scoped (the fixed managed-kb service role), with the bedrock service condition.
    t.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": Match.object_like(
                {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": "iam:PassRole",
                                    "Condition": {
                                        "StringEquals": {
                                            "iam:PassedToService": "bedrock.amazonaws.com"
                                        }
                                    },
                                }
                            )
                        ]
                    )
                }
            )
        },
    )


# ---- Ingress ---------------------------------------------------------------------------

def test_ingress_stack_lambda_schedule_and_scoped_invoke(tmp_path):
    # Provide a throwaway asset dir so Code.from_asset doesn't error.
    asset = tmp_path / "poller"
    asset.mkdir()
    (asset / "placeholder").write_text("x")

    app = cdk.App()
    stack = IngressStack(
        app,
        "Strandly-Ingress-dev",
        naming=_naming(),
        runtime_arn=_RUNTIME_ARN,
        mention_handle="bot",
        allowed_authors="alice",
        poller_asset=str(asset),
        env=_ENV,
    )
    t = Template.from_stack(stack)
    t.resource_count_is("AWS::Lambda::Function", 1)
    t.resource_count_is("AWS::Scheduler::Schedule", 1)
    # The poller MUST get the metric namespace, or metrics.emit() is a no-op → PollSuccess never
    # lands → MonitoringStack's poll-silent alarm can never clear. Regression guard for that prod
    # bug. Must equal naming.metrics_namespace (what the alarm reads).
    t.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": Match.object_like(
                    {"STRANDLY_METRICS_NAMESPACE": _naming().metrics_namespace}
                )
            }
        },
    )
    # The dedup table is referenced by deterministic name, NOT a cross-stack import (no deadlock).
    assert "Fn::ImportValue" not in str(t.to_json())
    # ...but the poller's grant still targets the dedup table ARN.
    assert "table/strandly-dev-dedup" in str(t.to_json())
    # Same by-name pattern for the mention log: env var wired, write grant present.
    assert "table/strandly-dev-mentionlog" in str(t.to_json())
    t.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": Match.object_like(
                    {"STRANDLY_MENTION_LOG_TABLE": "strandly-dev-mentionlog"}
                )
            }
        },
    )
    # The poller may invoke ONLY the one runtime (+ its sessions), not "*".
    t.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": Match.object_like(
                {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": "bedrock-agentcore:InvokeAgentRuntime",
                                    "Resource": Match.array_with([_RUNTIME_ARN]),
                                }
                            )
                        ]
                    )
                }
            )
        },
    )


# ---- RuntimeIam ------------------------------------------------------------------------

def test_runtime_iam_grants_kb_and_ledger_when_given():
    app = cdk.App()
    stack = RuntimeIamStack(
        app,
        "Strandly-RuntimeIam-dev",
        naming=_naming(),
        exec_role_name="some-exec-role",
        kb_id="KB123",
        run_ledger_table="strandly-dev-runledger",
        env=_ENV,
    )
    t = Template.from_stack(stack)
    # A KB grant scoped to the given knowledge-base id, and a ledger PutItem grant.
    body = str(t.to_json())
    assert "knowledge-base/KB123" in body
    assert "strandly-dev-runledger" in body
    assert "dynamodb:PutItem" in body


def test_runtime_iam_grants_config_secret_read_when_given():
    # With a config secret ARN, the exec role gets GetSecretValue on exactly that secret — so the
    # runtime can Config.load from Secrets Manager and token rotation needs no runtime redeploy.
    secret_arn = "arn:aws:secretsmanager:us-west-2:111111111111:secret:strandly/dev/config-abc123"
    app = cdk.App()
    stack = RuntimeIamStack(
        app,
        "Strandly-RuntimeIam-dev",
        naming=_naming(),
        exec_role_name="some-exec-role",
        config_secret_arn=secret_arn,
        env=_ENV,
    )
    t = Template.from_stack(stack)
    t.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": Match.object_like(
                {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": "secretsmanager:GetSecretValue",
                                    "Resource": secret_arn,
                                }
                            )
                        ]
                    )
                }
            )
        },
    )


def test_runtime_iam_no_secret_grant_without_arn():
    # No secret ARN → no Secrets Manager grant at all (opt-in, least privilege).
    app = cdk.App()
    stack = RuntimeIamStack(
        app, "Strandly-RuntimeIam-dev", naming=_naming(), exec_role_name="r", env=_ENV
    )
    assert "secretsmanager:GetSecretValue" not in str(Template.from_stack(stack).to_json())


# ---- Dashboard -------------------------------------------------------------------------

def _dashboard(runtime_arn: str | None, memory_id: str | None = None):
    app = cdk.App()
    stack = DashboardStack(
        app,
        "Strandly-Dashboard-dev",
        naming=_naming(),
        runtime_arn=runtime_arn,
        memory_id=memory_id,
        env=_ENV,
    )
    return Template.from_stack(stack)


def test_dashboard_references_run_ledger_without_cross_stack_import():
    # The run-ledger is referenced by deterministic name, not a cross-stack import — so no
    # Fn::ImportValue/export, so Data can be re-deployed while the dashboard is live (the bug fix).
    t = _dashboard(None)
    assert "Fn::ImportValue" not in str(t.to_json())
    # The read Lambda's grant must let it Query the "recent" GSI, so a single statement carries
    # dynamodb:Query AND both the table and /index/* ARNs (regression guard: if grant_index_permissions
    # or the GSI ref is dropped, the index ARN detaches from Query and the dashboard's Runs tab breaks).
    # The ARNs are concrete literal strings (the helper builds them from the deterministic name),
    # so match the statement directly: dynamodb:Query present, and both the table ARN and the
    # /index/* ARN on the same statement's Resource list.
    t.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": Match.object_like(
                {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": Match.array_with(["dynamodb:Query"]),
                                    "Resource": Match.array_with(
                                        [
                                            "arn:aws:dynamodb:us-west-2:111111111111:table/strandly-dev-runledger",
                                            "arn:aws:dynamodb:us-west-2:111111111111:table/strandly-dev-runledger/index/*",
                                        ]
                                    ),
                                }
                            )
                        ]
                    )
                }
            )
        },
    )


def test_dashboard_has_sessions_and_chat_routes():
    t = _dashboard(_RUNTIME_ARN)
    routes = {
        v["Properties"]["RouteKey"]
        for v in t.find_resources("AWS::ApiGatewayV2::Route").values()
    }
    # The new sessions + chat surface, alongside the original read routes.
    assert "GET /api/sessions" in routes
    assert "GET /api/sessions/{id}" in routes
    assert "GET /api/chat" in routes
    assert "POST /api/chat" in routes
    assert "GET /api/runs" in routes  # unchanged routes still present
    assert "GET /api/runs/{id}/logs" in routes  # the new runtime-logs route


def test_dashboard_grants_scoped_invoke_when_runtime_given():
    t = _dashboard(_RUNTIME_ARN)
    # The read Lambda may invoke ONLY the one runtime (+ its sessions) for chat.
    t.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": Match.object_like(
                {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": "bedrock-agentcore:InvokeAgentRuntime",
                                    "Resource": Match.array_with([_RUNTIME_ARN]),
                                }
                            )
                        ]
                    )
                }
            )
        },
    )
    # And the runtime arn is wired into the Lambda env so chat is enabled.
    assert _RUNTIME_ARN in str(t.to_json())


def test_dashboard_no_invoke_grant_without_runtime():
    t = _dashboard(None)
    # Chat off: no InvokeAgentRuntime grant anywhere when no runtime arn is supplied.
    assert "bedrock-agentcore:InvokeAgentRuntime" not in str(t.to_json())


def test_dashboard_describe_alarms_grant_is_unscoped():
    # cloudwatch:DescribeAlarms is a list-style action that does NOT support resource-level
    # permissions — scoping it to an alarm ARN yields AccessDenied at runtime (the health strip's
    # "alarm read failed"). The IAM resource must be "*"; result-scoping is done by the handler's
    # AlarmNamePrefix. Regression guard for that prod bug.
    t = _dashboard(_RUNTIME_ARN)
    t.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": Match.object_like(
                {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": "cloudwatch:DescribeAlarms",
                                    "Resource": "*",
                                }
                            )
                        ]
                    )
                }
            )
        },
    )
    # And the prefix that scopes the *results* is wired into the Lambda env.
    assert "strandly-dev-" in str(t.to_json())


def test_dashboard_grants_scoped_log_read_when_runtime_given():
    t = _dashboard(_RUNTIME_ARN)
    body = str(t.to_json())
    # Runtime logs in the drawer: the read Lambda may FilterLogEvents on the ONE runtime log group,
    # and the derived group name is wired into its env.
    runtime_id = _RUNTIME_ARN.split("/")[-1]
    log_group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
    assert "logs:FilterLogEvents" in body
    assert "logs:DescribeLogStreams" in body  # needed to match the date-prefixed stream name
    assert log_group in body  # both the env var and the IAM resource reference it
    t.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": Match.object_like(
                {
                    "Statement": Match.array_with(
                        [Match.object_like(
                            {"Action": ["logs:DescribeLogStreams", "logs:FilterLogEvents"]}
                        )]
                    )
                }
            )
        },
    )


def test_dashboard_no_log_grant_without_runtime():
    t = _dashboard(None)
    assert "logs:FilterLogEvents" not in str(t.to_json())


_MEMORY_ID = "strandly-memory-abc123"


def test_dashboard_grants_scoped_list_events_when_memory_given():
    t = _dashboard(None, _MEMORY_ID)
    # The read Lambda may ListEvents ONLY on the one memory resource (+ its sessions) for transcripts.
    t.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": Match.object_like(
                {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": "bedrock-agentcore:ListEvents",
                                    "Resource": Match.array_with(
                                        [Match.string_like_regexp(f".*memory/{_MEMORY_ID}.*")]
                                    ),
                                }
                            )
                        ]
                    )
                }
            )
        },
    )
    # And the memory id is wired into the Lambda env so the transcript route reads from Memory.
    t.has_resource_properties(
        "AWS::Lambda::Function",
        {"Environment": {"Variables": Match.object_like({"AGENTCORE_MEMORY_ID": _MEMORY_ID})}},
    )


def test_dashboard_no_list_events_grant_without_memory():
    t = _dashboard(_RUNTIME_ARN, None)
    # Transcript-from-Memory off: no ListEvents grant and no AGENTCORE_MEMORY_ID env.
    assert "bedrock-agentcore:ListEvents" not in str(t.to_json())
    assert "AGENTCORE_MEMORY_ID" not in str(t.to_json())




# ---- Oidc --------------------------------------------------------------------------------

_REPO = "mkmeral/strandly-harness"


def _oidc(**kwargs):
    app = cdk.App()
    stack = OidcStack(
        app,
        "Strandly-Oidc-dev",
        naming=_naming(),
        github_repo=kwargs.pop("github_repo", _REPO),
        env=_ENV,
        **kwargs,
    )
    return Template.from_stack(stack)


def _policy_doc_with_sid(t: Template, sid: str) -> str:
    """Return the JSON of the single AWS::IAM::Policy whose document contains the given Sid.

    Each role's `add_to_policy` renders as its own DefaultPolicy resource, so this lets a test
    isolate the deploy vs invoke policy and assert what it does (and does NOT) grant.
    """
    import json

    for res in t.find_resources("AWS::IAM::Policy").values():
        doc = res["Properties"]["PolicyDocument"]
        blob = json.dumps(doc)
        if f'"{sid}"' in blob:
            return blob
    raise AssertionError(f"no IAM::Policy with Sid {sid!r}")


def test_oidc_creates_provider_and_two_roles():
    t = _oidc()
    t.resource_count_is("Custom::AWSCDKOpenIdConnectProvider", 1)
    # Our two named roles (a third, unnamed role belongs to the provider's CR handler).
    role_names = {
        v["Properties"].get("RoleName")
        for v in t.find_resources("AWS::IAM::Role").values()
    }
    assert {"strandly-dev-gha-deploy", "strandly-dev-gha-invoke"} <= role_names


def test_oidc_imports_provider_when_arn_given():
    # Importing an existing account-global provider must NOT create a new one (the devx knob).
    t = _oidc(oidc_provider_arn="arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com")
    t.resource_count_is("Custom::AWSCDKOpenIdConnectProvider", 0)
    # Importing skips the CR handler role too — only our two named roles remain.
    t.resource_count_is("AWS::IAM::Role", 2)
    role_names = {
        v["Properties"].get("RoleName")
        for v in t.find_resources("AWS::IAM::Role").values()
    }
    assert {"strandly-dev-gha-deploy", "strandly-dev-gha-invoke"} <= role_names


def test_oidc_deploy_role_trust_locked_to_protected_refs_by_default():
    t = _oidc()
    # The deploy role's trust must pin aud=sts.amazonaws.com and a sub of the repo's main ref.
    body = str(t.to_json())
    assert "repo:mkmeral/strandly-harness:ref:refs/heads/main" in body
    assert "repo:mkmeral/strandly-harness:environment:production" in body
    assert "sts.amazonaws.com" in body
    # Web-identity federation (not a static principal).
    t.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": Match.object_like(
                {
                    "Statement": Match.array_with(
                        [Match.object_like({"Action": "sts:AssumeRoleWithWebIdentity"})]
                    )
                }
            )
        },
    )


def test_oidc_deploy_role_has_privileged_grants():
    t = _oidc()
    deploy_doc = _policy_doc_with_sid(t, "DeployServices")
    assert "cloudformation:*" in deploy_doc
    assert "bedrock-agentcore:*" in deploy_doc
    iam_doc = _policy_doc_with_sid(t, "DeployIam")
    assert "iam:PassRole" in iam_doc
    assert "iam:CreateRole" in iam_doc


def test_oidc_invoke_role_is_minimal_and_separate():
    t = _oidc()
    invoke_doc = _policy_doc_with_sid(t, "InvokeRuntime")
    assert "bedrock-agentcore:InvokeAgentRuntime" in invoke_doc
    # The whole point of the split: the invoke policy can NOT deploy/redeploy the agent.
    assert "cloudformation" not in invoke_doc
    assert "iam:CreateRole" not in invoke_doc
    assert "iam:PassRole" not in invoke_doc


def test_oidc_invoke_unscoped_uses_runtime_wildcard():
    t = _oidc()
    invoke_doc = _policy_doc_with_sid(t, "InvokeRuntime")
    # No runtime arn given → region/account-wide runtime wildcard (never a bare "*").
    assert ":runtime/*" in invoke_doc
    assert '"*"' not in invoke_doc


def test_oidc_invoke_scoped_to_runtime_and_memory_when_given():
    t = _oidc(runtime_arn=_RUNTIME_ARN, memory_id="strandly-memory-abc123")
    invoke_doc = _policy_doc_with_sid(t, "InvokeRuntime")
    assert _RUNTIME_ARN in invoke_doc
    poll_doc = _policy_doc_with_sid(t, "PollMemory")
    assert "memory/strandly-memory-abc123" in poll_doc
    assert "bedrock-agentcore:ListEvents" in poll_doc


def test_oidc_deploy_policy_admin_escape_hatch():
    t = _oidc(deploy_policy="admin")
    body = str(t.to_json())
    assert "AdministratorAccess" in body
    # In admin mode the curated deploy statements are not emitted.
    assert "DeployServices" not in body


def test_oidc_custom_subjects_override_defaults():
    t = _oidc(
        deploy_subjects=["repo:mkmeral/strandly-harness:ref:refs/heads/release"],
        invoke_subjects=["repo:mkmeral/strandly-harness:*"],
    )
    body = str(t.to_json())
    assert "refs/heads/release" in body
    assert "repo:mkmeral/strandly-harness:*" in body
    # The default main/production subjects are gone when overridden.
    assert "environment:production" not in body


def test_oidc_outputs_role_arns():
    t = _oidc()
    outputs = t.find_outputs("*")
    assert "DeployRoleArn" in outputs
    assert "InvokeRoleArn" in outputs
    assert "OidcProviderArn" in outputs


# ---- Scheduler -------------------------------------------------------------------------

def test_load_jobs_reads_registry():
    # The static parser must find at least the daily activity review, with name + schedule.
    jobs = load_jobs()
    assert any(j["name"] == "daily-activity-review" for j in jobs)
    for j in jobs:
        assert j["name"] and j["schedule"] and isinstance(j["enabled"], bool)


def test_load_jobs_tolerates_non_literal_prompt(tmp_path):
    # Regression: the parser must NOT literal_eval the prompt/skill (only name/schedule/enabled),
    # so a prompt written as an f-string or concatenation can't break `cdk synth`.
    jobs_file = tmp_path / "jobs.py"
    jobs_file.write_text(
        "VAR = 'x'\n"
        "class ScheduledJob: pass\n"
        "JOBS = [\n"
        "    ScheduledJob(name='j1', schedule='rate(1 day)', prompt=f'hello {VAR}', enabled=True),\n"
        "    ScheduledJob(name='j2', schedule='cron(0 9 ? * MON *)', prompt='a' + VAR),\n"
        "]\n"
    )
    jobs = load_jobs(jobs_file)
    assert [j["name"] for j in jobs] == ["j1", "j2"]
    assert jobs[0]["schedule"] == "rate(1 day)" and jobs[0]["enabled"] is True
    assert jobs[1]["enabled"] is True  # defaulted, prompt never evaluated


def test_scheduler_stack_one_lambda_and_schedule_per_job(tmp_path):
    asset = tmp_path / "poller"
    asset.mkdir()
    (asset / "placeholder").write_text("x")

    app = cdk.App()
    stack = SchedulerStack(
        app,
        "Strandly-Scheduler-dev",
        naming=_naming(),
        runtime_arn=_RUNTIME_ARN,
        poller_asset=str(asset),
        env=_ENV,
    )
    t = Template.from_stack(stack)
    t.resource_count_is("AWS::Lambda::Function", 1)  # ONE generic invoker for all jobs
    n_jobs = len(load_jobs())
    t.resource_count_is("AWS::Scheduler::Schedule", n_jobs)
    # Each schedule names its job in the target input, and may invoke only the one runtime.
    t.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {"Target": Match.object_like({"Input": Match.string_like_regexp(r'.*"job".*')})},
    )
    t.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": Match.object_like(
                {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": "bedrock-agentcore:InvokeAgentRuntime",
                                    "Resource": Match.array_with([_RUNTIME_ARN]),
                                }
                            )
                        ]
                    )
                }
            )
        },
    )


def test_scheduler_all_disabled_when_flag_off(tmp_path):
    asset = tmp_path / "poller"
    asset.mkdir()
    (asset / "placeholder").write_text("x")
    app = cdk.App()
    stack = SchedulerStack(
        app,
        "Strandly-Scheduler-dev",
        naming=_naming(),
        runtime_arn=_RUNTIME_ARN,
        poller_asset=str(asset),
        all_enabled=False,
        env=_ENV,
    )
    t = Template.from_stack(stack)
    for res in t.find_resources("AWS::Scheduler::Schedule").values():
        assert res["Properties"]["State"] == "DISABLED"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---- Monitoring / Cost / Audit (issue #356) --------------------------------------------

def _asset(tmp_path):
    asset = tmp_path / "poller"
    asset.mkdir()
    (asset / "placeholder").write_text("x")
    return str(asset)


def test_monitoring_stack_alarms_lambda_topic_and_schedule(tmp_path):
    from stacks.monitoring_stack import MonitoringStack

    app = cdk.App()
    data = DataStack(app, "Strandly-Data-dev", naming=_naming(), env=_ENV)
    stack = MonitoringStack(
        app,
        "Strandly-Monitoring-dev",
        naming=_naming(),
        run_ledger_table=data.run_ledger,
        alarm_email="ops@example.com",
        poller_asset=_asset(tmp_path),
        env=_ENV,
    )
    t = Template.from_stack(stack)
    # The stuck-run detector Lambda + its schedule, an alert topic with the email subscription.
    t.resource_count_is("AWS::Lambda::Function", 1)
    t.resource_count_is("AWS::Scheduler::Schedule", 1)
    t.resource_count_is("AWS::SNS::Topic", 1)
    t.resource_count_is("AWS::SNS::Subscription", 1)
    # The full operational alarm set, each wired to the topic.
    alarms = t.find_resources("AWS::CloudWatch::Alarm")
    assert len(alarms) >= 7
    names = {a["Properties"].get("AlarmName") for a in alarms.values()}
    assert {
        "strandly-dev-failure-rate",
        "strandly-dev-ledger-write-failed",
        "strandly-dev-poll-silent",
        "strandly-dev-stuck-runs",
    } <= names
    # Alarms reference the per-env EMF namespace rollup.
    body = str(t.to_json())
    assert "Strandly-dev" in body


def test_monitoring_stuck_lambda_reads_ledger_only(tmp_path):
    from stacks.monitoring_stack import MonitoringStack

    app = cdk.App()
    data = DataStack(app, "Strandly-Data-dev", naming=_naming(), env=_ENV)
    stack = MonitoringStack(
        app,
        "Strandly-Monitoring-dev",
        naming=_naming(),
        run_ledger_table=data.run_ledger,
        poller_asset=_asset(tmp_path),
        env=_ENV,
    )
    t = Template.from_stack(stack)
    body = str(t.to_json())
    # Read-only on the ledger (Query/GetItem), never PutItem/DeleteItem (the detector never mutates).
    assert "dynamodb:Query" in body
    assert "dynamodb:PutItem" not in body
    # No email subscription when no alarm_email is given (alarms still created).
    t.resource_count_is("AWS::SNS::Subscription", 0)
    assert len(t.find_resources("AWS::CloudWatch::Alarm")) >= 7


def test_monitoring_poll_silent_alarm_breaches_on_missing_data(tmp_path):
    from stacks.monitoring_stack import MonitoringStack

    app = cdk.App()
    data = DataStack(app, "Strandly-Data-dev", naming=_naming(), env=_ENV)
    stack = MonitoringStack(
        app,
        "Strandly-Monitoring-dev",
        naming=_naming(),
        run_ledger_table=data.run_ledger,
        poller_asset=_asset(tmp_path),
        env=_ENV,
    )
    t = Template.from_stack(stack)
    # The "no successful poll" alarm must treat missing data as breaching (absence is the signal).
    t.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "AlarmName": "strandly-dev-poll-silent",
            "TreatMissingData": "breaching",
            "ComparisonOperator": "LessThanThreshold",
        },
    )


def test_cost_stack_anomaly_monitor_and_subscription():
    from stacks.cost_stack import CostStack

    app = cdk.App()
    stack = CostStack(
        app, "Strandly-Cost-dev", naming=_naming(), alarm_email="ops@example.com", env=_ENV
    )
    t = Template.from_stack(stack)
    t.resource_count_is("AWS::CE::AnomalyMonitor", 1)
    t.resource_count_is("AWS::CE::AnomalySubscription", 1)
    # CUSTOM monitor scoped to the strandly cost-allocation tag (not a fabricated token metric).
    t.has_resource_properties(
        "AWS::CE::AnomalyMonitor",
        {"MonitorType": "CUSTOM", "MonitorSpecification": Match.string_like_regexp(r'.*"app".*strandly.*')},
    )
    # The subscription emails the anomaly (DAILY, EMAIL subscriber).
    t.has_resource_properties(
        "AWS::CE::AnomalySubscription",
        {
            "Frequency": "DAILY",
            "Subscribers": Match.array_with(
                [Match.object_like({"Type": "EMAIL", "Address": "ops@example.com"})]
            ),
        },
    )
    # CE rejects hyphens in a subscription name — assert the synthesized name carries none
    # (regression: it was built from ``naming.hyphen`` which embeds them).
    sub = t.find_resources("AWS::CE::AnomalySubscription")
    sub_name = next(iter(sub.values()))["Properties"]["SubscriptionName"]
    assert "-" not in sub_name, f"subscription name must not contain hyphens: {sub_name!r}"


def test_audit_stack_lambda_schedule_topic_and_scoped_iam(tmp_path):
    from stacks.audit_stack import AuditStack

    app = cdk.App()
    secret = "arn:aws:secretsmanager:us-west-2:111111111111:secret:strandly/dev/config-AbCdEf"
    stack = AuditStack(
        app,
        "Strandly-Audit-dev",
        naming=_naming(),
        allowed_owners="mkmeral, agent-of-mkmeral",
        secret_arn=secret,
        alarm_email="ops@example.com",
        poller_asset=_asset(tmp_path),
        env=_ENV,
    )
    t = Template.from_stack(stack)
    t.resource_count_is("AWS::Lambda::Function", 1)
    t.resource_count_is("AWS::Scheduler::Schedule", 1)
    t.resource_count_is("AWS::SNS::Topic", 1)
    body = str(t.to_json())
    # The audit handler + its allow-list env, and the findings topic ARN wired in.
    t.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "strandly_harness.ops.lambdas.mention_poller.audit.lambda_handler",
            "Environment": {
                "Variables": Match.object_like(
                    {"STRANDLY_AUDIT_ALLOWED_OWNERS": "mkmeral,agent-of-mkmeral"}
                )
            },
        },
    )
    # It reads only the one secret; it needs no AWS data-plane (it talks to GitHub over HTTPS).
    assert "secretsmanager:GetSecretValue" in body
    assert "dynamodb" not in body.lower()
    assert "bedrock-agentcore:InvokeAgentRuntime" not in body


def test_audit_stack_rejects_empty_allow_list(tmp_path):
    from stacks.audit_stack import AuditStack

    with pytest.raises(ValueError, match="non-empty allow-list"):
        AuditStack(
            cdk.App(),
            "Strandly-Audit-dev",
            naming=_naming(),
            allowed_owners="   ,  ",
            poller_asset=_asset(tmp_path),
            env=_ENV,
        )
