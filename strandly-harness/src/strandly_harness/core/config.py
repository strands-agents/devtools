"""Configuration — loaded from AWS Secrets Manager (an arn) or the local environment / ``.env``.

Strandly is opinionated: the model, tools, plugins, prompt, and context strategy are fixed. The
only things that vary by deployment are **secrets/credentials**, and capabilities turn on **only
when their secret is present**:

- ``STRANDLY_GITHUB_TOKEN`` → the ``use_github`` tool is enabled.
- ``STRANDLY_SEARCH_MCP_URL`` (+ optional ``STRANDLY_SEARCH_MCP_TOKEN``) → the web-search MCP is added.
- ``AGENTCORE_CODE_INTERPRETER_ID`` → the managed sandbox is used (else local).
- ``AGENTCORE_MEMORY_ID`` → the AgentCore short-term session is used (else a file session).
- ``AWS_PROFILE`` / ``AWS_REGION`` → the shared boto session (else ambient credentials).

**Where the values come from.** If ``STRANDLY_SECRETS_ARN`` is set, the named Secrets Manager
secret (a JSON object of the keys above) is fetched and merged **under** the process environment
(env wins, so you can override a secret locally). Otherwise a ``.env`` file in the cwd is loaded.
Either way the result is one flat dict of strings — ``Config`` reads its values from it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import boto3

# Env/secret keys (one place).
SECRETS_ARN = "STRANDLY_SECRETS_ARN"
GITHUB_TOKEN = "STRANDLY_GITHUB_TOKEN"
SEARCH_MCP_URL = "STRANDLY_SEARCH_MCP_URL"
SEARCH_MCP_TOKEN = "STRANDLY_SEARCH_MCP_TOKEN"
CODE_INTERPRETER_ID = "AGENTCORE_CODE_INTERPRETER_ID"
MEMORY_ID = "AGENTCORE_MEMORY_ID"
# Owner write allow-list for the `use_github` tool (comma-separated owners). When set and
# non-empty it overrides the hardcoded STRANDS_ORG_OWNERS default; otherwise that default applies.
ALLOWED_OWNERS = "STRANDLY_ALLOWED_OWNERS"
# Stable actor id for AgentCore Memory continuity (reader & writer must agree). Defaults to a
# fixed constant (DEFAULT_ACTOR_ID), NOT the OS ``USER`` which is unset/``root`` in containers.
ACTOR_ID = "STRANDLY_ACTOR_ID"
# Long-term memory: a writable Bedrock Knowledge Base (CUSTOM data source). Both ids are required
# to enable it — the KB to retrieve/ingest into, and the data source to write documents to.
KB_ID = "STRANDLY_KB_ID"
KB_DATA_SOURCE_ID = "STRANDLY_KB_DATA_SOURCE_ID"
# Durable run-ledger: a DynamoDB table that each deployed run is written through to (one row per
# invocation). Optional; when unset the deployed runtime keeps only its in-memory poll store.
RUN_LEDGER_TABLE = "STRANDLY_RUN_LEDGER_TABLE"
# Operational metrics (metrics.py): the CloudWatch-EMF namespace. Optional; unset → metrics are
# a no-op. When set (e.g. "Strandly-dev") each deployed run + poll emits operational telemetry.
METRICS_NAMESPACE = "STRANDLY_METRICS_NAMESPACE"
# Stuck-run detector (ops/lambdas/stuck_runs.py): a scheduled scan of the run-ledger for rows
# left "running" past a threshold (a recycled instance never wrote a terminal row). Optional.
MONITORING_SNS_TOPIC_ARN = "STRANDLY_MONITORING_SNS_TOPIC_ARN"  # where a stuck-run finding posts
STUCK_RUN_MINUTES = "STRANDLY_STUCK_RUN_MINUTES"  # a run "running" longer than this is stuck (default 30)
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
RUNTIME_ARN = "STRANDLY_RUNTIME_ARN"  # deployed runtime ARN the poller dispatches to (fire-and-forget)

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
    throttle_enabled: bool = False
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

    def is_authorized(self, author: str | None) -> bool:
        """True iff ``author`` is a known, non-empty login in the allow-list (case-insensitive).

        An unknown/empty author is never authorized, so a
        ``reason=mention`` whose author we couldn't identify is skipped for security.

        NOTE: this covers only the *static* allow-list. Org-membership (``allowed_orgs``) is an
        ADDITIONAL, network-backed grant checked separately in ``ops.lambdas.mention_poller.handler`` so this method
        stays pure/synchronous; it is deliberately not folded in here.
        """
        if not author:
            return False
        return author.lower() in {a.lower() for a in self.allowed_authors}


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

    @property
    def github(self) -> GitHubSettings:
        """GitHub guardrail settings for the ``use_github`` tool.

        The owner write allow-list is **on by default**: it resolves to the hardcoded
        ``STRANDS_ORG_OWNERS`` (the Strands orgs) unless ``STRANDLY_ALLOWED_OWNERS`` is set to a
        non-empty comma-separated list, which overrides it. ``internal_owners`` mirrors the same
        resolved tuple, and ``strict_mutations`` stays on so unverifiable mutation targets are
        blocked rather than silently allowed.

        Each entry is either a **bare owner** (``strands-agents`` — grants the whole org) or a
        **repo glob** (``aws/bedrock-agentcore-*`` — grants only matching ``owner/repo``); the tool's
        ``_target_allowed`` matcher honours both. This is how a deployment can let Strandly write to
        specific external repos (e.g. the AgentCore SDK/CLI packages) without opening a whole org —
        e.g. ``STRANDLY_ALLOWED_OWNERS=strands-agents,strands-labs,aws/bedrock-agentcore-*,aws/agentcore-cli``.
        """
        from strandly_harness.core.constants import STRANDS_ORG_OWNERS

        raw_owners = self.get(ALLOWED_OWNERS) or ""
        parsed = tuple(o.strip() for o in raw_owners.split(",") if o.strip())
        owners = parsed or STRANDS_ORG_OWNERS
        # Resolve the token from the loaded config (Secrets Manager / .env merged under the env), in
        # token_env order, so the deployed runtime — where the token lives only in the secret and
        # never reaches os.environ — can authenticate. None ⇒ the tool falls back to os.environ.
        token = next((self.get(name) for name in GitHubSettings.token_env if self.get(name)), None)
        return GitHubSettings(allowed_owners=owners, internal_owners=owners, token=token)

    # ---- loading -------------------------------------------------------------------

    @classmethod
    def load(cls, env: dict[str, str] | None = None) -> Config:
        """Load config: Secrets Manager (if STRANDLY_SECRETS_ARN) or .env, under the environment."""
        base = dict(env if env is not None else os.environ)
        merged: dict[str, str] = {}
        arn = base.get(SECRETS_ARN)
        if arn:
            merged.update(_load_secret(arn, base.get(AWS_REGION)))
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
    def search_mcp_url(self) -> str | None:
        return self.get(SEARCH_MCP_URL)

    @property
    def search_mcp_token(self) -> str | None:
        return self.get(SEARCH_MCP_TOKEN)

    @property
    def code_interpreter_id(self) -> str | None:
        return self.get(CODE_INTERPRETER_ID)

    @property
    def memory_id(self) -> str | None:
        return self.get(MEMORY_ID)

    @property
    def actor_id(self) -> str:
        """Stable AgentCore Memory actor id (``STRANDLY_ACTOR_ID`` or a fixed default).

        A fire-and-forget poll reads the Memory session under the *same* actor id the run wrote
        under, so this must be deterministic across invocations — not the OS ``USER`` (often
        unset or ``root`` in a deployed container, and developer-specific locally).
        """
        from strandly_harness.core.constants import DEFAULT_ACTOR_ID

        return self.get(ACTOR_ID) or DEFAULT_ACTOR_ID

    @property
    def kb_id(self) -> str | None:
        return self.get(KB_ID)

    @property
    def kb_data_source_id(self) -> str | None:
        return self.get(KB_DATA_SOURCE_ID)

    @property
    def use_agentcore_sandbox(self) -> bool:
        return bool(self.code_interpreter_id)

    @property
    def use_agentcore_session(self) -> bool:
        return bool(self.memory_id)

    @property
    def use_long_term_memory(self) -> bool:
        """Long-term KB memory is on only with both the KB id and its (writable) data source id."""
        return bool(self.kb_id and self.kb_data_source_id)

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
        from strandly_harness.core.constants import STRANDS_ORGS

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
        )

    # ---- run ledger --------------------------------------------------

    @property
    def run_ledger_table(self) -> str | None:
        return self.get(RUN_LEDGER_TABLE)

    @property
    def run_ledger_enabled(self) -> bool:
        """The durable run-ledger is on only when its DynamoDB table name is configured."""
        return bool(self.run_ledger_table)

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
        """Minutes a run may sit ``running`` before the detector flags it as stuck (default 30)."""
        raw = self.get(STUCK_RUN_MINUTES)
        try:
            return int(raw) if raw else 30
        except ValueError:
            return 30

    # ---- write audit (out-of-band GitHub safety check) -----------------------------

    @property
    def audit(self) -> AuditSettings:
        raw_owners = self.get(AUDIT_ALLOWED_OWNERS) or ""
        owners = tuple(o.strip() for o in raw_owners.split(",") if o.strip())
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

        Both are required: without the allow-list there's no notion of "out of org" to flag, and
        without a token there's nothing to ask GitHub with. A misconfigured audit is a no-op, never
        a check that silently passes everything.
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
