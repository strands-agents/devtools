"""IngressStack: the scheduled GitHub mention poller that dispatches the agent runtime.

An EventBridge schedule runs ``strandly.ops.lambdas.mention_poller.handler`` on a fixed interval;
it polls GitHub notifications, deduplicates and invokes the runtime fire-and-forget. The Lambda code
is the poller asset (``-c poller_asset=<dir>``, default ``infra/build/poller``).
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

from .common import POLLER_LAMBDA_TIMEOUT_S, Naming, dynamodb_table_arn

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

        # The people directory (DataStack-owned, by name): the DynamoDB-backed allow-list the
        # poller authorizes mention authors against, and the source of the "who this is" line in
        # the dispatch prompt. Query-only, on the table AND its GSIs (``grant_index_permissions`` —
        # the lookup is a Query on the ``by_github`` index). This grant exists on the ingress
        # Lambdas and nowhere the agent runs: RuntimeIamStack deliberately has none.
        people_table = dynamodb.Table.from_table_attributes(
            self,
            "PeopleRef",
            table_arn=dynamodb_table_arn(
                naming.people_table, region=self.region, account=self.account
            ),
            grant_index_permissions=True,
        )

        asset_path = Path(poller_asset) if poller_asset else _DEFAULT_ASSET
        if not asset_path.is_dir():
            raise FileNotFoundError(
                f"poller Lambda asset not found at {asset_path}. Build it first:\n"
                "  infra/scripts/build-poller-package.sh --local infra/build/poller\n"
                "or pass -c poller_asset=<dir>."
            )

        # STRANDLY_MENTION_ALLOWED_AUTHORS is deliberately NOT baked in here: Config.load() merges
        # the Secrets Manager secret UNDER the process environment (env wins), so an env var set at
        # Lambda-creation time would permanently shadow any later update to the secret — the secret
        # is the single source of truth for who may trigger the agent, editable without a redeploy.
        env_vars = {
            "STRANDLY_RUNTIME_ARN": runtime_arn,
            "STRANDLY_MENTION_HANDLE": mention_handle,
            "STRANDLY_DEDUP_TABLE": dedup_table.table_name,
            # Gates + names the EMF metric namespace. Without it the poller's metrics.emit() is a
            # no-op, so PollSuccess never lands and MonitoringStack's `poll-silent` alarm can never
            # clear (it treats missing data as breaching). Must match the namespace the alarm reads.
            "STRANDLY_METRICS_NAMESPACE": naming.metrics_namespace,
            "STRANDLY_MENTION_LOG_TABLE": mention_log_table.table_name,
            "STRANDLY_PEOPLE_TABLE": people_table.table_name,
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
            handler="strandly.ops.lambdas.mention_poller.handler.lambda_handler",
            code=lambda_.Code.from_asset(str(asset_path)),
            # Sequential launches per tick, each possibly a slow cold start (never retried).
            timeout=Duration.seconds(POLLER_LAMBDA_TIMEOUT_S),
            memory_size=256,
            environment=env_vars,
            log_group=log_group,
        )

        # Least privilege: RW the dedup table (incl. DeleteItem for intent rollback), invoke the one
        # runtime (+ its sessions), read the one secret. Logs come from the LogGroup grant below.
        dedup_table.grant(poller, "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem")
        mention_log_table.grant(poller, "dynamodb:PutItem")
        people_table.grant(poller, "dynamodb:Query")
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
