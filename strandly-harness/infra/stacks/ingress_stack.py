"""IngressStack — the GitHub ``@mention`` poller: a scheduled Lambda that dispatches the runtime.

This replaces ``deploy/mention-poller.yaml`` (the hand-written CloudFormation). An EventBridge
Scheduler fires the Lambda on a fixed interval; the Lambda polls the GitHub Notifications API for
authorized ``@mentions`` and dispatches the deployed AgentCore runtime fire-and-forget
(``InvokeAgentRuntime``). The durable dedup backstop table lives in :class:`DataStack`; this stack
references it *by deterministic name* (``from_table_attributes``, not a cross-stack import that
would deadlock a Data re-deploy), so tearing down ingress doesn't drop dedup history.

IAM is least-privilege and matches the original template: logs to its own group, RW on the dedup
table only, ``InvokeAgentRuntime`` on the one runtime only, and (when a secret arn is given)
``GetSecretValue`` on that one secret.

**Lambda code.** The poller runs the full ``strandly_harness`` package (its dispatch path imports
the Strands SDK), so the deployment package is built ahead of synth by
``infra/scripts/build-poller-package.sh`` into a local asset directory (arm64 manylinux wheels). Point this
stack at it with ``-c poller_asset=<dir>`` (default ``infra/build/poller``). Building ahead of synth
avoids a Docker bundling step.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from constructs import Construct

from .common import Naming, dynamodb_table_arn

_DEFAULT_ASSET = Path(__file__).resolve().parents[1] / "build" / "poller"


class IngressStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        naming: Naming,
        runtime_arn: str,
        mention_handle: str,
        allowed_authors: str,
        skip_repo: str | None = None,
        secret_arn: str | None = None,
        schedule_expression: str = "rate(5 minutes)",
        schedule_enabled: bool = True,
        poller_asset: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Reference the Data stack's dedup table by its deterministic name (no cross-stack import →
        # no export → Data can be re-deployed freely). Item-level grants only; no GSI needed.
        dedup_table = dynamodb.Table.from_table_attributes(
            self,
            "DedupRef",
            table_arn=dynamodb_table_arn(
                naming.dedup_table, region=self.region, account=self.account
            ),
        )

        # The mention-log table (also DataStack-owned, referenced by name): the poller writes one
        # row per processed mention so the dashboard's Mentions tab can show who mentioned the
        # agent and whether they were authorized. Write-only (PutItem) — the dashboard reads it.
        mention_log_table = dynamodb.Table.from_table_attributes(
            self,
            "MentionLogRef",
            table_arn=dynamodb_table_arn(
                naming.mention_log_table, region=self.region, account=self.account
            ),
        )

        asset_path = Path(poller_asset) if poller_asset else _DEFAULT_ASSET
        if not asset_path.is_dir():
            raise FileNotFoundError(
                f"poller Lambda asset not found at {asset_path}. Build it first:\n"
                "  infra/scripts/build-poller-package.sh --local infra/build/poller\n"
                "or pass -c poller_asset=<dir>."
            )

        env_vars = {
            "STRANDLY_RUNTIME_ARN": runtime_arn,
            "STRANDLY_MENTION_HANDLE": mention_handle,
            "STRANDLY_MENTION_ALLOWED_AUTHORS": allowed_authors,
            "STRANDLY_DEDUP_TABLE": dedup_table.table_name,
            # Gates + names the EMF metric namespace. Without it the poller's metrics.emit() is a
            # no-op, so PollSuccess never lands and MonitoringStack's `poll-silent` alarm can never
            # clear (it treats missing data as breaching). Must match the namespace the alarm reads.
            "STRANDLY_METRICS_NAMESPACE": naming.metrics_namespace,
            "STRANDLY_MENTION_LOG_TABLE": mention_log_table.table_name,
        }
        if skip_repo:
            env_vars["STRANDLY_MENTION_SKIP_REPO"] = skip_repo
        if secret_arn:
            env_vars["STRANDLY_SECRETS_ARN"] = secret_arn

        # Explicit log group so retention is managed (and the role's log scope can match it).
        log_group = logs.LogGroup(
            self,
            "PollerLogGroup",
            log_group_name=f"/aws/lambda/{naming.poller_function}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        poller = lambda_.Function(
            self,
            "PollerFunction",
            function_name=naming.poller_function,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="strandly_harness.ops.lambdas.mention_poller.handler.lambda_handler",
            code=lambda_.Code.from_asset(str(asset_path)),
            timeout=Duration.seconds(120),
            memory_size=256,
            environment=env_vars,
            log_group=log_group,
        )

        # Least privilege: RW the dedup table (incl. DeleteItem for intent rollback), invoke the one
        # runtime (+ its sessions), read the one secret. Logs come from the LogGroup grant below.
        dedup_table.grant(poller, "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem")
        mention_log_table.grant(poller, "dynamodb:PutItem")
        poller.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[runtime_arn, f"{runtime_arn}/*"],
            )
        )
        if secret_arn:
            poller.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"], resources=[secret_arn]
                )
            )

        # EventBridge Scheduler — the interval trigger (replaces the GitHub-Actions cron).
        scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
        )
        poller.grant_invoke(scheduler_role)

        scheduler.CfnSchedule(
            self,
            "Schedule",
            name=naming.poller_function,
            state="ENABLED" if schedule_enabled else "DISABLED",
            schedule_expression=schedule_expression,
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=poller.function_arn, role_arn=scheduler_role.role_arn
            ),
        )

        CfnOutput(self, "PollerFunctionArn", value=poller.function_arn)
