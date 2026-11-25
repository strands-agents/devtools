"""MonitoringStack: operational alarms and the stuck-run detector.

Alarms on the deployment's metrics namespace rollup (failure rate, invocation spikes, ledger and
session-store failures, a silent poller, dispatch failures, throttles, stuck runs), plus runtime
log metric filters, all publishing to one SNS topic; nobody is paged until someone subscribes. A
scheduled Lambda (``strandly.ops.lambdas.stuck_runs``) flags runs that stopped reporting and reaps
orphaned rows. Cost is CostStack's job and the write audit AuditStack's.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
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

from .common import STUCK_RUN_MINUTES, Naming
from .runtime_common import (
    bound_runtime_log_retention,
    runtime_id_from_arn,
    runtime_log_group_name,
)

_DEFAULT_ASSET = Path(__file__).resolve().parents[1] / "build" / "poller"

# Metric names — mirror strandly.ops.metrics (the runtime emits these; alarms read them). A
# separate venv that can't import the harness, so they're copied; kept trivially in lockstep.
_INVOCATIONS = "Invocations"
_COMPLETED = "Completed"
_FAILURES = "Failures"
_CANCELLED = "Cancelled"
_DURATION_MS = "DurationMs"
_LEDGER_WRITE_FAILED = "LedgerWriteFailed"
_TOOL_ERRORS = "ToolErrors"
_POLL_SUCCESS = "PollSuccess"
_DISPATCH_FAILED = "DispatchFailed"
_STUCK_RUNS = "StuckRuns"
# Session store (memory/session_store.py) and the work-product parser (serve/agentcore_app.py).
_SESSION_WRITE_FAILED = "SessionWriteFailed"
_SESSION_READ_FAILED = "SessionReadFailed"
# Transcript events (memory/transcript.py): a message not recorded after the bounded retries.
_TRANSCRIPT_WRITE_FAILED = "TranscriptWriteFailed"
_WORK_PRODUCT_PARSE_FAILED = "WorkProductParseFailed"

#: Service metrics namespace for AgentCore Runtime. The dimension set is NOT hand-written here: the
#: alarms take it from aws-cdk-lib's own ``Runtime.metric_*`` helpers (``Operation``,
#: ``Name=<runtime name>::DEFAULT``, ``Resource=<runtime ARN>``), because a metric with a missing
#: dimension never matches and the alarm silently never fires.
AGENTCORE_NAMESPACE = "AWS/Bedrock-AgentCore"
#: Log metric filters on the AGENT runtime log group: (metric name, exact phrase, alarm threshold
#: per 15 min). The harness logs every sandbox transport failure there.
_AGENT_LOG_SIGNALS = (
    ("SandboxInitTimeouts", "Runtime initialization time exceeded", 3),
    ("SandboxBadGateway", "Received error (502) from runtime", 20),
    ("SandboxEnospc", "No space left on device", 20),
    # Every failed wake attempt the harness logs ("sandbox wake failed (…) on attempt n/3"). On
    # Runtime instances a wake is what launches/resumes the session's EC2 instance, and AgentCore
    # publishes no instance-launch metric, so this is the launch-failure signal (EC2 capacity, an
    # AMI or ENI problem, throttles). Baseline: rare.
    ("SandboxWakeFailures", "sandbox wake failed", 5),
    # The session store logs this exact token on every failed session read/write/list/delete (the
    # store fails closed, so that build or turn fails too), and the transcript recorder on every
    # event it could not write (op=transcript; the turn goes on). Baseline: zero.
    ("SessionStorageFailure", "SESSION_STORAGE_FAILURE", 3),
)
#: Excludes the OpenTelemetry log records (``otel-rt-logs`` stream) from every agent log signal: they
#: repeat each harness log line and also carry prompt text, which can quote any of the phrases.
_NOT_OTEL = '-"telemetry.sdk.name"'
#: The harness's failed sandbox-session TTL upserts (sandbox/ttl.py) per hour. A few are tolerable
#: (the next wake retries); a sustained rate means sessions are not getting a TTL, i.e. 40 GiB
#: volumes that are never deleted. Mirrors ``ops.metrics.SANDBOX_TTL_UPSERT_FAILED``.
_SANDBOX_TTL_UPSERT_FAILED = "SandboxTtlUpsertFailed"
_SANDBOX_TTL_UPSERT_FAILURES_THRESHOLD = 3
#: The harness found the sandbox TTL switched off on a runtime sandbox (1–2 of its 3 deploy values
#: set, or none): no session it creates will ever be deleted. Emitted ONCE per agent process, so any
#: datapoint is a page. Mirrors ``ops.metrics.SANDBOX_TTL_MISCONFIGURED``.
_SANDBOX_TTL_MISCONFIGURED = "SandboxTtlMisconfigured"
#: Work-product parse failures as a % of completed runs: ~9% of runs before the parser fixes. Alarm
#: above 25% sustained for an hour, so the baseline does not page.
_WORK_PRODUCT_PARSE_BASELINE_PERCENT = 9
_WORK_PRODUCT_PARSE_THRESHOLD_PERCENT = 25


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
        stuck_run_minutes: int = STUCK_RUN_MINUTES,
        invocation_spike_threshold: int = 100,
        runtime_arn: str | None = None,
        sandbox_runtime_arn: str | None = None,
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
            handler="strandly.ops.lambdas.stuck_runs.lambda_handler",
            code=lambda_.Code.from_asset(str(asset_path)),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment=env_vars,
            log_group=log_group,
        )
        # Least privilege: read the ledger (Query on table + the recent GSI), publish the topic,
        # read the one secret. grant_read_data covers the index ARNs.
        run_ledger_table.grant_read_data(stuck_fn)
        # The orphan reaper's ONLY write: a conditional UpdateItem (status must still be "running")
        # on rows older than any runtime session can live. No PutItem, no DeleteItem.
        run_ledger_table.grant(stuck_fn, "dynamodb:UpdateItem")
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

        # Tool-error burst: many failed tool calls while runs still "complete" — the signature of
        # a systemic tool-layer outage (an expired GitHub token failing every use_github call, a
        # dead sandbox) that the failure-rate alarm is blind to because the turns finish politely.
        # The threshold tolerates the odd flaky call; a burst across 15 min pages.
        alarms.append(
            metric(_TOOL_ERRORS, minutes=15).create_alarm(
                self,
                "ToolErrorBurstAlarm",
                alarm_name=f"{naming.hyphen}-tool-errors",
                threshold=5,
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
                alarm_description=(
                    f"A run-ledger row has been `running` for more than {stuck_run_minutes} "
                    "minutes (the detector's STRANDLY_STUCK_RUN_MINUTES). An agent session lives "
                    "at most 8 h, so this is an orphaned row; see docs/monitoring.md."
                ),
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

        # Session store: every failure stops its build or turn (fail closed), so these say *why*
        # runs fail. Baseline is zero for both (a missing key is not a failure). The transcript is
        # fail-open — a lost event does not fail the turn — so its failures would otherwise go
        # unnoticed in the dashboard's history; same zero baseline, same threshold.
        for key, alarm_id, threshold in (
            (_SESSION_WRITE_FAILED, "SessionWriteFailedAlarm", 3),
            (_SESSION_READ_FAILED, "SessionReadFailedAlarm", 3),
            (_TRANSCRIPT_WRITE_FAILED, "TranscriptWriteFailedAlarm", 3),
        ):
            alarms.append(
                metric(key, minutes=15).create_alarm(
                    self,
                    alarm_id,
                    alarm_name=f"{naming.hyphen}-{_kebab(key)}",
                    threshold=threshold,
                    evaluation_periods=1,
                    comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                    treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
                )
            )

        # Work-product parse failures as a share of finished runs (a count alarm would sit in ALARM
        # at the ~9% baseline). No completed runs -> no data -> NOT_BREACHING.
        parse_rate = cw.MathExpression(
            expression="100 * FILL(p, 0) / c",
            using_metrics={
                "p": metric(_WORK_PRODUCT_PARSE_FAILED, minutes=15),
                "c": metric(_COMPLETED, minutes=15),
            },
            period=Duration.minutes(15),
            label="WorkProductParseFailedPercent",
        )
        alarms.append(
            parse_rate.create_alarm(
                self,
                "WorkProductParseRateAlarm",
                alarm_name=f"{naming.hyphen}-work-product-parse-rate",
                threshold=_WORK_PRODUCT_PARSE_THRESHOLD_PERCENT,
                evaluation_periods=4,
                datapoints_to_alarm=4,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
        )

        alarms.extend(
            self._runtime_observability(
                naming,
                runtime_arn=runtime_arn,
                sandbox_runtime_arn=sandbox_runtime_arn,
            )
        )

        for alarm in alarms:
            alarm.add_alarm_action(alarm_action)
            alarm.add_ok_action(alarm_action)

        self.alarms = alarms
        self.dashboard = self._ops_dashboard(naming, alarms, with_agent_logs=bool(runtime_arn))
        CfnOutput(self, "AlertTopicArn", value=topic.topic_arn)
        CfnOutput(self, "StuckRunFunctionArn", value=stuck_fn.function_arn)

    def _ops_dashboard(
        self, naming: Naming, alarms: list[cw.Alarm], *, with_agent_logs: bool
    ) -> cw.Dashboard:
        """One CloudWatch dashboard: every alarm's state, then the agent's key metrics."""
        ns = naming.metrics_namespace

        def m(name: str, stat: str = "Sum", label: str | None = None) -> cw.Metric:
            return cw.Metric(
                namespace=ns,
                metric_name=name,
                statistic=stat,
                period=Duration.hours(1),
                label=label or name,
            )

        sandbox = [m(_SANDBOX_TTL_UPSERT_FAILED)]
        if with_agent_logs:
            sandbox += [m(key) for key, _, _ in _AGENT_LOG_SIGNALS if key.startswith("Sandbox")]
        dashboard = cw.Dashboard(
            self,
            "OpsDashboard",
            dashboard_name=f"{naming.hyphen}-ops",
            default_interval=Duration.days(7),
        )
        dashboard.add_widgets(
            cw.AlarmStatusWidget(title="Alarms", alarms=list(alarms), width=24, height=6)
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Runs",
                left=[m(_INVOCATIONS), m(_COMPLETED), m(_FAILURES), m(_CANCELLED)],
                width=12,
            ),
            cw.GraphWidget(
                title="Run duration (ms)",
                left=[m(_DURATION_MS, "p50", "p50"), m(_DURATION_MS, "p90", "p90")],
                width=12,
            ),
        )
        dashboard.add_widgets(
            cw.GraphWidget(title="Tool errors", left=[m(_TOOL_ERRORS)], width=12),
            cw.GraphWidget(title="Sandbox signals", left=sandbox, width=12),
        )
        CfnOutput(
            self,
            "OpsDashboardUrl",
            value=(
                f"https://{self.region}.console.aws.amazon.com/cloudwatch/home"
                f"?region={self.region}#dashboards/dashboard/{naming.hyphen}-ops"
            ),
        )
        return dashboard

    def _runtime_observability(
        self,
        naming: Naming,
        *,
        runtime_arn: str | None,
        sandbox_runtime_arn: str | None,
    ) -> list[cw.Alarm]:
        """Log retention + sandbox alarms for the two AgentCore runtimes (D-report blind spots).

        Retention: both runtimes' service-created log groups get :data:`RUNTIME_LOG_RETENTION`
        (they held prompts, tool output and sandbox commands forever — ~58 GB/30 d on the agent).

        Sandbox alarms — the failure classes that killed most failed runs but paged nobody:

        * **sandbox-system-errors** — ``AWS/Bedrock-AgentCore`` ``SystemErrors`` on the sandbox
          runtime (wake + command API), the service's own view of an unhealthy sandbox.
        * **sandbox-wake-latency** — p90 ``Latency`` of the sandbox wake (``InvokeAgentRuntime``)
          above 90 s: cold starts approaching the 120 s init limit.
        * **sandbox-init-timeouts** / **sandbox-bad-gateway** / **sandbox-enospc** /
          **sandbox-wake-failures** — log metric filters on the AGENT log group (where the harness
          logs the sandbox's errors): "Runtime initialization time exceeded", "Received error
          (502) from runtime", "No space left on device", "sandbox wake failed" (the instance-launch
          signal: AgentCore publishes no capacity-provider metric).
        * **sandbox-ttl-target-errors** / **sandbox-ttl-dropped** — ``AWS/Scheduler``
          ``TargetErrorCount`` / ``InvocationDroppedCount`` for the TTL schedule group: a
          ``DeleteCapacityProviderSession`` that failed, or that Scheduler gave up on (that session
          and its volume need a manual delete).
        * **sandbox-ttl-upsert-failures** — the harness's ``SandboxTtlUpsertFailed``: uses whose
          TTL schedule could not be written (the wake/command itself still succeeded).
        * **sandbox-ttl-misconfigured** — the harness's ``SandboxTtlMisconfigured`` (≥ 1, emitted
          once per agent process): the TTL is off on a runtime sandbox, so no session expires.
        * **session-storage-failure** — the session store's ``SESSION_STORAGE_FAILURE`` log token on
          the AGENT log group.

        Log-text counts include echoed transcript text, so thresholds sit above the echo noise.
        """
        alarms: list[cw.Alarm] = []
        ns = naming.metrics_namespace

        def log_alarm(
            group: logs.ILogGroup,
            retention: Construct,
            key: str,
            phrase: str,
            threshold: int,
            periods: int = 1,
        ) -> cw.Alarm:
            mf = logs.MetricFilter(
                self,
                f"{key}Filter",
                log_group=group,
                filter_pattern=logs.FilterPattern.literal(f'"{phrase}" {_NOT_OTEL}'),
                metric_namespace=ns,
                metric_name=key,
                metric_value="1",
            )
            # The group may not exist until the runtime's first invoke; LogRetention creates it.
            mf.node.add_dependency(retention)
            return mf.metric(statistic="Sum", period=Duration.minutes(15)).create_alarm(
                self,
                f"{key}Alarm",
                alarm_name=f"{naming.hyphen}-{_kebab(key)}",
                threshold=threshold,
                evaluation_periods=periods,
                datapoints_to_alarm=periods,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )

        if runtime_arn:
            agent_id = runtime_id_from_arn(runtime_arn)
            agent_retention = bound_runtime_log_retention(self, "AgentRuntimeLogRetention", agent_id)
            agent_logs = logs.LogGroup.from_log_group_name(
                self, "AgentRuntimeLogs", runtime_log_group_name(agent_id)
            )
            for key, phrase, threshold in _AGENT_LOG_SIGNALS:
                alarms.append(log_alarm(agent_logs, agent_retention, key, phrase, threshold))

        if sandbox_runtime_arn:
            sandbox_id = runtime_id_from_arn(sandbox_runtime_arn)
            # Bounded retention only: nothing alarms on the sandbox's own log group (the harness
            # logs sandbox failures on the agent's).
            bound_runtime_log_retention(self, "SandboxRuntimeLogRetention", sandbox_id)
            alarms.extend(self._sandbox_ttl_alarms(naming))

            # The service metrics' dimension set comes from aws-cdk-lib's Runtime L2
            # (metric_system_errors), so it matches what the L2 documents the service publishes.
            # Runtime names have no hyphen, so the ARN's ``<name>-<suffix>`` id splits cleanly.
            sandbox_rt = agentcore.Runtime.from_agent_runtime_attributes(
                self,
                "SandboxRuntimeRef",
                agent_runtime_arn=sandbox_runtime_arn,
                agent_runtime_id=sandbox_id,
                agent_runtime_name=sandbox_id.rsplit("-", 1)[0],
                role_arn=f"arn:aws:iam::{self.account}:role/unused-for-metrics",
            )
            l2_dimensions = dict(sandbox_rt.metric_system_errors().dimensions or {})

            def service(metric_name: str, operation: str, stat: str) -> cw.Metric:
                return cw.Metric(
                    namespace=AGENTCORE_NAMESPACE,
                    metric_name=metric_name,
                    dimensions_map={**l2_dimensions, "Operation": operation},
                    statistic=stat,
                    period=Duration.minutes(15),
                )

            system_errors = cw.MathExpression(
                expression="wake + cmd",
                using_metrics={
                    "wake": service("SystemErrors", "InvokeAgentRuntime", "Sum"),
                    "cmd": service("SystemErrors", "InvokeAgentRuntimeCommand", "Sum"),
                },
                period=Duration.minutes(15),
                label="SandboxSystemErrors",
            )
            alarms.append(
                system_errors.create_alarm(
                    self,
                    "SandboxSystemErrorsAlarm",
                    alarm_name=f"{naming.hyphen}-sandbox-system-errors",
                    threshold=5,
                    evaluation_periods=1,
                    comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                    treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
                )
            )
            alarms.append(
                service("Latency", "InvokeAgentRuntime", "p90").create_alarm(
                    self,
                    "SandboxWakeLatencyAlarm",
                    alarm_name=f"{naming.hyphen}-sandbox-wake-latency",
                    threshold=90_000,  # ms; the init limit is 120 s
                    evaluation_periods=2,
                    datapoints_to_alarm=2,
                    comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                    treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
                )
            )
        return alarms


    def _sandbox_ttl_alarms(self, naming: Naming) -> list[cw.Alarm]:
        """Alarms for the sandbox sessions' sliding TTL (SandboxStack's Scheduler group + harness).

        Every sandbox session is deleted by its own one-time EventBridge Scheduler schedule, which
        the harness moves on every use (wake, command, file op; at most hourly). A TTL that never fires, or fires and fails, is a
        silent EBS cost leak (a retained 40 GiB volume per session), hence four alarms — the
        fourth for a TTL that is switched off altogether (a deploy missing some of its ids).
        """

        def scheduler_metric(name: str) -> cw.Metric:
            return cw.Metric(
                namespace="AWS/Scheduler",
                metric_name=name,
                dimensions_map={"ScheduleGroup": naming.sandbox_ttl_schedule_group},
                statistic="Sum",
                period=Duration.hours(1),
            )

        def harness_metric(name: str) -> cw.Metric:
            return cw.Metric(
                namespace=naming.metrics_namespace,
                metric_name=name,
                statistic="Sum",
                period=Duration.hours(1),
            )

        def alarm(source: cw.Metric, construct_id: str, suffix: str, threshold: int) -> cw.Alarm:
            return source.create_alarm(
                self,
                construct_id,
                alarm_name=f"{naming.hyphen}-{suffix}",
                threshold=threshold,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )

        return [
            alarm(
                scheduler_metric("TargetErrorCount"),
                "SandboxTtlTargetErrorsAlarm",
                "sandbox-ttl-target-errors",
                1,
            ),
            alarm(
                scheduler_metric("InvocationDroppedCount"),
                "SandboxTtlDroppedAlarm",
                "sandbox-ttl-dropped",
                1,
            ),
            alarm(
                harness_metric(_SANDBOX_TTL_UPSERT_FAILED),
                "SandboxTtlUpsertFailuresAlarm",
                "sandbox-ttl-upsert-failures",
                _SANDBOX_TTL_UPSERT_FAILURES_THRESHOLD,
            ),
            alarm(
                harness_metric(_SANDBOX_TTL_MISCONFIGURED),
                "SandboxTtlMisconfiguredAlarm",
                "sandbox-ttl-misconfigured",
                1,
            ),
        ]


def _kebab(name: str) -> str:
    """``SandboxInitTimeouts`` -> ``sandbox-init-timeouts``."""
    return "".join(f"-{c.lower()}" if c.isupper() else c for c in name).lstrip("-")
