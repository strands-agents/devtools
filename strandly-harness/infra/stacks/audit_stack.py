"""AuditStack — the independent, out-of-band GitHub write-audit job.

This deploys ``strandly_harness.ops.lambdas.mention_poller.audit`` (the safety check, *not* a metric): a scheduled
Lambda that asks GitHub directly — via the GraphQL ``contributionsCollection`` + the REST events
backstop — what our token's account actually did across all of GitHub in the last window, and flags
any write whose repo owner is **not** in the allow-list. It runs on its own EventBridge schedule,
independently of the agent runtime, so it still catches a leaked token, a bypassed in-band guardrail,
or a prompt-injected write — none of which a metric our own code emits could ever see.

A finding is published to a dedicated SNS topic (and always logged); subscribe an email with
``-c alarm_email=…``.

**Two deployment-safety invariants (the carried-over review notes), enforced here:**

1. **No silent pass.** The allow-list is **required at synth** — an empty ``allowed_owners`` raises
   rather than deploying an audit that the runtime gate (:meth:`Config.audit_enabled`) would quietly
   treat as ``disabled``. A misconfigured audit must be un-deployable, never a check that passes
   everything.
2. **Same-account token (documented, not CFN-enforceable).** The audit token (``STRANDLY_AUDIT_TOKEN``
   in the config secret, falling back to the notifications/github token) **must belong to the same
   GitHub account** as the agent's write token — ``viewer`` audits *that token's* identity, so an
   audit token for a different account would audit the wrong identity and report a false "clean".
   CloudFormation can't verify a token's identity, so this is a deployment contract, called out
   here and in ``docs/monitoring.md``.

IAM is least-privilege and mirrors the ingress poller: its own log group, ``sns:Publish`` on the one
findings topic only, and (when a secret arn is given) ``GetSecretValue`` on that one secret. It needs
**no** AWS data-plane access beyond that — it talks to GitHub over HTTPS, not to AWS. The Lambda code
is the same prebuilt poller asset (the audit path is pure-stdlib + lazy boto3 for SNS).
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
        allowed_owners: str,
        secret_arn: str | None = None,
        lookback_hours: int = 24,
        schedule_expression: str = "rate(6 hours)",
        schedule_enabled: bool = True,
        alarm_email: str | None = None,
        poller_asset: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Invariant 1: an empty allow-list is un-deployable (no silent pass).
        owners = [o.strip() for o in allowed_owners.split(",") if o.strip()]
        if not owners:
            raise ValueError(
                "AuditStack requires a non-empty allow-list — pass -c audit_allowed_owners=owner1,owner2. "
                "Deploying without one would create an audit the runtime treats as disabled (a silent "
                "pass), which is exactly the failure mode this stack exists to prevent."
            )

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
            "STRANDLY_AUDIT_ALLOWED_OWNERS": ",".join(owners),
            "STRANDLY_AUDIT_LOOKBACK_HOURS": str(lookback_hours),
            "STRANDLY_AUDIT_SNS_TOPIC_ARN": topic.topic_arn,
        }
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
            handler="strandly_harness.ops.lambdas.mention_poller.audit.lambda_handler",
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
