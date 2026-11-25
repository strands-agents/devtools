"""Naming and fixed values shared by every stack.

Physical names derive from ``{name}-{env}`` (or ``{name}_{env}`` where a service rejects hyphens),
so environments never collide. Constants marked as mirrors copy values from the harness (this
package must import without the Strands SDK); ``tests/test_infra_constants_sync.py`` keeps them equal.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aws_cdk import aws_iam as iam

# --- fixed provisioning choices (mirror strandly.core.constants) ---
MEMORY_EVENT_EXPIRY_DAYS = 90
MEMORY_FACTS_STRATEGY = "facts"
MEMORY_FACTS_NAMESPACE_TEMPLATE = "/facts/{actorId}/"
MEMORY_PREFERENCES_STRATEGY = "preferences"
MEMORY_PREFERENCES_NAMESPACE_TEMPLATE = "/preferences/{actorId}/"
KB_EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
KB_VECTOR_DIMENSION = 1024
KB_VECTOR_DISTANCE_METRIC = "cosine"

# The "recent" GSI on the run-ledger table. Mirrors strandly.core.constants.RUN_LEDGER_GSI_NAME
# (the canonical source) and dashboard/api/handler.py's GSI_NAME. The tests/test_infra_constants_sync
# guard asserts all three agree — keep them in lockstep.
RUN_LEDGER_GSI = "recent"

# Run-ledger partition-key prefix of the per-session title rows. Mirrors
# strandly.core.session_ids.SESSION_TITLE_KEY_PREFIX (guarded by tests/test_infra_constants_sync);
# the dashboard's ledger PutItem grant is limited to keys with this prefix.
SESSION_TITLE_KEY_PREFIX = "session-title#"

# Run-ledger partition-key prefix of the Slack home-channel thread links. Mirrors
# strandly.ops.slack.THREAD_LINK_KEY_PREFIX (guarded by tests/test_infra_constants_sync); the Slack
# ingress GetItem and alarm notifier PutItem grants are limited to keys with this prefix.
SLACK_THREAD_LINK_KEY_PREFIX = "slack-thread#"

# The "recent" GSI on the mention-log table + its constant partition value. Mirror
# strandly.core.constants.MENTION_LOG_GSI_NAME / MENTION_LOG_GSI_PK_VALUE (canonical) and
# dashboard/api/handler.py's MENTION_LOG_GSI_NAME / MENTION_LOG_GSI_PK_VALUE — the sync test guards them.
MENTION_LOG_GSI = "recent"

# The two GSIs on the work-product table (portal review/triage artifacts). Mirror
# strandly.core.constants.WORK_PRODUCT_STATUS_GSI_NAME / WORK_PRODUCT_SESSION_GSI_NAME
# (canonical) and dashboard/api/handler.py's copy of the session GSI — the sync test guards them.
# ``by_status`` (pk=status, sort=updated_at) has no dashboard reader; ``by_session``
# (pk=session_id, sort=created_at) powers the portal's per-GitHub-item card.
WORK_PRODUCT_STATUS_GSI = "by_status"
WORK_PRODUCT_SESSION_GSI = "by_session"

# The SPARSE GSIs on the people-directory table (ingress allow-list + cross-surface identity).
# Mirror strandly.core.constants.PEOPLE_GITHUB_GSI_NAME / PEOPLE_SLACK_GSI_NAME /
# PEOPLE_COGNITO_GSI_NAME (canonical; read by ops/people.py) — the sync test guards them.
# ``by_github`` is keyed on ``github_login``, ``by_slack`` on ``slack_user_id``, ``by_cognito`` on
# ``cognito_sub`` (the portal user bound to the person); a row appears only in the indexes whose key
# it carries. DynamoDB adds at most one GSI per table update: add a new index in its own deploy.
PEOPLE_GITHUB_GSI = "by_github"
PEOPLE_SLACK_GSI = "by_slack"
PEOPLE_COGNITO_GSI = "by_cognito"

# --- sandbox runtime: mirrored (the SandboxStack configures the real resource) ---
# Copies of strandly.core.constants (this package imports without the harness), guarded by
# tests/test_infra_constants_sync.py. Drift is not cosmetic: the mount path is also the cwd of every
# sandbox command, so a mismatch fails every shell command with "No such file or directory".
SANDBOX_MOUNT_PATH = "/mnt/workspace"
SANDBOX_IDLE_TIMEOUT_SECONDS = 3_600
# 14 days: the Runtime-instances session maximum (CloudFormation allows up to 1,209,600 s). A
# session that reaches it is STOPPED with its EBS volume kept; only the sliding TTL deletes it.
SANDBOX_MAX_LIFETIME_SECONDS = 1_209_600

#: Read timeout of the harness's InvokeAgentRuntime launch (mirrors
#: ``strandly.ops.runtime_client.INVOKE_READ_TIMEOUT_S``; tests/test_infra_constants_sync).
#: A launch is never auto-retried (a retry = a second run) and a cold session can take over a minute
#: to accept its first invoke (78 s observed on the microVM), so every Lambda that WAITS on a launch
#: (mention poller, Slack ingress, scheduled invoker) must outlive one worst-case call plus its own
#: work.
INVOKE_AGENT_READ_TIMEOUT_S = 300
#: Headroom over one worst-case launch for a waiting Lambda's other work (secret fetch, DynamoDB,
#: the GitHub/Slack ack calls).
INVOKE_CALLER_MARGIN_S = 60
#: Timeout of a Lambda that dispatches at most ONE launch per invocation (Slack ingress, scheduled
#: invoker): one worst-case launch + margin.
SINGLE_INVOKE_LAMBDA_TIMEOUT_S = INVOKE_AGENT_READ_TIMEOUT_S + INVOKE_CALLER_MARGIN_S
#: The mention poller dispatches every new mention of a tick sequentially, so it gets the Lambda
#: maximum (900 s — room for two worst-case cold launches plus the fetch). A tick it can't finish
#: is safe: each dispatched thread's dedup intent is already written, and the unread rest are
#: picked up by the next tick.
POLLER_LAMBDA_TIMEOUT_S = 900

# --- sandbox capacity provider (not mirrored: CDK-only) ---
# Bump when anything in the capacity provider's compute configuration changes (instance types,
# volumes, root volume, instance lifecycle, subnets): it is create-only and the provider is named, so
# it cannot be replaced in place. The bump renames the provider AND the runtime (``_g<n>``);
# CloudFormation creates the new pair, the deploy points the agent at it, and the old pair is
# deleted — with every old session's volume (git is the source of truth; announce it).
SANDBOX_INSTANCES_GENERATION = 1

# --- the AGENT runtime on the AgentCore microVM (agent_runtime_stack.py) ---
#: The microVM platform version (``V1`` or ``V2``). The one value to change to move between them:
#: it updates the runtime in place.
AGENT_PLATFORM_VERSION = "V1"
# An idle session is stopped after this long; a resume is a snapshot load on a fresh microVM. Short
# on purpose: the 8 h maximum below counts from the session's start, so a session kept warm is a
# session closer to it. A RUNNING turn is never idle (the app reports HEALTHY_BUSY).
AGENT_IDLE_TIMEOUT_SECONDS = 900
# The longest an agent session (and so one run) may live: 8 h, the microVM maximum. Mirrored in
# strandly.core.constants (tests/test_infra_constants_sync.py): the stuck-run reaper closes a
# `running` ledger row once it is older than this plus a margin.
AGENT_MAX_LIFETIME_SECONDS = 28_800

# --- the stuck-run alarm (CDK-only; MonitoringStack + app.py read it) ---
#: A run-ledger row ``running`` longer than this is "stuck": the detector Lambda gets it as
#: ``STRANDLY_STUCK_RUN_MINUTES`` and emits the ``StuckRuns`` gauge the ``<name>-<env>-stuck-runs``
#: alarm fires on (≥ 1). The session maximum: no live run can be older, so the alarm means an
#: orphaned row and never a long run; the detector's reaper closes it an hour later.
#: Override per deploy with ``-c stuck_run_minutes=…``.
STUCK_RUN_MINUTES = AGENT_MAX_LIFETIME_SECONDS // 60

#: The prefix every capacity provider's EC2 instance role must carry: the AWS-managed operator policy
#: (``BedrockAgentCoreRuntimeInstancesOperatorRolePolicy``) only lets AgentCore ``iam:PassRole``
#: roles named ``AmazonBedrockAgentCoreCapacityProviderDefaultInstanceRole*``. See
#: :meth:`Naming.instance_role`.
INSTANCE_ROLE_PREFIX = "AmazonBedrockAgentCoreCapacityProviderDefaultInstanceRole"

# --- NOT mirrored: the Code Interpreter resource ---
# The harness has no Code Interpreter backend, so this has no counterpart in strandly.core.constants
# (the sync test asserts that). The resource stays because deleting a stateful resource is its own
# verified change.
CODE_INTERPRETER_NETWORK_MODE = "PUBLIC"

# --- ABAC tag boundary for the sandbox's CI execution role (e2e testing) ---
# The agent's sandbox role can only touch resources carrying MANAGED_BY_TAG_KEY == AGENT_TAG_VALUE,
# and may only create resources if it tags them so. Prod backends are tagged INFRA_TAG_VALUE (a
# DIFFERENT value), so the agent's grants can never reach them. The e2e-test skill must tag every
# resource it creates with {AGENT}, and name S3 buckets with the MANAGED_NAME_PREFIX.
MANAGED_BY_TAG_KEY = "ManagedBy"
AGENT_TAG_VALUE = "strandly"          # agent-created, ephemeral test resources
INFRA_TAG_VALUE = "strandly-infra"    # provisioner-created prod backends (off-limits to the agent)
MANAGED_NAME_PREFIX = "strandly-managed"  # S3 bucket name prefix the agent must use (name-scoped)

# Session store (DataStack sessions bucket). Top-level key prefixes the runtime writes — mirrors
# strandly.core.constants SESSION_STORE_*_PREFIX (guarded by tests/test_infra_constants_sync).
SESSION_STORE_PREFIXES = ("session", "context")
# Idle expiration of a session's SNAPSHOT (``session/<surface>-…``, rewritten on every message, so
# S3's age-since-last-write is the session's idle time), in days, by session-id surface; "*" is the
# default (GitHub ``gh-…`` threads get revisited weeks later). Surfaces are the id builders' prefixes
# (Slack ``slack-``, portal ``portal-``, MCP ``mcp-``, scheduler ``sched-<job>-<date>``) — tied to the
# builders by tests/test_infra_constants_sync. Slack DMs keep 30 days.
# Every value outlives AGENT_MAX_LIFETIME_SECONDS: a stash written at a run's start must still exist
# at its end.
SESSION_RETENTION_DAYS: dict[str, int] = {"*": 90, "slack": 30, "portal": 30, "mcp": 30, "sched": 15}
# The prefixes that carry those rules: the snapshot (rewritten on every message, so its age is the
# session's idle time) and the context-manager stash (one object per offloaded tool result, keyed by
# session id the same way). A stash object expires by its own age, so a session active for longer
# than its retention loses the originals of results offloaded before that window: the previews in
# its history stay, and retrieve_context reports the original as gone.
SESSION_RETENTION_PREFIXES = ("session", "context")
# Noncurrent versions of overwritten snapshots: a short clobber-undo window, not an archive.
SESSION_NONCURRENT_DAYS = 3

# The reconcile tick's schedule in the jobs group. Not a valid job id, so no job can take the name;
# mirrors strandly.ops.schedules.timer.RECONCILE_SCHEDULE (tests/test_infra_constants_sync),
# which the harness never lists, updates or deletes.
JOBS_RECONCILE_SCHEDULE = "_reconcile"


# The runtime's AgentCore Memory data-plane actions (memory/transcript.py writes events,
# memory/long_term.py retrieves records) — exactly these, on the one Memory.
MEMORY_RUNTIME_ACTIONS = (
    "bedrock-agentcore:CreateEvent",
    "bedrock-agentcore:RetrieveMemoryRecords",
)
# What a transcript reader (the dashboard, a poll) may do on it: read events, nothing else.
MEMORY_READER_ACTIONS = ("bedrock-agentcore:ListEvents", "bedrock-agentcore:GetEvent")


@dataclass(frozen=True)
class Naming:
    """Derives every physical resource name from a ``name`` prefix + ``env`` suffix.

    Two styles, because AWS services disagree on the allowed charset:
    - ``hyphen`` (``strandly-dev``) for S3 vector buckets, DynamoDB tables, Lambdas — no underscores.
    - ``under`` (``strandly_dev``) for AgentCore Memory / Code Interpreter — name regex is
      ``^[a-zA-Z][a-zA-Z0-9_]{0,47}$`` (underscores only, hyphens rejected).

    ``env`` never contains a hyphen (``name`` may) and no reserved word follows a hyphen
    (``validate_deployment_coordinates``), so both stems, and every name, tag, prefix and
    role scope built from them, identify exactly one accepted ``(name, env)`` pair.
    """

    name: str = "strandly"
    env: str = "dev"
    legacy_scope: bool = False

    def __post_init__(self) -> None:
        from strandly.profiles import validate_deployment_coordinates
        validate_deployment_coordinates(self.name, self.env)

    @property
    def agent_tag(self) -> str:
        return AGENT_TAG_VALUE if self.legacy_scope else self.hyphen

    @property
    def infra_tag(self) -> str:
        return INFRA_TAG_VALUE if self.legacy_scope else f"{self.hyphen}-infra"

    @property
    def managed_prefix(self) -> str:
        return MANAGED_NAME_PREFIX if self.legacy_scope else f"{self.hyphen}-managed"

    @property
    def hyphen(self) -> str:
        return f"{self.name}-{self.env}"

    @property
    def under(self) -> str:
        return f"{self.name}_{self.env}".replace("-", "_")

    # ---- per-resource names ----
    @property
    def memory(self) -> str:
        return f"{self.under}_memory"

    @property
    def code_interpreter_v2(self) -> str:
        """The Code Interpreter's physical name.

        Named "_v2" because a same-config-swap replacement once stood this up alongside an original
        "_ci" resource (CloudFormation can't mutate execution_role_arn in place — a role change
        forces replace-with-same-name, which AWS rejects while the old resource is live). The
        original is gone now; kept the "_v2" name rather than renaming, since renaming hits the
        exact same replace-with-same-name problem.
        """
        return f"{self.under}_ci_v2"

    @property
    def sandbox_runtime(self) -> str:
        """The SANDBOX AgentCore Runtime's name, e.g. ``strandly_dev_sandbox_ec2``.

        Underscore style: the runtime name regex is ``^[a-zA-Z][a-zA-Z0-9_]{0,47}$`` (hyphens
        rejected). Env-suffixed so a ``dev`` and a ``prod`` deployment can never share one sandbox.
        ``_ec2`` because the sandbox runs on AgentCore Runtime instances (an EC2 capacity provider);
        the new name — the construct's logical id is unchanged — is what makes CloudFormation
        *replace* the earlier ``strandly_<env>_sandbox`` runtime (create the new one, then delete
        the old) instead of trying an in-place update a runtime name cannot take.
        """
        return self.sandbox_ec2(SANDBOX_INSTANCES_GENERATION)

    @property
    def sandbox_capacity_provider(self) -> str:
        """The sandbox's EC2 capacity provider (name regex ``^[a-zA-Z][a-zA-Z0-9_]{0,47}$``)."""
        return self.sandbox_ec2(SANDBOX_INSTANCES_GENERATION)

    def sandbox_ec2(self, generation: int = 1) -> str:
        """``<name>_<env>_sandbox_ec2``, plus ``_g<n>`` for ``generation`` > 1 (see
        :data:`SANDBOX_INSTANCES_GENERATION`)."""
        return f"{self.under}_sandbox_ec2" + (f"_g{generation}" if generation > 1 else "")

    @property
    def sandbox_ttl_schedule_group(self) -> str:
        """The EventBridge Scheduler group holding one sliding-TTL schedule per sandbox session
        (the harness's ``sandbox/ttl.py`` creates them; SandboxStack owns the group)."""
        return f"{self.hyphen}-sandbox-ttl"

    @property
    def sandbox_ttl_scheduler_role(self) -> str:
        """The role EventBridge Scheduler assumes to delete an expired sandbox session — a fixed
        name, so RuntimeIam can scope the agent's ``iam:PassRole`` to it without a stack import."""
        return f"{self.hyphen}-sandbox-ttl-scheduler"

    @property
    def schedules_table(self) -> str:
        """Scheduled jobs + their ``rev#n`` revisions (harness ``ops/schedules``)."""
        return f"{self.hyphen}-schedules"

    @property
    def jobs_schedule_group(self) -> str:
        """The EventBridge Scheduler group holding one schedule per scheduled job."""
        return f"{self.hyphen}-jobs"

    @property
    def jobs_scheduler_role(self) -> str:
        """The role the jobs' schedules run as (invokes the scheduled invoker only). Fixed name,
        so callers that create schedules can scope ``iam:PassRole`` to it without a stack import."""
        return f"{self.hyphen}-jobs-scheduler"

    @property
    def vector_bucket(self) -> str:
        return self.hyphen

    @property
    def vector_index(self) -> str:
        return f"{self.hyphen}-index"

    @property
    def kb(self) -> str:
        return f"{self.under}_kb"

    @property
    def kb_role(self) -> str:
        return f"{self.under}_kb_role"

    @property
    def data_source(self) -> str:
        return f"{self.under}_memories"

    @property
    def secret(self) -> str:
        return f"{self.name}/{self.env}/config"

    @property
    def run_ledger_table(self) -> str:
        return f"{self.hyphen}-runledger"

    @property
    def dedup_table(self) -> str:
        return f"{self.hyphen}-dedup"

    @property
    def mention_log_table(self) -> str:
        return f"{self.hyphen}-mentionlog"

    @property
    def work_product_table(self) -> str:
        return f"{self.hyphen}-workproducts"

    @property
    def people_table(self) -> str:
        """The people directory (ops/people.py): who may trigger the agent, and who they are.

        Read by the ingress Lambdas (the dashboard API among them) only. Deliberately NOT folded into the config secret and NOT
        granted to the runtime exec role — the allow-list stays outside the agent's reach."""
        return f"{self.hyphen}-people"

    def session_bucket(self, account: str) -> str:
        """The session-store bucket: conversation snapshots, the context-manager stash, and the
        verbatim transcript log (``memory/session_store.py``). Account-suffixed because S3 bucket
        names are global; one per env, never shared — two envs on one bucket would overwrite each
        other's sessions (the store has no conditional writes)."""
        return f"{self.hyphen}-sessions-{account}"

    @property
    def session_key_alias(self) -> str:
        """KMS alias of the session bucket's customer-managed key (default bucket encryption)."""
        return f"alias/{self.hyphen}-sessions"

    @property
    def poller_function(self) -> str:
        return f"{self.hyphen}-mention-poller"

    @property
    def people_admin_function(self) -> str:
        """The people-directory editor (DashboardStack): list/save/delete rows of ``people_table``.

        A fixed name for an optional deployment extension's directory editor."""
        return f"{self.hyphen}-people-admin"

    @property
    def slack_function(self) -> str:
        return f"{self.hyphen}-slack-ingress"

    @property
    def slack_alarms_function(self) -> str:
        return f"{self.hyphen}-slack-alarms"

    @property
    def slack_secret(self) -> str:
        """The Slack credentials secret (bot token + signing secret) — separate from ``secret``
        so Slack rotation is independent and the ingress Lambda's GetSecretValue grant is scoped
        to only this secret, never the one holding GitHub tokens."""
        return f"{self.name}/{self.env}/slack"

    @property
    def gha_deploy_role(self) -> str:
        """The GitHub Actions OIDC *deploy* role — privileged, locked to the repo's main branch."""
        return f"{self.hyphen}-gha-deploy"

    @property
    def gha_invoke_role(self) -> str:
        """The GitHub Actions OIDC *invoke* role — InvokeAgentRuntime on the agent runtime only
        (fire-and-forget), separate from deploy so a compromised invoke workflow can't redeploy the
        agent."""
        return f"{self.hyphen}-gha-invoke"

    @property
    def scheduler_function(self) -> str:
        return f"{self.hyphen}-scheduled-invoker"

    @property
    def audit_function(self) -> str:
        return f"{self.hyphen}-write-audit"

    @property
    def stuck_run_function(self) -> str:
        return f"{self.hyphen}-stuck-runs"

    @property
    def audit_topic(self) -> str:
        return f"{self.hyphen}-audit"

    @property
    def monitoring_topic(self) -> str:
        return f"{self.hyphen}-monitoring"

    @property
    def cost_topic(self) -> str:
        return f"{self.hyphen}-cost-anomaly"

    @property
    def metrics_namespace(self) -> str:
        """The CloudWatch-EMF metric namespace — per-env (e.g. ``Strandly-dev``), so the empty-set
        metric rollup the alarms read is already scoped to this environment. Wired onto each Lambda
        (and folded into the config secret for the deployed runtime) as ``STRANDLY_METRICS_NAMESPACE``."""
        return f"{self.name.capitalize()}-{self.env}"

    @property
    def agent_runtime(self) -> str:
        """The AGENT runtime's name, e.g. ``strandly_prod_agent``.

        Distinct from ``strandly_prod``, an existing runtime of another kind: runtime names are
        create-only.
        """
        return f"{self.under}_agent"

    @property
    def agent_runtime_exec_role(self) -> str:
        """The agent runtime's execution role — a fixed name, so RuntimeIam / WebSearch attach to
        it by ``-c exec_role_name``."""
        return f"{self.hyphen}-agent-runtime-exec"

    def agent_runtime_name(self, generation: int = 1) -> str:
        """The AGENT runtime's name for a capacity-provider generation: :attr:`agent_runtime` for
        generation 1, plus ``_g<n>`` for ``n`` > 1 (``strandly_prod_agent_g2``).

        The generation is in the RUNTIME name too (as the sandbox's is), so a bump of
        ``AGENT_INSTANCES_GENERATION`` makes CloudFormation create a NEW runtime on the new provider
        and then delete the old pair — runtime first, provider second, which is the order AWS
        requires (a provider still referenced by a runtime cannot be deleted). Re-pointing the same
        runtime at a new provider instead would leave the old provider behind. Consumers read the
        ARN from the stack's ``AgentRuntimeArn`` output, never from this name.
        """
        return self.agent_runtime + (f"_g{generation}" if generation > 1 else "")

    def agent_traces_source(self, runtime_name: str) -> str:
        """The CloudWatch Logs delivery source of a runtime's AgentCore TRACES,
        ``<runtime name>-traces`` (pattern ``[\\w-]``, at most 60 characters)."""
        return f"{runtime_name}-traces"

    @property
    def agent_traces_destination(self) -> str:
        """The deployment's X-Ray delivery destination for the agent runtimes' TRACES."""
        return f"{self.under}_agent-traces-xray"

    def agent_capacity_provider(self, generation: int) -> str:
        """The agent's EC2 capacity provider, ``<name>_<env>_agent_g<generation>``.

        Its compute configuration (and name) are create-only, so a change is always a NEW provider
        under a new name: bump ``AGENT_INSTANCES_GENERATION`` (agent_runtime_stack.py), which also
        renames the runtime (:meth:`agent_runtime_name`). Pattern ``^[a-zA-Z][a-zA-Z0-9_]{0,47}$``.
        """
        return f"{self.under}_agent_g{generation}"

    def instance_role(self, component: str) -> str:
        """A capacity provider's EC2 instance role (system logs only), unique per deployment.

        It MUST start with :data:`INSTANCE_ROLE_PREFIX` (57 characters): the AWS-managed
        ``BedrockAgentCoreRuntimeInstancesOperatorRolePolicy`` scopes its ``iam:PassRole`` to
        ``role/AmazonBedrockAgentCoreCapacityProviderDefaultInstanceRole*``. That leaves 7 of IAM's
        64 characters, so instead of a (silently truncated) ``-<component>-<env>``, the suffix is a
        deterministic hash: ``-`` + the first 6 hex digits of ``sha256("<name>-<env>-<component>")``
        — the same deployment always gets the same name, and different envs/components (or a sibling
        app in the same account) do not collide.
        """
        digest = hashlib.sha256(f"{self.name}-{self.env}-{component}".encode()).hexdigest()[:6]
        return f"{INSTANCE_ROLE_PREFIX}-{digest}"

    def stack(self, kind: str) -> str:
        """Stack id, e.g. ``Strandly-Backend-dev`` (Capitalized name, capitalized kind)."""
        return f"{self.name.capitalize()}-{kind}-{self.env}"


def dynamodb_table_arn(table_name: str, *, region: str, account: str) -> str:
    """The ARN of a DynamoDB table from its (deterministic) name.

    Lets a consumer stack reconstruct a Data-stack table reference by its known name instead of
    importing a live ``ITable`` across stacks. That import would emit an ``Fn::ImportValue`` and a
    matching CloudFormation export — and an export can't be modified while another stack imports it,
    which deadlocks a re-deploy of the producing (Data) stack. The table names are fully
    deterministic (``Naming``), so deriving the ARN here is exact and needs no export.
    """
    return f"arn:aws:dynamodb:{region}:{account}:table/{table_name}"


def session_store_statements(
    bucket: str, naming: Naming, region: str, account: str, *, write: bool
) -> list[iam.PolicyStatement]:
    """IAM for the session store (DataStack's sessions bucket + its CMK), by name.

    Objects: Get (+ Put/Delete when ``write``) on exactly the three prefixes the store uses.
    Listing: ``s3:ListBucket`` on the bucket (the stash cleanup and the transcript reader list
    prefixes, and it is what turns a missing key into a 404 rather than a 403). Key: decrypt (+ generate data keys when ``write``) on the
    session CMK only — matched by its alias (no cross-stack import of the key ARN) and only via S3.
    Shared by the runtime (``write=True``) and the dashboard read Lambda (``write=False``); the
    sandbox gets neither.
    """
    from aws_cdk import aws_iam as iam

    object_actions = ["s3:GetObject"] + (["s3:PutObject", "s3:DeleteObject"] if write else [])
    kms_actions = ["kms:Decrypt"] + (["kms:GenerateDataKey"] if write else [])
    return [
        iam.PolicyStatement(
            sid="SessionStoreObjects",
            actions=object_actions,
            resources=[f"arn:aws:s3:::{bucket}/{prefix}/*" for prefix in SESSION_STORE_PREFIXES],
        ),
        # UNconditioned on purpose: a GetObject of a missing key returns 404 (NoSuchKey — what the
        # store reads as "no snapshot yet") only to a caller holding s3:ListBucket on the bucket,
        # and an s3:prefix condition doesn't apply to that implicit check (the key is absent from
        # a GetObject). Without it every brand-new session's first read would be a 403, which the
        # store treats as a failed read, failing the build. The bucket is dedicated, so listing exposes
        # only session key names.
        iam.PolicyStatement(
            sid="SessionStoreList",
            actions=["s3:ListBucket"],
            resources=[f"arn:aws:s3:::{bucket}"],
        ),
        iam.PolicyStatement(
            sid="SessionStoreKey",
            actions=kms_actions,
            resources=[f"arn:aws:kms:{region}:{account}:key/*"],
            conditions={
                "ForAnyValue:StringEquals": {"kms:ResourceAliases": naming.session_key_alias},
                "StringEquals": {"kms:ViaService": f"s3.{region}.amazonaws.com"},
            },
        ),
    ]


def memory_arn(region: str, account: str, naming: Naming, memory_id: str | None = None) -> str:
    """The ARN of the deployment's one AgentCore Memory.

    Exact when the id is known (``-c memory_id``); otherwise the name prefix, which only that Memory
    has: an id is ``<name>-<10 characters>``, and the name ``<name>_<env>_memory`` is fixed.
    """
    resource = memory_id or f"{naming.memory}-*"
    return f"arn:aws:bedrock-agentcore:{region}:{account}:memory/{resource}"


def sandbox_ttl_statements(naming: Naming, region: str, account: str) -> list[iam.PolicyStatement]:
    """The AGENT's grant for the sandbox's sliding session TTL (harness ``sandbox/ttl.py``).

    Create/update one-time schedules **in the TTL group only**, and pass **only** the TTL scheduler
    role, **only** to Scheduler. Nothing else: the agent cannot delete a session itself (only the
    scheduler role can, when a schedule fires), cannot touch another group's schedules, and cannot
    hand any other role to Scheduler. Applied by RuntimeIamStack alongside the sandbox invoke grant,
    so it reaches whichever agent runtime role is live (toolkit or CDK).
    """
    from aws_cdk import aws_iam as iam

    group = naming.sandbox_ttl_schedule_group
    return [
        iam.PolicyStatement(
            sid="SandboxSessionTtlSchedules",
            actions=["scheduler:CreateSchedule", "scheduler:UpdateSchedule"],
            resources=[f"arn:aws:scheduler:{region}:{account}:schedule/{group}/*"],
        ),
        iam.PolicyStatement(
            sid="SandboxSessionTtlPassRole",
            actions=["iam:PassRole"],
            resources=[f"arn:aws:iam::{account}:role/{naming.sandbox_ttl_scheduler_role}"],
            conditions={"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}},
        ),
    ]


def schedules_cap_environment(max_active: int | None) -> dict[str, str]:
    """``STRANDLY_SCHEDULES_MAX_ACTIVE``, the team's active-job cap, when ``-c schedules_max_active``
    is set; absent, the harness default applies (``ops/schedules/service.py``)."""
    if max_active is None:
        return {}
    if max_active < 1:
        raise ValueError("schedules_max_active must be a positive integer")
    return {"STRANDLY_SCHEDULES_MAX_ACTIVE": str(max_active)}


def schedules_environment(
    naming: Naming, region: str, account: str, *, max_active: int | None = None
) -> dict[str, str]:
    """The agent's ``STRANDLY_SCHEDULES_*`` (harness ``ops/schedules``), from the Scheduler stack's
    fixed names: the jobs table and group, the invoker the schedules target, the role they run as."""
    return {
        **schedules_cap_environment(max_active),
        "STRANDLY_SCHEDULES_TABLE": naming.schedules_table,
        "STRANDLY_SCHEDULES_GROUP": naming.jobs_schedule_group,
        "STRANDLY_SCHEDULES_TARGET_ARN": (
            f"arn:aws:lambda:{region}:{account}:function:{naming.scheduler_function}"
        ),
        "STRANDLY_SCHEDULES_ROLE_ARN": f"arn:aws:iam::{account}:role/{naming.jobs_scheduler_role}",
    }


def schedules_statements(naming: Naming, region: str, account: str) -> list[iam.PolicyStatement]:
    """The AGENT's grant for its ``schedule`` tool (harness ``tools/schedule.py``).

    Read/write the schedules table; create, update, delete and read schedules in the jobs group
    only; pass only the jobs scheduler role, only to Scheduler; and invoke the scheduled invoker,
    which launches a ``run_now`` (the agent never invokes its own runtime). No ``ListSchedules``:
    reconciling the group is the invoker's job.
    """
    from aws_cdk import aws_iam as iam

    table = f"arn:aws:dynamodb:{region}:{account}:table/{naming.schedules_table}"
    return [
        iam.PolicyStatement(
            sid="ScheduledJobsTable",
            actions=[
                "dynamodb:GetItem",
                "dynamodb:Query",
                "dynamodb:Scan",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
                "dynamodb:ConditionCheckItem",
            ],
            resources=[table],
        ),
        iam.PolicyStatement(
            sid="ScheduledJobsSchedules",
            actions=[
                "scheduler:CreateSchedule",
                "scheduler:UpdateSchedule",
                "scheduler:DeleteSchedule",
                "scheduler:GetSchedule",
            ],
            resources=[
                f"arn:aws:scheduler:{region}:{account}:schedule/{naming.jobs_schedule_group}/*"
            ],
        ),
        iam.PolicyStatement(
            sid="DenyReservedJobSchedules",
            effect=iam.Effect.DENY,
            actions=["scheduler:*"],
            resources=[
                f"arn:aws:scheduler:{region}:{account}:schedule/{naming.jobs_schedule_group}/_*"
            ],
        ),
        iam.PolicyStatement(
            sid="ScheduledJobsPassRole",
            actions=["iam:PassRole"],
            resources=[f"arn:aws:iam::{account}:role/{naming.jobs_scheduler_role}"],
            conditions={"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}},
        ),
        iam.PolicyStatement(
            sid="ScheduledJobsRunNow",
            actions=["lambda:InvokeFunction"],
            resources=[f"arn:aws:lambda:{region}:{account}:function:{naming.scheduler_function}"],
        ),
    ]
