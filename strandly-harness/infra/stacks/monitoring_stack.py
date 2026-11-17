"""MonitoringStack — operational alarms + the stuck-run detector (the operational half of #356).

Cost and the GitHub write-audit are *not* here — cost is AWS-native anomaly detection (``CostStack``)
and the audit is an out-of-band safety job (``AuditStack``). This stack is the **operational** layer:
it finally *watches* the success-rate / poller / ledger telemetry the harness already produces but
nobody alarmed on.

It wires:

- **An SNS alert topic** (subscribe an email with ``-c alarm_email=…``); every alarm + the stuck-run
  detector publish here.
- **The stuck-run detector Lambda** (``ops.lambdas.stuck_runs.lambda_handler``) on an EventBridge
  schedule — the one operational check that can't be a choke-point EMF emit (its symptom is the
  *absence* of a terminal ledger write), so it scans the ledger out of band on a timer. Reuses the
  prebuilt poller asset; least-privilege (read the run-ledger, publish the topic, read the secret).
- **CloudWatch alarms over the EMF namespace** the runtime + poller emit into
  (``naming.metrics_namespace``, e.g. ``Strandly-dev``). The alarms reference the **namespace-level
  rollup** (the empty ``[]`` dimension set every emit includes), so they don't depend on a
  dimension-value contract across the infra/runtime boundary:
    * **FailureRate** > 20% over 15 min (we *compute* success_rate; now it's watched).
    * **InvocationSpike** — a runaway loop / poller storm (Invocations over a ceiling).
    * **LedgerWriteFailed** ≥ 1 — fail-open writes were silently dropping; the dashboard would just
      go blank. Now it pages.
    * **PollSilent** — *no* successful poll in 30 min (``treatMissingData=BREACHING``): the poller is
      fail-soft, so a dead trigger is invisible without this.
    * **DispatchFailed** ≥ 1 — the poller's fail-closed paths (rejected invoke / per-item error).
    * **StuckRuns** ≥ 1 — from the detector below.
    * **DynamoThrottle** — ledger-table read/write throttles (the upstream cause of a blank dashboard).

Alarms are always created (they cost nothing with no data — they sit ``INSUFFICIENT_DATA`` until the
runtime emits); only the email subscription is gated on ``alarm_email``.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from constructs import Construct

from .common import Naming

_DEFAULT_ASSET = Path(__file__).resolve().parents[1] / "build" / "poller"

# Metric names — mirror strandly_harness.ops.metrics (the runtime emits these; alarms read them). A
# separate venv that can't import the harness, so they're copied; kept trivially in lockstep.
_INVOCATIONS = "Invocations"
_COMPLETED = "Completed"
_FAILURES = "Failures"
_LEDGER_WRITE_FAILED = "LedgerWriteFailed"
_POLL_SUCCESS = "PollSuccess"
_DISPATCH_FAILED = "DispatchFailed"
_STUCK_RUNS = "StuckRuns"


class MonitoringStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        naming: Naming,
        run_ledger_table: dynamodb.ITable,
        secret_arn: str | None = None,
        alarm_email: str | None = None,
        poller_asset: str | None = None,
        schedule_expression: str = "rate(15 minutes)",
        schedule_enabled: bool = True,
        stuck_run_minutes: int = 30,
        invocation_spike_threshold: int = 100,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        ns = naming.metrics_namespace

        # ---- alert topic -----------------------------------------------------------
        topic = sns.Topic(self, "AlertTopic", topic_name=naming.monitoring_topic)
        if alarm_email:
            topic.add_subscription(subs.EmailSubscription(alarm_email))
        alarm_action = cw_actions.SnsAction(topic)

        # ---- stuck-run detector Lambda + schedule ----------------------------------
        asset_path = Path(poller_asset) if poller_asset else _DEFAULT_ASSET
        if not asset_path.is_dir():
            raise FileNotFoundError(
                f"stuck-run Lambda asset not found at {asset_path}. Build it first:\n"
                "  infra/scripts/build-poller-package.sh --local infra/build/poller\n"
                "(it reuses the same package as the poller) or pass -c poller_asset=<dir>."
            )

        env_vars = {
            "STRANDLY_RUN_LEDGER_TABLE": run_ledger_table.table_name,
            "STRANDLY_MONITORING_SNS_TOPIC_ARN": topic.topic_arn,
            "STRANDLY_STUCK_RUN_MINUTES": str(stuck_run_minutes),
            "STRANDLY_METRICS_NAMESPACE": ns,
            "STRANDLY_ENV": naming.env,
        }
        if secret_arn:
            env_vars["STRANDLY_SECRETS_ARN"] = secret_arn

        log_group = logs.LogGroup(
            self,
            "StuckRunLogGroup",
            log_group_name=f"/aws/lambda/{naming.stuck_run_function}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        stuck_fn = lambda_.Function(
            self,
            "StuckRunFunction",
            function_name=naming.stuck_run_function,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="strandly_harness.ops.lambdas.stuck_runs.lambda_handler",
            code=lambda_.Code.from_asset(str(asset_path)),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment=env_vars,
            log_group=log_group,
        )
        # Least privilege: read the ledger (Query on table + the recent GSI), publish the topic,
        # read the one secret. grant_read_data covers the index ARNs.
        run_ledger_table.grant_read_data(stuck_fn)
        topic.grant_publish(stuck_fn)
        if secret_arn:
            stuck_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"], resources=[secret_arn]
                )
            )

        scheduler_role = iam.Role(
            self,
            "StuckRunSchedulerRole",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
        )
        stuck_fn.grant_invoke(scheduler_role)
        scheduler.CfnSchedule(
            self,
            "StuckRunSchedule",
            name=naming.stuck_run_function,
            state="ENABLED" if schedule_enabled else "DISABLED",
            schedule_expression=schedule_expression,
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=stuck_fn.function_arn, role_arn=scheduler_role.role_arn
            ),
        )

        # ---- CloudWatch alarms over the EMF namespace ------------------------------
        def metric(name: str, *, stat: str = "Sum", minutes: int = 5) -> cw.Metric:
            return cw.Metric(
                namespace=ns,
                metric_name=name,
                statistic=stat,
                period=Duration.minutes(minutes),
            )

        alarms: list[cw.Alarm] = []

        # FailureRate > 20% over 15 min. MathExpression yields no data when there are no finished
        # runs (f+c=0), so NOT_BREACHING keeps a quiet period quiet.
        failure_rate = cw.MathExpression(
            expression="100 * f / (f + c)",
            using_metrics={"f": metric(_FAILURES), "c": metric(_COMPLETED)},
            period=Duration.minutes(5),
            label="FailureRatePercent",
        )
        alarms.append(
            failure_rate.create_alarm(
                self,
                "FailureRateAlarm",
                alarm_name=f"{naming.hyphen}-failure-rate",
                threshold=20,
                evaluation_periods=3,
                datapoints_to_alarm=3,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
        )

        # Invocation spike (runaway loop / poller storm).
        alarms.append(
            metric(_INVOCATIONS, minutes=15).create_alarm(
                self,
                "InvocationSpikeAlarm",
                alarm_name=f"{naming.hyphen}-invocation-spike",
                threshold=invocation_spike_threshold,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
        )

        # Ledger-write failures (fail-open writes were silently dropping telemetry).
        alarms.append(
            metric(_LEDGER_WRITE_FAILED, minutes=15).create_alarm(
                self,
                "LedgerWriteFailedAlarm",
                alarm_name=f"{naming.hyphen}-ledger-write-failed",
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
        )

        # No successful poll in 30 min → the trigger silently died. BREACHING on missing data is the
        # whole point: absence of PollSuccess is the signal.
        alarms.append(
            metric(_POLL_SUCCESS, minutes=30).create_alarm(
                self,
                "PollSilentAlarm",
                alarm_name=f"{naming.hyphen}-poll-silent",
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.BREACHING,
            )
        )

        # Poller fail-closed paths (a rejected invoke / per-item error left a mention unread).
        alarms.append(
            metric(_DISPATCH_FAILED, minutes=15).create_alarm(
                self,
                "DispatchFailedAlarm",
                alarm_name=f"{naming.hyphen}-dispatch-failed",
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
        )

        # Stuck runs (from the detector above).
        alarms.append(
            metric(_STUCK_RUNS, stat="Maximum", minutes=15).create_alarm(
                self,
                "StuckRunsAlarm",
                alarm_name=f"{naming.hyphen}-stuck-runs",
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
        )

        # DynamoDB throttling on the ledger table — the upstream cause of dropped telemetry. Sum the
        # read + write throttle-event metrics (both keyed on TableName only).
        ddb_throttle = cw.MathExpression(
            expression="r + w",
            using_metrics={
                "r": cw.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="ReadThrottleEvents",
                    dimensions_map={"TableName": run_ledger_table.table_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                ),
                "w": cw.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="WriteThrottleEvents",
                    dimensions_map={"TableName": run_ledger_table.table_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                ),
            },
            period=Duration.minutes(5),
            label="LedgerThrottleEvents",
        )
        alarms.append(
            ddb_throttle.create_alarm(
                self,
                "LedgerThrottleAlarm",
                alarm_name=f"{naming.hyphen}-ledger-throttle",
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
        )

        for alarm in alarms:
            alarm.add_alarm_action(alarm_action)
            alarm.add_ok_action(alarm_action)

        CfnOutput(self, "AlertTopicArn", value=topic.topic_arn)
        CfnOutput(self, "StuckRunFunctionArn", value=stuck_fn.function_arn)
