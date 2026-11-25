"""AuditStack: the out-of-band GitHub write audit.

A scheduled Lambda (``strandly.ops.lambdas.mention_poller.audit``) asks GitHub what the agent's
token actually did in the last window and publishes any write outside the owner allow-list to an
SNS topic. It runs independently of the agent runtime, so it catches a leaked token or a bypassed
in-band guardrail. Its role has no AWS data-plane access beyond its own topic and secret.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from constructs import Construct

from .common import Naming

_DEFAULT_ASSET = Path(__file__).resolve().parents[1] / "build" / "poller"


class AuditStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        naming: Naming,
        allowed_owners: str | None = None,
        secret_arn: str | None = None,
        lookback_hours: int = 24,
        schedule_expression: str = "rate(6 hours)",
        schedule_enabled: bool = True,
        alarm_email: str | None = None,
        poller_asset: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Invariant 1 (no silent pass) now lives at RUNTIME (Config.audit's fallback to
        # github.allowed_owners, which itself always defaults to STRANDS_ORG_OWNERS) rather than at
        # synth — see the module docstring for why gating synth on this arg was itself a silent-pass
        # vector. An explicit -c audit_allowed_owners=… still overrides the shared list when given.
        owners = [o.strip() for o in (allowed_owners or "").split(",") if o.strip()]

        asset_path = Path(poller_asset) if poller_asset else _DEFAULT_ASSET
        if not asset_path.is_dir():
            raise FileNotFoundError(
                f"audit Lambda asset not found at {asset_path}. Build it first:\n"
                "  infra/scripts/build-poller-package.sh --local infra/build/poller\n"
                "(the audit job reuses the same package as the poller) or pass -c poller_asset=<dir>."
            )

        # Dedicated findings topic (a violation is published here; subscribe an email if given).
        topic = sns.Topic(self, "AuditTopic", topic_name=naming.audit_topic)
        if alarm_email:
            topic.add_subscription(subs.EmailSubscription(alarm_email))

        env_vars = {
            "STRANDLY_AUDIT_LOOKBACK_HOURS": str(lookback_hours),
            "STRANDLY_AUDIT_SNS_TOPIC_ARN": topic.topic_arn,
        }
        # Deliberately NOT baked in when empty: Config.audit falls back to github.allowed_owners
        # (the write guardrail's list, read from the secret) at runtime. Baking an env var here —
        # even a same-value one — would permanently shadow later edits to the secret (Config.load()
        # merges the secret UNDER the process env), exactly the bug already fixed for the mention
        # poller's STRANDLY_MENTION_ALLOWED_AUTHORS.
        if owners:
            env_vars["STRANDLY_AUDIT_ALLOWED_OWNERS"] = ",".join(owners)
        if secret_arn:
            # The audit token (ideally its own read-only PAT) lives in the config secret.
            env_vars["STRANDLY_SECRETS_ARN"] = secret_arn

        log_group = logs.LogGroup(
            self,
            "AuditLogGroup",
            log_group_name=f"/aws/lambda/{naming.audit_function}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        audit = lambda_.Function(
            self,
            "AuditFunction",
            function_name=naming.audit_function,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="strandly.ops.lambdas.mention_poller.audit.lambda_handler",
            code=lambda_.Code.from_asset(str(asset_path)),
            timeout=Duration.seconds(120),
            memory_size=256,
            environment=env_vars,
            log_group=log_group,
        )

        topic.grant_publish(audit)
        if secret_arn:
            audit.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"], resources=[secret_arn]
                )
            )

        scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
        )
        audit.grant_invoke(scheduler_role)

        scheduler.CfnSchedule(
            self,
            "Schedule",
            name=naming.audit_function,
            state="ENABLED" if schedule_enabled else "DISABLED",
            schedule_expression=schedule_expression,
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=audit.function_arn, role_arn=scheduler_role.role_arn
            ),
        )

        CfnOutput(self, "AuditFunctionArn", value=audit.function_arn)
        CfnOutput(self, "AuditTopicArn", value=topic.topic_arn)
