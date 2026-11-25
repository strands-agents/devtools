"""SchedulerStack: scheduled jobs.

The schedules table, the jobs EventBridge Scheduler group and the invoker Lambda
(``strandly.ops.lambdas.scheduled.invoker``). The harness creates and deletes one schedule per job
(``strandly.ops.schedules``); each fires the invoker, which dispatches the agent runtime.
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

from .common import JOBS_RECONCILE_SCHEDULE, SINGLE_INVOKE_LAMBDA_TIMEOUT_S, Naming

_DEFAULT_ASSET = Path(__file__).resolve().parents[1] / "build" / "poller"


class SchedulerStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        naming: Naming,
        runtime_arn: str,
        secret_arn: str | None = None,
        poller_asset: str | None = None,
        all_enabled: bool = True,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        asset_path = Path(poller_asset) if poller_asset else _DEFAULT_ASSET
        if not asset_path.is_dir():
            raise FileNotFoundError(
                f"scheduled-invoker Lambda asset not found at {asset_path}. Build it first:\n"
                "  infra/scripts/build-poller-package.sh --local infra/build/poller\n"
                "(the scheduler reuses the same package as the poller) or pass -c poller_asset=<dir>."
            )

        table = dynamodb.Table(
            self,
            "SchedulesTable",
            table_name=naming.schedules_table,
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
        )
        group = scheduler.CfnScheduleGroup(self, "JobsGroup", name=naming.jobs_schedule_group)

        group_arn = f"arn:{self.partition}:scheduler:{self.region}:{self.account}:schedule-group"
        # The jobs group's schedules (and the reconcile tick) run as this role; it may invoke only
        # the invoker. Scheduler's aws:SourceArn is the schedule's group.
        jobs_role = iam.Role(
            self,
            "JobsSchedulerRole",
            role_name=naming.jobs_scheduler_role,
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={
                    "StringEquals": {
                        "aws:SourceAccount": self.account,
                        "aws:SourceArn": f"{group_arn}/{naming.jobs_schedule_group}",
                    },
                },
            ),
            description="EventBridge Scheduler fires the Strandly scheduled-job invoker.",
        )

        # Built from the fixed function name, so the invoker can name itself as the target.
        invoker_arn = (
            f"arn:{self.partition}:lambda:{self.region}:{self.account}"
            f":function:{naming.scheduler_function}"
        )
        env_vars = {
            "STRANDLY_RUNTIME_ARN": runtime_arn,
            "STRANDLY_SCHEDULES_TABLE": naming.schedules_table,
            "STRANDLY_SCHEDULES_GROUP": naming.jobs_schedule_group,
            "STRANDLY_SCHEDULES_TARGET_ARN": invoker_arn,
            "STRANDLY_SCHEDULES_ROLE_ARN": jobs_role.role_arn,
            "STRANDLY_SCHEDULES_ENABLED": "true" if all_enabled else "false",
        }
        if secret_arn:
            env_vars["STRANDLY_SECRETS_ARN"] = secret_arn

        log_group = logs.LogGroup(
            self,
            "InvokerLogGroup",
            log_group_name=f"/aws/lambda/{naming.scheduler_function}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        invoker = lambda_.Function(
            self,
            "InvokerFunction",
            function_name=naming.scheduler_function,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="strandly.ops.lambdas.scheduled.invoker.lambda_handler",
            code=lambda_.Code.from_asset(str(asset_path)),
            # One launch per invocation; it waits out a cold session's accept (never retried).
            timeout=Duration.seconds(SINGLE_INVOKE_LAMBDA_TIMEOUT_S),
            memory_size=256,
            environment=env_vars,
            log_group=log_group,
        )

        invoker.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[runtime_arn, f"{runtime_arn}/*"],
            )
        )
        if secret_arn:
            invoker.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"], resources=[secret_arn]
                )
            )

        table.grant_read_write_data(invoker)
        group_schedules = (
            f"arn:{self.partition}:scheduler:{self.region}:{self.account}"
            f":schedule/{naming.jobs_schedule_group}/*"
        )
        invoker.add_to_role_policy(
            iam.PolicyStatement(
                sid="JobSchedules",
                actions=[
                    "scheduler:CreateSchedule",
                    "scheduler:UpdateSchedule",
                    "scheduler:DeleteSchedule",
                    "scheduler:GetSchedule",
                ],
                resources=[group_schedules],
            )
        )
        # ListSchedules has no resource-level permissions.
        invoker.add_to_role_policy(
            iam.PolicyStatement(
                sid="ListJobSchedules", actions=["scheduler:ListSchedules"], resources=["*"]
            )
        )
        invoker.add_to_role_policy(
            iam.PolicyStatement(
                sid="PassJobsSchedulerRole",
                actions=["iam:PassRole"],
                resources=[jobs_role.role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}},
            )
        )
        invoker.grant_invoke(jobs_role)

        # Hourly: make the jobs group match the table (and run late a once job that missed).
        scheduler.CfnSchedule(
            self,
            "ReconcileSchedule",
            name=JOBS_RECONCILE_SCHEDULE,
            group_name=group.ref,
            state="ENABLED" if all_enabled else "DISABLED",
            schedule_expression="cron(17 * * * ? *)",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=invoker.function_arn, role_arn=jobs_role.role_arn, input='{"reconcile": true}'
            ),
        )

        CfnOutput(self, "InvokerFunctionArn", value=invoker.function_arn)
        CfnOutput(self, "SchedulesTableName", value=table.table_name)
        CfnOutput(self, "JobsScheduleGroupName", value=group.ref)
        CfnOutput(self, "JobsSchedulerRoleArn", value=jobs_role.role_arn)
