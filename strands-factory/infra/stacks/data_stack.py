"""DataStack: the run ledger and mention dedup tables, the stateful core.

They live in their own stack so dashboard and trigger stacks can be replaced without touching
operational data; both tables are retained on stack deletion.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from constructs import Construct

from .common import (
    MENTION_LOG_GSI,
    PEOPLE_COGNITO_GSI,
    PEOPLE_GITHUB_GSI,
    PEOPLE_SLACK_GSI,
    RUN_LEDGER_GSI,
    SESSION_NONCURRENT_DAYS,
    SESSION_RETENTION_DAYS,
    SESSION_RETENTION_PREFIXES,
    WORK_PRODUCT_SESSION_GSI,
    WORK_PRODUCT_STATUS_GSI,
    Naming,
)


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

        # People directory: the allow-list as rows. Not TTL-reaped (a person is a durable record,
        # not a rolling window) and PITR-protected — losing it locks everyone out, and a bad bulk
        # edit should be recoverable. Sparse GSIs: a row without ``slack_user_id`` is simply absent
        # from ``by_slack`` (DynamoDB skips items missing the index key), so single-identity rows
        # need no placeholder values.
        self.people = dynamodb.Table(
            self,
            "People",
            table_name=naming.people_table,
            partition_key=dynamodb.Attribute(name="person_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=removal,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
        )
        self.people.add_global_secondary_index(
            index_name=PEOPLE_GITHUB_GSI,
            partition_key=dynamodb.Attribute(
                name="github_login", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        self.people.add_global_secondary_index(
            index_name=PEOPLE_SLACK_GSI,
            partition_key=dynamodb.Attribute(
                name="slack_user_id", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        # The portal user bound to a person (the dashboard API's caller -> person lookup). Added to
        # the existing table in place: DynamoDB creates at most one new GSI per table update.
        self.people.add_global_secondary_index(
            index_name=PEOPLE_COGNITO_GSI,
            partition_key=dynamodb.Attribute(
                name="cognito_sub", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # Work products: one row per review/triage/first-response artifact the runtime emits (the
        # portal's Today/Review/Triage surface reads it, curate writes it). NOT TTL-reaped — a
        # curated/posted artifact is durable record, not a rolling window — and PITR-protected like
        # the run-ledger. Two partitioned newest-first GSIs: by_status (status →
        # updated_at) powers the Today inbox draft query; by_session (session_id → created_at)
        # powers per-GitHub-item history.
        self.work_products = dynamodb.Table(
            self,
            "WorkProducts",
            table_name=naming.work_product_table,
            partition_key=dynamodb.Attribute(
                name="work_product_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=removal,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
        )
        self.work_products.add_global_secondary_index(
            index_name=WORK_PRODUCT_STATUS_GSI,
            partition_key=dynamodb.Attribute(name="status", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="updated_at", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        self.work_products.add_global_secondary_index(
            index_name=WORK_PRODUCT_SESSION_GSI,
            partition_key=dynamodb.Attribute(name="session_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="created_at", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        self.session_key, self.sessions = self._session_bucket(naming, removal)

        CfnOutput(self, "RunLedgerTableName", value=self.run_ledger.table_name)
        CfnOutput(self, "DedupTableName", value=self.dedup.table_name)
        CfnOutput(self, "MentionLogTableName", value=self.mention_log.table_name)
        CfnOutput(self, "PeopleTableName", value=self.people.table_name)
        CfnOutput(self, "WorkProductTableName", value=self.work_products.table_name)
        CfnOutput(self, "SessionBucketName", value=self.sessions.bucket_name)
        CfnOutput(self, "SessionKeyArn", value=self.session_key.key_arn)

    def _session_bucket(self, naming: Naming, removal: RemovalPolicy) -> tuple[kms.Key, s3.Bucket]:
        """The session-store bucket + its customer-managed key.

        - **Encryption:** SSE-KMS with a CMK and an S3 bucket key. ``S3Storage`` passes no SSE
          arguments, so bucket default encryption carries all of it; the CMK is a second,
          key-policy-level fence (a principal needs an explicit KMS grant, not just a bucket grant)
          and puts every decrypt in CloudTrail. Conversations hold repo code, Slack DMs, and anything
          a user pastes.
        - **Versioning, short noncurrent retention:** ``snapshot_latest`` is overwritten on every
          message, so versions are a clobber-undo during rollout, not an archive — noncurrent
          versions go after :data:`SESSION_NONCURRENT_DAYS`.
        - **Retention by surface** (``SESSION_RETENTION_DAYS`` on ``session/`` and ``context/``): S3
          counts from the object's last write. ``snapshot_latest`` is rewritten on every message, so
          an active session's snapshot never expires — an idle TTL. A stash object (``context/``) is
          written once, so it expires that many days after it was written: an idle session's stash
          goes no later than its snapshot, and a session active for longer than its retention keeps
          the previews of older offloaded results but not their originals. Overlapping rules resolve
          to the SHORTEST, so ``<prefix>/`` carries the default and ``<prefix>/<surface>-…`` the
          shorter ones. (The verbatim transcript is AgentCore Memory events, not objects here.)
        - Block all public access, TLS only, bucket-owner-enforced ownership (no ACLs).
        """
        key = kms.Key(
            self,
            "SessionKey",
            alias=naming.session_key_alias,
            description=f"{naming.hyphen} session store (conversation snapshots + context stash)",
            enable_key_rotation=True,
            removal_policy=removal,
        )
        rules: list[s3.LifecycleRule] = [
            s3.LifecycleRule(
                id="noncurrent-and-markers",
                noncurrent_version_expiration=Duration.days(SESSION_NONCURRENT_DAYS),
                abort_incomplete_multipart_upload_after=Duration.days(7),
                expired_object_delete_marker=True,
            )
        ]
        for prefix in SESSION_RETENTION_PREFIXES:
            for surface, days in SESSION_RETENTION_DAYS.items():
                rules.append(
                    s3.LifecycleRule(
                        id=f"expire-{prefix}-{'default' if surface == '*' else surface}",
                        prefix=f"{prefix}/" if surface == "*" else f"{prefix}/{surface}-",
                        expiration=Duration.days(days),
                    )
                )
        bucket = s3.Bucket(
            self,
            "Sessions",
            bucket_name=naming.session_bucket(self.account),
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            removal_policy=removal,
            auto_delete_objects=removal == RemovalPolicy.DESTROY,
            lifecycle_rules=rules,
        )
        return key, bucket
