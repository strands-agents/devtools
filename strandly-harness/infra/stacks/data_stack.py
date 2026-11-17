"""DataStack — the stateful core: the two DynamoDB tables the runtime + ingress depend on.

These live in their own stack on purpose. Both tables are *runtime-adjacent operational data* with a
lifecycle independent of the presentation layer (dashboard) and the trigger layer (ingress):

- **run-ledger** — written by the deployed runtime (one row per invocation), read by the dashboard.
  Previously owned by the dashboard stack, which meant tearing down the UI deleted telemetry the
  runtime was actively writing. The dashboard now references this table *by its deterministic name*
  (not a cross-stack import, which would deadlock a Data re-deploy); deleting the dashboard leaves
  the data intact.
- **dedup** — the mention poller's durable dispatch backstop. The ingress stack references it by name.
- **mention-log** — one row per ``@mention`` the poller processed (dispatched / unauthorized /
  stale, ...), written by the poller and read by the dashboard's Mentions tab. Same "recent" GSI
  shape as the run-ledger (constant ``gsi_pk`` + ISO sort key) so the dashboard lists newest-first
  with a single Query.

Both are ``PAY_PER_REQUEST`` and carry ``RETAIN`` on prod / ``DESTROY`` elsewhere — telemetry and a
dedup backstop are cheap to recreate in dev but shouldn't vanish from prod on a stack delete.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from constructs import Construct

from .common import MENTION_LOG_GSI, RUN_LEDGER_GSI, Naming


class DataStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, naming: Naming, **kwargs: object
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Prod data outlives a stack delete; dev/test is disposable.
        removal = RemovalPolicy.RETAIN if naming.env == "prod" else RemovalPolicy.DESTROY

        self.run_ledger = dynamodb.Table(
            self,
            "RunLedger",
            table_name=naming.run_ledger_table,
            partition_key=dynamodb.Attribute(name="task_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=removal,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
        )
        # "recent" GSI: every row shares gsi_pk="RUN" with an ISO started_at sort key, so the
        # dashboard lists newest-first via one Query (ScanIndexForward=False), not a table Scan.
        self.run_ledger.add_global_secondary_index(
            index_name=RUN_LEDGER_GSI,
            partition_key=dynamodb.Attribute(name="gsi_pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="started_at", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # Mention-poller dedup backstop: one row per notification thread, TTL-reaped.
        self.dedup = dynamodb.Table(
            self,
            "Dedup",
            table_name=naming.dedup_table,
            partition_key=dynamodb.Attribute(name="thread_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=removal,
        )

        # Mention log: every @mention the poller processed, for the dashboard's Mentions tab.
        # TTL-reaped like dedup (the log is a rolling window, not an archive — GitHub is durable).
        self.mention_log = dynamodb.Table(
            self,
            "MentionLog",
            table_name=naming.mention_log_table,
            partition_key=dynamodb.Attribute(name="mention_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=removal,
        )
        # Same newest-first Query shape as the run-ledger: constant gsi_pk="MENTION", ISO seen_at.
        self.mention_log.add_global_secondary_index(
            index_name=MENTION_LOG_GSI,
            partition_key=dynamodb.Attribute(name="gsi_pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="seen_at", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        CfnOutput(self, "RunLedgerTableName", value=self.run_ledger.table_name)
        CfnOutput(self, "DedupTableName", value=self.dedup.table_name)
        CfnOutput(self, "MentionLogTableName", value=self.mention_log.table_name)
