"""SlackStack: the Slack ingress, an HTTP API and Lambda that dispatch the agent runtime.

Slack posts signed events to ``POST /slack/events``; ``strandly.ops.lambdas.slack_ingress.handler``
verifies the signature, authorizes the author, deduplicates and invokes the runtime fire-and-forget.
A second Lambda forwards alarm notifications to Slack. See docs/deploy.md.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from constructs import Construct

from .common import (
    SINGLE_INVOKE_LAMBDA_TIMEOUT_S,
    SLACK_THREAD_LINK_KEY_PREFIX,
    Naming,
    dynamodb_table_arn,
)

_DEFAULT_ASSET = Path(__file__).resolve().parents[1] / "build" / "poller"

EVENTS_PATH = "/slack/events"


class SlackStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        naming: Naming,
        runtime_arn: str,
        slack_secret_arn: str,
        poller_asset: str | None = None,
        alarm_topic: bool = False,
        home_channel: bool = False,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        # Home-channel thread links live in the run ledger; nothing here touches it unless enabled.
        self._ledger_arn = (
            dynamodb_table_arn(naming.run_ledger_table, region=self.region, account=self.account)
            if home_channel
            else None
        )

        # The Data stack's dedup table, referenced by deterministic name (no cross-stack import →
        # no export → Data can be re-deployed freely) — same pattern as IngressStack/DashboardStack.
        dedup_table = dynamodb.Table.from_table_attributes(
            self,
            "DedupRef",
            table_arn=dynamodb_table_arn(
                naming.dedup_table, region=self.region, account=self.account
            ),
        )

        # The people directory (DataStack-owned, by name) — the DynamoDB-backed DM allow-list the
        # handler authorizes ``event.user`` against (a Query on the ``by_slack`` GSI, hence
        # ``grant_index_permissions``). Query-only; the runtime itself gets no grant on this table.
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

        # STRANDLY_SLACK_ALLOWED_USERS is deliberately NOT baked in here: the handler reads the
        # allow-list from the Slack secret's ``allowed_users`` key first (env is only a fallback),
        # so who may DM the agent is editable in the secret without a redeploy — same single-
        # source-of-truth rationale as IngressStack's mention allow-list.
        env_vars = {
            "STRANDLY_RUNTIME_ARN": runtime_arn,
            "STRANDLY_SLACK_SECRETS_ARN": slack_secret_arn,
            "STRANDLY_DEDUP_TABLE": dedup_table.table_name,
            "STRANDLY_METRICS_NAMESPACE": naming.metrics_namespace,
            "STRANDLY_PEOPLE_TABLE": people_table.table_name,
        }

        log_group = logs.LogGroup(
            self,
            "SlackIngressLogGroup",
            log_group_name=f"/aws/lambda/{naming.slack_function}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        fn = lambda_.Function(
            self,
            "SlackIngressFunction",
            function_name=naming.slack_function,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="strandly.ops.lambdas.slack_ingress.handler.lambda_handler",
            code=lambda_.Code.from_asset(str(asset_path)),
            # Slack expects the ack within 3 seconds; a warm launch acks well inside that. A COLD
            # session's launch (over a minute at worst) can't: the HTTP API answers Slack at
            # its 30 s integration cap while this Lambda keeps running to finish the dispatch +
            # the 👀 ack, and Slack's same-event_id redeliveries hit the dedup row. So the timeout
            # covers one worst-case InvokeAgentRuntime (never auto-retried) + margin — at 30 s the
            # Lambda was killed mid-dispatch on every cold start (no ack, no metrics).
            timeout=Duration.seconds(SINGLE_INVOKE_LAMBDA_TIMEOUT_S),
            memory_size=256,
            environment={**env_vars, "STRANDLY_SLACK_HOME_CHANNEL_ENABLED": "false"},
            log_group=log_group,
        )

        # Least privilege: RW the dedup table (incl. DeleteItem for intent rollback), invoke the
        # one runtime (+ its sessions), read ONLY the Slack secret.
        dedup_table.grant(fn, "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem")
        people_table.grant(fn, "dynamodb:Query")
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[runtime_arn, f"{runtime_arn}/*"],
            )
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"], resources=[slack_secret_arn]
            )
        )
        if self._ledger_arn:
            # Reads only the notification thread links, never a run row.
            self._grant_thread_links(fn, naming, "dynamodb:GetItem")

        # HTTP API: one POST route, no authorizer — Slack signs the body; the handler verifies
        # (and 401s) inside. No CORS needed: Slack calls server-to-server, never from a browser.
        http_api = apigw.HttpApi(self, "SlackEventsApi")
        http_api.add_routes(
            path=EVENTS_PATH,
            methods=[apigw.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("SlackIngressIntegration", fn),
        )

        # Paste this into the Slack app's Event Subscriptions → Request URL.
        CfnOutput(self, "EventsUrl", value=f"{http_api.api_endpoint}{EVENTS_PATH}")
        CfnOutput(self, "SlackIngressFunctionArn", value=fn.function_arn)

        if alarm_topic:
            self._alarm_notifier(naming, slack_secret_arn, asset_path)

    def _grant_thread_links(self, fn: lambda_.Function, naming: Naming, action: str) -> None:
        """``action`` on the run ledger's thread-link rows only, and the table name in the env."""
        fn.add_environment("STRANDLY_RUN_LEDGER_TABLE", naming.run_ledger_table)
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[action],
                resources=[self._ledger_arn],
                conditions={
                    "ForAllValues:StringLike": {
                        "dynamodb:LeadingKeys": [f"{SLACK_THREAD_LINK_KEY_PREFIX}*"]
                    }
                },
            )
        )

    def _alarm_notifier(self, naming: Naming, slack_secret_arn: str, asset_path: Path) -> None:
        """The monitoring topic → Slack home channel Lambda."""
        log_group = logs.LogGroup(
            self,
            "SlackAlarmsLogGroup",
            log_group_name=f"/aws/lambda/{naming.slack_alarms_function}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        fn = lambda_.Function(
            self,
            "SlackAlarmsFunction",
            function_name=naming.slack_alarms_function,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="strandly.ops.lambdas.slack_alarms.lambda_handler",
            code=lambda_.Code.from_asset(str(asset_path)),
            timeout=Duration.seconds(60),
            memory_size=128,
            environment={"STRANDLY_SLACK_SECRETS_ARN": slack_secret_arn,
                         "STRANDLY_SLACK_HOME_CHANNEL_ENABLED": "false"},
            retry_attempts=0,
            log_group=log_group,
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"], resources=[slack_secret_arn]
            )
        )
        if self._ledger_arn:
            # Writes only the notification thread links, never a run row.
            self._grant_thread_links(fn, naming, "dynamodb:PutItem")
        topic = sns.Topic.from_topic_arn(
            self,
            "MonitoringTopicRef",
            self.format_arn(service="sns", resource=naming.monitoring_topic),
        )
        topic.add_subscription(subs.LambdaSubscription(fn))
        CfnOutput(self, "SlackAlarmsFunctionArn", value=fn.function_arn)
