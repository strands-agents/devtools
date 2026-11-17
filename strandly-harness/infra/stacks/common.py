"""Shared naming + fixed values for the unified Strandly CDK app.

Every stack is parameterized by an **environment** (``dev`` / ``prod`` / …) so one app can stand up
isolated copies side-by-side: physical resource names all derive from ``{name}-{env}`` (or
``{name}_{env}`` where the service disallows hyphens), and stack ids carry the env too. Deploying a
new env therefore never collides with an existing one — the whole point of the env knob.

The constants below **mirror** ``src/strandly_harness/constants.py`` (the infra app is a separate
package with its own venv, so it can't import the harness without dragging in the Strands SDK). Keep
them in sync by hand; they change ~never.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- fixed provisioning choices (mirror strandly_harness.core.constants) ---
MEMORY_EVENT_EXPIRY_DAYS = 30
CODE_INTERPRETER_NETWORK_MODE = "PUBLIC"
KB_EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
KB_VECTOR_DIMENSION = 1024
KB_VECTOR_DISTANCE_METRIC = "cosine"

# The "recent" GSI on the run-ledger table. Mirrors strandly_harness.core.constants.RUN_LEDGER_GSI_NAME
# (the canonical source) and dashboard/api/handler.py's GSI_NAME. The tests/test_infra_constants_sync
# guard asserts all three agree — keep them in lockstep.
RUN_LEDGER_GSI = "recent"

# The "recent" GSI on the mention-log table + its constant partition value. Mirror
# strandly_harness.core.constants.MENTION_LOG_GSI_NAME / MENTION_LOG_GSI_PK_VALUE (canonical) and
# dashboard/api/handler.py's MENTION_LOG_GSI_NAME / MENTION_LOG_GSI_PK_VALUE — the sync test guards them.
MENTION_LOG_GSI = "recent"

# --- ABAC tag boundary for the sandbox's CI execution role (e2e testing) ---
# The agent's sandbox role can only touch resources carrying MANAGED_BY_TAG_KEY == AGENT_TAG_VALUE,
# and may only create resources if it tags them so. Prod backends are tagged INFRA_TAG_VALUE (a
# DIFFERENT value), so the agent's grants can never reach them. The e2e-test skill must tag every
# resource it creates with {AGENT}, and name S3 buckets with the MANAGED_NAME_PREFIX.
MANAGED_BY_TAG_KEY = "ManagedBy"
AGENT_TAG_VALUE = "strandly"          # agent-created, ephemeral test resources
INFRA_TAG_VALUE = "strandly-infra"    # provisioner-created prod backends (off-limits to the agent)
MANAGED_NAME_PREFIX = "strandly-managed"  # S3 bucket name prefix the agent must use (name-scoped)


@dataclass(frozen=True)
class Naming:
    """Derives every physical resource name from a ``name`` prefix + ``env`` suffix.

    Two styles, because AWS services disagree on the allowed charset:
    - ``hyphen`` (``strandly-dev``) for S3 vector buckets, DynamoDB tables, Lambdas — no underscores.
    - ``under`` (``strandly_dev``) for AgentCore Memory / Code Interpreter — name regex is
      ``^[a-zA-Z][a-zA-Z0-9_]{0,47}$`` (underscores only, hyphens rejected).
    """

    name: str = "strandly"
    env: str = "dev"

    @property
    def hyphen(self) -> str:
        return f"{self.name}-{self.env}"

    @property
    def under(self) -> str:
        return f"{self.name}_{self.env}"

    # ---- per-resource names ----
    @property
    def memory(self) -> str:
        return f"{self.under}_memory"

    @property
    def code_interpreter(self) -> str:
        return f"{self.under}_ci"

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
    def poller_function(self) -> str:
        return f"{self.hyphen}-mention-poller"

    @property
    def gha_deploy_role(self) -> str:
        """The GitHub Actions OIDC *deploy* role — privileged, locked to the repo's main branch."""
        return f"{self.hyphen}-gha-deploy"

    @property
    def gha_invoke_role(self) -> str:
        """The GitHub Actions OIDC *invoke* role — minimal (InvokeAgentRuntime + poll), separate
        from deploy so a compromised invoke workflow can't redeploy the agent."""
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

    def schedule_name(self, job_name: str) -> str:
        return f"{self.hyphen}-{job_name}"

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
