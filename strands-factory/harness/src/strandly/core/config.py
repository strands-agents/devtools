"""Configuration, loaded from AWS Secrets Manager (``STRANDLY_SECRETS_ARN``) or the environment.

The model, tools, plugins, prompt and context strategy are fixed in code; configuration holds
credentials and operator profile settings, and each credential enables its capability (GitHub,
Slack, web search, the AgentCore sandbox, the S3 session store). Shared Slack channels also need an
explicit default-off switch. See docs/deploy.md.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import boto3

# Env/secret keys (one place).
SECRETS_ARN = "STRANDLY_SECRETS_ARN"
GITHUB_TOKEN = "STRANDLY_GITHUB_TOKEN"
# The SANDBOX's own git credential — deliberately separate from GITHUB_TOKEN. Scoped to `Contents`
# only (no Issues / Pull requests), so a credential read out of the sandbox cannot speak as the agent
# on GitHub. See Config.sandbox_git_token and docs/deploy.md.
SANDBOX_GIT_TOKEN = "STRANDLY_SANDBOX_GIT_TOKEN"
SEARCH_MCP_URL = "STRANDLY_SEARCH_MCP_URL"
SEARCH_MCP_TOKEN = "STRANDLY_SEARCH_MCP_TOKEN"
# "bearer" (default, STRANDLY_SEARCH_MCP_TOKEN as an Authorization header) or "sigv4" (SigV4-sign
# with the process's ambient AWS credentials — the AgentCore Gateway web-search connector's AWS_IAM
# authorizer type; no token to manage). See core/sigv4_httpx.py.
SEARCH_MCP_AUTH = "STRANDLY_SEARCH_MCP_AUTH"
SEARCH_MCP_REGION = "STRANDLY_SEARCH_MCP_REGION"
# The SANDBOX AgentCore Runtime — a *second*, deliberately powerless runtime that every shell
# command and file operation runs in (see sandbox/runtime.py). Required: unset, the agent refuses
# to build (sandbox/select.py). Never the agent's own runtime ARN: the
# whole point is that the box the model can run code in is not the box holding the credentials.
SANDBOX_RUNTIME_ARN = "STRANDLY_SANDBOX_RUNTIME_ARN"
# Optional endpoint qualifier on the sandbox runtime ("DEFAULT", or a pinned version). Unset lets
# the service resolve its default endpoint, which is what you want unless you are staging an image.
SANDBOX_RUNTIME_QUALIFIER = "STRANDLY_SANDBOX_RUNTIME_QUALIFIER"
# Deploy wiring for the sandbox sessions' sliding TTL (sandbox/ttl.py) — the Sandbox stack's
# CapacityProviderId / SandboxTtlScheduleGroup / SandboxTtlSchedulerRoleArn outputs, set on the
# AGENT runtime exactly like the sandbox runtime ARN. Not user knobs: all three or none (then no
# session is ever scheduled for deletion, and its EBS volume is kept until deleted by hand).
SANDBOX_CAPACITY_PROVIDER_ID = "STRANDLY_SANDBOX_CAPACITY_PROVIDER_ID"
SANDBOX_TTL_SCHEDULE_GROUP = "STRANDLY_SANDBOX_TTL_SCHEDULE_GROUP"
SANDBOX_TTL_ROLE_ARN = "STRANDLY_SANDBOX_TTL_ROLE_ARN"
# The AgentCore Memory: transcript events + long-term records (memory/transcript.py,
# memory/long_term.py).
MEMORY_ID = "AGENTCORE_MEMORY_ID"
# The per-env S3 bucket holding the short-term session store (memory/session_store.py): snapshots
# and the context-manager stash. Unset ⇒ local files.
SESSION_BUCKET = "STRANDLY_SESSION_BUCKET"
# Owner write allow-list for the `use_github` tool (comma-separated owners). When set and
# empty (or absent) denies writes. Installation defaults are explicit profile inputs.
ALLOWED_OWNERS = "STRANDLY_ALLOWED_OWNERS"
# Daily cap on `use_github` writes outside the bare-owner allow-list entries. On unless set to
# "false"; the limit falls back to GitHubSettings.throttle_limit when not a positive integer.
GITHUB_THROTTLE_ENABLED = "STRANDLY_GITHUB_THROTTLE_ENABLED"
GITHUB_THROTTLE_LIMIT = "STRANDLY_GITHUB_THROTTLE_LIMIT"
# The top agent's model as a harness model string ("bedrock/<model id>", "anthropic/<name>", ...)
# and its harness effort. Unset ⇒ the harness default model and ``constants.EFFORT_DEFAULT``.
MODEL = "STRANDLY_MODEL"
EFFORT = "STRANDLY_EFFORT"
# Durable run-ledger: a DynamoDB table that each deployed run is written through to (one row per
# invocation). Optional; when unset the deployed runtime keeps only its in-memory poll store.
RUN_LEDGER_TABLE = "STRANDLY_RUN_LEDGER_TABLE"
# Portal work-product store: a DynamoDB table holding the structured review/triage artifacts the
# agent emits (see ops/work_product.py). Optional; unset ⇒ the runtime skips the
# serialize-and-persist step and the portal's Today/Review/Triage views have no data (fail-open).
WORK_PRODUCT_TABLE = "STRANDLY_WORK_PRODUCT_TABLE"
# Operational metrics (metrics.py): the CloudWatch-EMF namespace. Optional; unset → metrics are
# a no-op. When set (e.g. "Strandly-dev") each deployed run + poll emits operational telemetry.
METRICS_NAMESPACE = "STRANDLY_METRICS_NAMESPACE"
# Stuck-run detector (ops/lambdas/stuck_runs.py): a scheduled scan of the run-ledger for rows
# left "running" past a threshold (a recycled instance never wrote a terminal row). Optional.
MONITORING_SNS_TOPIC_ARN = "STRANDLY_MONITORING_SNS_TOPIC_ARN"  # where a stuck-run finding posts
STUCK_RUN_MINUTES = "STRANDLY_STUCK_RUN_MINUTES"  # a run "running" longer than this is stuck (deployed: 24 h)
# Tokens above which a user message's text is offloaded to the stash (core/agent._context_manager).
USER_TEXT_OFFLOAD_TOKENS = "STRANDLY_USER_TEXT_OFFLOAD_TOKENS"
AWS_PROFILE = "AWS_PROFILE"
AWS_REGION = "AWS_REGION"

# Mention poller (ingress): the AWS-native GitHub ``@mention`` trigger layer. The poller is a
# gated capability like the others — it turns on only when its notifications token *and* a deployed
# runtime ARN to dispatch to are both present.
NOTIFICATIONS_TOKEN = "STRANDLY_NOTIFICATIONS_TOKEN"  # PAT that can read cross-repo notifications
MENTION_HANDLE = "STRANDLY_MENTION_HANDLE"  # the @handle the poller searches for, e.g. your-bot-handle
MENTION_ALLOWED_AUTHORS = "STRANDLY_MENTION_ALLOWED_AUTHORS"  # comma-separated allow-list
MENTION_ALLOWED_ORGS = "STRANDLY_MENTION_ALLOWED_ORGS"  # comma-separated orgs whose members may invoke
MENTION_SKIP_REPO = "STRANDLY_MENTION_SKIP_REPO"  # own repo to skip (handled by direct events)
DEDUP_TABLE = "STRANDLY_DEDUP_TABLE"  # DynamoDB table name for the durable dispatch backstop
MENTION_LOG_TABLE = "STRANDLY_MENTION_LOG_TABLE"  # DynamoDB table logging every processed mention (dashboard)
# People directory (ops/people.py): the DynamoDB table of persons who may trigger the agent, with
# their GitHub/Slack identities. Read by the ingress Lambdas ONLY — never folded into the runtime's
# config secret and never granted to the runtime exec role (it is the allow-list, and must stay
# outside the agent's reach). Unset ⇒ the ingresses fall back to the static secret lists alone.
PEOPLE_TABLE = "STRANDLY_PEOPLE_TABLE"
RUNTIME_ARN = "STRANDLY_RUNTIME_ARN"  # deployed runtime ARN the poller dispatches to (fire-and-forget)

# Slack (DM ingress + the agent's reply tool). The bot token gates the ``use_slack`` tool the
# same way GITHUB_TOKEN gates ``use_github``. The signing secret is **ingress-Lambda-only**: the
# slack_ingress Lambda reads it straight from its own Secrets Manager secret to verify request
# signatures — it is deliberately NOT surfaced as a Config property, so it never reaches the agent.
SLACK_BOT_TOKEN = "STRANDLY_SLACK_BOT_TOKEN"
SLACK_SIGNING_SECRET = "STRANDLY_SLACK_SIGNING_SECRET"  # ingress-only, Lambda-side (not loaded here)
SLACK_ALLOWED_USERS = "STRANDLY_SLACK_ALLOWED_USERS"  # comma-separated Slack user ids (DM allow-list)
# The team's Slack home channel (a ``C…``/``G…`` id). Unset ⇒ every home-channel behaviour is off:
# ``use_slack`` stays a Slack-triggered reply path and scheduled jobs get no default notify target.
# The Slack-stack Lambdas read the same id from the Slack secret's ``home_channel`` key.
SLACK_HOME_CHANNEL_ENABLED = "STRANDLY_SLACK_HOME_CHANNEL_ENABLED"
SLACK_HOME_CHANNEL = "STRANDLY_SLACK_HOME_CHANNEL"
_SLACK_CHANNEL_ID_RE = re.compile(r"^[CG][A-Z0-9]{2,}$")


def home_channel_enabled(raw: object) -> bool:
    """Only an explicit operator true enables shared-channel features; absent/invalid is off."""
    return str(raw).strip().lower() == "true"


def slack_channel_id(raw: object) -> str | None:
    """A well-formed public/private Slack channel id (``C…``/``G…``), else ``None``."""
    value = raw.strip() if isinstance(raw, str) else ""
    return value if _SLACK_CHANNEL_ID_RE.match(value) else None

# Independent GitHub write-audit (ops/lambdas/mention_poller/audit.py): an out-of-band scheduled job that asks
# GitHub directly what our token's account actually did, and flags any write outside the
# allow-list. Gated on a non-empty allow-list AND a token (its own read-only token ideally).
AUDIT_ALLOWED_OWNERS = "STRANDLY_AUDIT_ALLOWED_OWNERS"  # comma-separated owners we permit
AUDIT_TOKEN = "STRANDLY_AUDIT_TOKEN"  # read-only audit PAT; falls back to the notifications/github token
AUDIT_SNS_TOPIC_ARN = "STRANDLY_AUDIT_SNS_TOPIC_ARN"  # where a violation finding is published
AUDIT_LOOKBACK_HOURS = "STRANDLY_AUDIT_LOOKBACK_HOURS"  # how far back each pass looks (default 24)


@dataclass(frozen=True)
class GitHubSettings:
    """Repo-scope guardrails for the ``use_github`` tool (beyond the harness's normal gates)."""

    allowed_owners: tuple[str, ...] = ()
    strict_mutations: bool = True
    throttle_enabled: bool = True
    throttle_limit: int = 50
    internal_owners: tuple[str, ...] = ()
    # The token env var(s) checked, in order. Strandly's token is STRANDLY_GITHUB_TOKEN; the
    # legacy names are kept as fallbacks so a stock GITHUB_TOKEN also works locally.
    token_env: tuple[str, ...] = ("STRANDLY_GITHUB_TOKEN", "GITHUB_TOKEN", "PAT_TOKEN")
    # The token resolved from the loaded Config (which merges Secrets Manager / .env under the
    # process env). Carried here so `use_github` can authenticate when the token lives ONLY in the
    # secret and never reaches os.environ — the deployed-runtime case (STRANDLY_SECRETS_ARN). None
    # falls back to reading token_env from os.environ (local / GitHub-Actions).
    token: str | None = None


@dataclass(frozen=True)
class MentionPollerSettings:
    """Config for the AWS mention poller (ingress) — the GitHub ``@mention`` trigger layer.

    All values are deployment-specific (the handle to watch, who may trigger it, the runtime to
    dispatch to), so unlike :class:`GitHubSettings` these are read from the loaded config, not fixed.
    """

    handle: str = ""
    allowed_authors: tuple[str, ...] = ()
    allowed_orgs: tuple[str, ...] = ()
    skip_repo: str | None = None
    runtime_arn: str | None = None
    region: str | None = None
    dedup_table: str | None = None
    mention_log_table: str | None = None
    people_table: str | None = None
    # The owners (or ``owner/repo`` globs) the deployment manages, the same list ``use_github``
    # enforces; the poller dispatches only for mentions in these repositories. Empty manages none.
    managed_owners: tuple[str, ...] = ()

    def is_authorized(self, author: str | None) -> bool:
        """True iff ``author`` is a known, non-empty login in the allow-list (case-insensitive).

        An unknown/empty author is never authorized, so a
        ``reason=mention`` whose author we couldn't identify is skipped for security.

        NOTE: this covers only the *static* allow-list. The people directory (``people_table``,
        ``ops.people``) and org-membership (``allowed_orgs``) are ADDITIONAL, I/O-backed grants
        checked separately in ``ops.lambdas.mention_poller.handler`` so this method stays
        pure/synchronous; they are deliberately not folded in here.
        """
        if not author:
            return False
        return author.lower() in {a.lower() for a in self.allowed_authors}


@dataclass(frozen=True)
class SlackSettings:
    """Settings for the ``use_slack`` tool (and, informationally, the DM allow-list).

    ``bot_token`` is what the tool authenticates with. ``allowed_users`` is the DM allow-list the
    *ingress* enforces (``ops/lambdas/slack_ingress``) — carried here so the loaded config is the
    one place both sides read it from; the tool itself only posts and does not gate on it. The
    signing secret is deliberately absent: it is ingress-Lambda-only and never reaches the agent.
    """

    bot_token: str = ""
    allowed_users: tuple[str, ...] = ()
    home_channel: str | None = None
    home_channel_enabled: bool = False


@dataclass(frozen=True)
class AuditSettings:
    """Config for the independent GitHub write-audit (``ops/lambdas/mention_poller/audit.py``).

    All deployment-specific (which owners are in-org, which token to audit with, where to notify),
    so like :class:`MentionPollerSettings` these are read from the loaded config, not fixed.
    """

    allowed_owners: tuple[str, ...] = ()
    token: str | None = None
    sns_topic_arn: str | None = None
    lookback_hours: int = 24
    region: str | None = None


@dataclass(frozen=True)
class Config:
    """Resolved deployment config — a thin view over the loaded values dict."""

    values: dict[str, str]

    def __post_init__(self) -> None:
        # Trusted installation defaults merge BELOW secret/.env/process values, including empty
        # values: an explicit empty allowlist revokes access instead of restoring a fallback.
        defaults = json.loads(self.values.get("STRANDLY_PROFILE_DEFAULTS", "{}"))
        path = self.values.get("STRANDLY_PROFILE")
        if path:
            from strandly.profiles import Profile
            defaults.update(Profile.load(path).runtime_values())
        object.__setattr__(self, "values", {**defaults, **self.values})

    @property
    def github(self) -> GitHubSettings:
        """GitHub guardrail settings for the ``use_github`` tool.

        A fresh installation denies writes. An operator profile can supply defaults, and the
        secret/process environment may override them, including an empty list to revoke access.
        ``internal_owners`` mirrors the resolved policy; strict mutation checking remains enabled.
        The external-write throttle is on unless ``STRANDLY_GITHUB_THROTTLE_ENABLED`` is ``false``,
        with ``STRANDLY_GITHUB_THROTTLE_LIMIT`` writes per 24 hours.

        Each entry is either a **bare owner** (``example-owner`` — grants the whole org) or a
        **repo glob** (``aws/bedrock-agentcore-*`` — grants only matching ``owner/repo``); the tool's
        ``_target_allowed`` matcher honours both. This is how a deployment can let Strandly write to
        specific external repos (e.g. the AgentCore SDK/CLI packages) without opening a whole org —
        e.g. ``STRANDLY_ALLOWED_OWNERS=example-owner,example-org/approved-*``.
        """
        from strandly.core.constants import STRANDS_ORG_OWNERS

        raw_owners = self.get(ALLOWED_OWNERS) or ""
        parsed = tuple(o.strip() for o in raw_owners.split(",") if o.strip())
        owners = parsed or STRANDS_ORG_OWNERS
        # Resolve the token from the loaded config (Secrets Manager / .env merged under the env), in
        # token_env order, so the deployed runtime — where the token lives only in the secret and
        # never reaches os.environ — can authenticate. None ⇒ the tool falls back to os.environ.
        token = next((self.get(name) for name in GitHubSettings.token_env if self.get(name)), None)
        # Anything but an explicit "false" keeps the throttle on, so a typo cannot disable it.
        throttle_enabled = (self.get(GITHUB_THROTTLE_ENABLED) or "").strip().lower() != "false"
        throttle_limit = GitHubSettings.throttle_limit
        raw_limit = (self.get(GITHUB_THROTTLE_LIMIT) or "").strip()
        if raw_limit.isdigit() and int(raw_limit) > 0:
            throttle_limit = int(raw_limit)
        return GitHubSettings(
            allowed_owners=owners,
            internal_owners=owners,
            token=token,
            throttle_enabled=throttle_enabled,
            throttle_limit=throttle_limit,
        )

    @property
    def aws_test_scope(self) -> str:
        """Non-secret scope hints for skills; these bounds are not a grant of permission."""
        tag = self.get("STRANDLY_TEST_MANAGED_BY_VALUE")
        prefix = self.get("STRANDLY_TEST_NAME_PREFIX")
        protected = self.get("STRANDLY_PROTECTED_MANAGED_BY_VALUE")
        if not (tag and prefix and protected):
            return ""
        return (
            f"Test resource tag: `ManagedBy={tag}`. Test name prefix: `{prefix}`. "
            f"Protected infrastructure tag: `ManagedBy={protected}`. "
            "These are the deployment's scope bounds, NOT authorization or credentials. "
            "Only create test resources when explicitly authorized; never guess a scope from a GitHub login."
        )

    # ---- loading -------------------------------------------------------------------

    @classmethod
    def load(cls, env: dict[str, str] | None = None) -> Config:
        """Load config: Secrets Manager (if STRANDLY_SECRETS_ARN) or .env, under the environment."""
        base = dict(env if env is not None else os.environ)
        merged: dict[str, str] = {}
        if base.get("STRANDLY_PROFILE"):
            from strandly.profiles import Profile
            merged.update(Profile.load(base["STRANDLY_PROFILE"]).runtime_values())
        arn = base.get(SECRETS_ARN, merged.get(SECRETS_ARN))
        region = base.get(AWS_REGION, merged.get(AWS_REGION))
        if arn:
            merged.update(_load_secret(arn, region))
        else:
            merged.update(_load_dotenv(Path(".env")))
        merged.update(base)  # process env always wins over the secret/.env source
        return cls(values=merged)

    def get(self, key: str) -> str | None:
        v = self.values.get(key)
        return v or None

    # ---- credentials ---------------------------------------------------------------

    @property
    def aws_region(self) -> str | None:
        return self.get(AWS_REGION) or self.get("AWS_DEFAULT_REGION")

    def boto_session(self) -> boto3.Session | None:
        """Shared boto3 session, or None for ambient defaults (the local fallback)."""
        profile, region = self.get(AWS_PROFILE), self.aws_region
        if not profile and not region:
            return None
        return _session(profile, region)

    # ---- capability gates (a capability is on only when its secret is present) -----

    @property
    def github_enabled(self) -> bool:
        return bool(self.get(GITHUB_TOKEN))

    @property
    def github_token(self) -> str | None:
        """The GitHub token itself — first non-empty of the ``use_github`` tool's env-var order.

        Same precedence as the tool (``GitHubSettings.token_env``: ``STRANDLY_GITHUB_TOKEN``,
        then the legacy ``GITHUB_TOKEN`` / ``PAT_TOKEN``), but resolved through :meth:`get` so a
        Secrets-Manager-only deployment (no process env var) also finds it. Used to bootstrap the
        sandbox's git credentials, so native ``git clone``/``push`` inside the sandbox authenticates
        as the same identity as the tool's API writes.
        """
        for name in self.github.token_env:
            value = self.get(name)
            if value:
                return value
        return None

    @property
    def sandbox_git_token(self) -> str | None:
        """The token bootstrapped into the sandbox's git client — NOT the ``use_github`` token.

        Returns ``STRANDLY_SANDBOX_GIT_TOKEN`` if set, else ``None``. It deliberately does **not**
        fall back to :attr:`github_token`: the whole point is that the credential inside the sandbox
        is weaker than the one the tool holds. Falling back would silently restore the very exposure
        this key exists to remove (a full-privilege token readable by any code the agent runs — and
        the agent does read it: ``git credential fill`` was observed in prod).

        Scope it to **Contents: Read and write + Metadata: Read** and nothing else. Contents is all
        native git needs (clone / fetch / push); withholding Issues and Pull requests is what stops a
        leaked credential from commenting, reviewing, or approving as the agent. Everything that
        *speaks* as the agent goes through ``use_github`` host-side, never through git.

        Unset ⇒ the sandbox gets no git credential at all: public clones still work anonymously
        (which is all that was observed in prod), but pushes fail. Set it to enable pushes.
        """
        return self.get(SANDBOX_GIT_TOKEN)

    @property
    def slack_enabled(self) -> bool:
        return bool(self.get(SLACK_BOT_TOKEN))

    @property
    def slack(self) -> SlackSettings:
        """Slack settings for the ``use_slack`` tool (token + the DM allow-list)."""
        raw_users = self.get(SLACK_ALLOWED_USERS) or ""
        users = tuple(u.strip() for u in raw_users.split(",") if u.strip())
        return SlackSettings(
            bot_token=self.get(SLACK_BOT_TOKEN) or "",
            allowed_users=users,
            home_channel=self.slack_home_channel,
            home_channel_enabled=home_channel_enabled(self.get(SLACK_HOME_CHANNEL_ENABLED)),
        )

    @property
    def slack_home_channel(self) -> str | None:
        """The enabled home destination, or ``None`` when the switch, token, or valid ID is absent."""
        if not self.slack_enabled or not home_channel_enabled(self.get(SLACK_HOME_CHANNEL_ENABLED)):
            return None
        return slack_channel_id(self.get(SLACK_HOME_CHANNEL))

    @property
    def search_mcp_url(self) -> str | None:
        return self.get(SEARCH_MCP_URL)

    @property
    def search_mcp_token(self) -> str | None:
        return self.get(SEARCH_MCP_TOKEN)

    @property
    def search_mcp_sigv4(self) -> bool:
        """True when the web-search MCP endpoint expects SigV4 auth instead of a bearer token."""
        return (self.get(SEARCH_MCP_AUTH) or "").strip().lower() == "sigv4"

    @property
    def search_mcp_region(self) -> str | None:
        """Region to sign SigV4 requests for (the Gateway's region, independent of the deployment's)."""
        return self.get(SEARCH_MCP_REGION) or self.aws_region

    @property
    def sandbox_runtime_arn(self) -> str | None:
        """ARN of the sandbox AgentCore Runtime the agent's shell/file tools execute in."""
        return self.get(SANDBOX_RUNTIME_ARN)

    @property
    def sandbox_runtime_qualifier(self) -> str | None:
        """Endpoint qualifier for the sandbox runtime, or ``None`` for the service default."""
        return self.get(SANDBOX_RUNTIME_QUALIFIER)

    @property
    def sandbox_capacity_provider_id(self) -> str | None:
        """The sandbox's EC2 capacity provider id (deploy wiring for the session TTL)."""
        return self.get(SANDBOX_CAPACITY_PROVIDER_ID)

    @property
    def sandbox_ttl_schedule_group(self) -> str | None:
        """The Scheduler group the sandbox session TTL schedules live in (deploy wiring)."""
        return self.get(SANDBOX_TTL_SCHEDULE_GROUP)

    @property
    def sandbox_ttl_role_arn(self) -> str | None:
        """The role Scheduler assumes to delete an expired sandbox session (deploy wiring)."""
        return self.get(SANDBOX_TTL_ROLE_ARN)

    @property
    def memory_id(self) -> str | None:
        """The AgentCore Memory id: transcript events and long-term records."""
        return self.get(MEMORY_ID)

    @property
    def model(self) -> str:
        """The top agent's model string (``STRANDLY_MODEL``, else the harness default model)."""
        raw = self.get(MODEL)
        if raw and raw.strip():
            return raw.strip()
        from strands_harness.defaults import DEFAULT_MODEL

        return DEFAULT_MODEL

    @property
    def effort(self) -> str:
        """The top agent's harness effort (``STRANDLY_EFFORT``, else ``constants.EFFORT_DEFAULT``)."""
        from strandly.core.constants import EFFORT_DEFAULT

        raw = self.get(EFFORT)
        return raw.strip() if raw and raw.strip() else EFFORT_DEFAULT

    @property
    def session_bucket(self) -> str | None:
        """The session-store bucket (``STRANDLY_SESSION_BUCKET``), or ``None`` for local files."""
        return self.get(SESSION_BUCKET)

    @property
    def use_long_term_memory(self) -> bool:
        """Long-term memory is on whenever the AgentCore Memory is configured."""
        return bool(self.memory_id)

    # ---- mention poller (ingress) --------------------------------------------------

    @property
    def notifications_token(self) -> str | None:
        """PAT used to read cross-repo notifications. Falls back to the github tool's token."""
        return self.get(NOTIFICATIONS_TOKEN) or self.get(GITHUB_TOKEN)

    @property
    def runtime_arn(self) -> str | None:
        return self.get(RUNTIME_ARN)

    @property
    def poller_enabled(self) -> bool:
        """The poller runs only when it has a notifications token AND a runtime to dispatch to."""
        return bool(self.notifications_token and self.runtime_arn)

    @property
    def mention_poller(self) -> MentionPollerSettings:
        from strandly.core.constants import STRANDS_ORGS

        raw_authors = self.get(MENTION_ALLOWED_AUTHORS) or ""
        authors = tuple(a.strip() for a in raw_authors.split(",") if a.strip())
        # Org-membership invoke gate: comma-separated orgs whose members may invoke, falling back to
        # the STRANDS_ORGS default pair whenever the env value is unset/empty/whitespace-only (a
        # stray comma can't silently disable the gate). To run with NO org gating, construct
        # MentionPollerSettings(allowed_orgs=()) directly.
        raw_orgs = self.get(MENTION_ALLOWED_ORGS) or ""
        parsed_orgs = tuple(o.strip() for o in raw_orgs.split(",") if o.strip())
        orgs = parsed_orgs or STRANDS_ORGS
        return MentionPollerSettings(
            handle=(self.get(MENTION_HANDLE) or "").lstrip("@"),
            allowed_authors=authors,
            allowed_orgs=orgs,
            skip_repo=self.get(MENTION_SKIP_REPO),
            runtime_arn=self.runtime_arn,
            region=self.aws_region,
            dedup_table=self.get(DEDUP_TABLE),
            mention_log_table=self.get(MENTION_LOG_TABLE),
            people_table=self.get(PEOPLE_TABLE),
            managed_owners=self.github.allowed_owners,
        )

    # ---- run ledger --------------------------------------------------

    @property
    def run_ledger_table(self) -> str | None:
        return self.get(RUN_LEDGER_TABLE)

    @property
    def run_ledger_enabled(self) -> bool:
        """The durable run-ledger is on only when its DynamoDB table name is configured."""
        return bool(self.run_ledger_table)

    # ---- work-product store (portal review/triage artifacts) ---------

    @property
    def work_product_table(self) -> str | None:
        return self.get(WORK_PRODUCT_TABLE)

    @property
    def work_product_enabled(self) -> bool:
        """The portal work-product store is on only when its DynamoDB table name is configured."""
        return bool(self.work_product_table)

    # ---- operational metrics (CloudWatch EMF) --------------------------------------

    @property
    def metrics_namespace(self) -> str | None:
        """The CloudWatch-EMF metric namespace, or ``None`` when metrics are disabled."""
        return self.get(METRICS_NAMESPACE)

    @property
    def metrics_enabled(self) -> bool:
        """Operational metrics are emitted only when a namespace is configured."""
        return bool(self.metrics_namespace)

    # ---- stuck-run monitoring ------------------------------------------------------

    @property
    def monitoring_sns_topic_arn(self) -> str | None:
        """SNS topic the stuck-run detector publishes a finding to (optional)."""
        return self.get(MONITORING_SNS_TOPIC_ARN)

    @property
    def stuck_run_minutes(self) -> int:
        """Minutes a run may sit ``running`` before the detector flags it as stuck.

        The deployed detector always gets ``STRANDLY_STUCK_RUN_MINUTES`` from the Monitoring stack
        (infra ``STUCK_RUN_MINUTES``, 8 h: the agent session maximum); 30 is only the
        unset fallback for local use.
        """
        raw = self.get(STUCK_RUN_MINUTES)
        try:
            return int(raw) if raw else 30
        except ValueError:
            return 30

    @property
    def user_text_offload_tokens(self) -> int:
        """Tokens above which a user text block is offloaded; the constant unless overridden.

        A value that is not an integer above the preview size falls back to the default, since the
        SDK rejects a threshold at or below the preview it cuts to.
        """
        from strandly.core.constants import (
            CONTEXT_USER_TEXT_PREVIEW_TOKENS,
            CONTEXT_USER_TEXT_THRESHOLD_TOKENS,
        )

        raw = self.get(USER_TEXT_OFFLOAD_TOKENS)
        try:
            value = int(raw) if raw else CONTEXT_USER_TEXT_THRESHOLD_TOKENS
        except ValueError:
            value = 0
        return value if value > CONTEXT_USER_TEXT_PREVIEW_TOKENS else CONTEXT_USER_TEXT_THRESHOLD_TOKENS

    # ---- write audit (out-of-band GitHub safety check) -----------------------------

    @property
    def audit(self) -> AuditSettings:
        """Out-of-band GitHub write-audit settings.

        ``allowed_owners`` defaults to the SAME resolved list ``Config.github`` uses (the write
        guardrail) — one edit to ``STRANDLY_ALLOWED_OWNERS`` keeps both systems in sync, so the
        audit can never silently drift to a stale allow-list independent of what writes are
        actually permitted. ``STRANDLY_AUDIT_ALLOWED_OWNERS`` still overrides it when set, for the
        rare case an operator wants the audit to check a narrower/wider set than the write gate
        itself (e.g. auditing more strictly than what's currently allowed, during a migration).
        """
        raw_owners = self.get(AUDIT_ALLOWED_OWNERS) or ""
        owners = tuple(o.strip() for o in raw_owners.split(",") if o.strip()) or self.github.allowed_owners
        raw_hours = self.get(AUDIT_LOOKBACK_HOURS)
        try:
            hours = int(raw_hours) if raw_hours else 24
        except ValueError:
            hours = 24
        return AuditSettings(
            allowed_owners=owners,
            # Prefer a dedicated read-only audit token; fall back to the notifications/github token
            # so the audit still runs when only the existing token is available.
            token=self.get(AUDIT_TOKEN) or self.notifications_token,
            sns_topic_arn=self.get(AUDIT_SNS_TOPIC_ARN),
            lookback_hours=hours,
            region=self.aws_region,
        )

    @property
    def audit_enabled(self) -> bool:
        """The write-audit runs only with a non-empty allow-list AND a token to audit with.

        ``Config.audit`` always resolves a non-empty allow-list, but ``AuditSettings`` built
        directly can hold an empty one: a misconfigured audit is a no-op, never a check that
        passes everything.
        """
        s = self.audit
        return bool(s.allowed_owners and s.token)


def _region_from_secret_arn(arn: str) -> str | None:
    """The region embedded in a secret ARN (``arn:aws:secretsmanager:<region>:...``), or None.

    A full secret ARN always carries its region in the 4th ``:``-delimited field. Falling back to it
    means the secret still loads even when neither ``AWS_REGION`` nor a boto default region is set —
    e.g. the AgentCore runtime container, whose process env has no ``AWS_REGION``, where relying on
    the passed-in region would raise ``NoRegionError`` and silently leave the config (and thus the
    GitHub token) empty.
    """
    parts = arn.split(":")
    return parts[3] if len(parts) > 4 and parts[3] else None


def _load_secret(arn: str, region: str | None) -> dict[str, str]:
    """Fetch a Secrets Manager secret (a JSON object) and return it as a flat str dict."""
    import boto3

    # Prefer the caller's region, but fall back to the region baked into the ARN so the fetch works
    # even when the process has no AWS_REGION (the runtime container) — otherwise NoRegionError.
    region = region or _region_from_secret_arn(arn)
    client = boto3.Session(region_name=region).client("secretsmanager")
    resp = client.get_secret_value(SecretId=arn)
    raw = resp.get("SecretString") or "{}"
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"secret {arn} must be a JSON object")
    return {str(k): str(v) for k, v in data.items()}


def _load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env reader (KEY=VALUE per line, # comments, optional quotes). No deps."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


@lru_cache(maxsize=8)
def _session(profile: str | None, region: str | None) -> Any:
    import boto3

    return boto3.Session(profile_name=profile, region_name=region)
