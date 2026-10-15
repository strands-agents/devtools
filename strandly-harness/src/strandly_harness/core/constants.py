"""Fixed choices for the harness — opinionated, not configurable.

Everything here is a deliberate decision, not a knob. The handful of things that legitimately vary
by deployment (AWS creds, GitHub guardrails, a knowledge-base id) live in ``settings.py``; the
things that vary per request (session id, cwd) are arguments to ``build_agent``.
"""

from __future__ import annotations

from typing import Any

# --- model: Claude Opus 4.8 on Bedrock, adaptive thinking, real 1M window pinned ---
MODEL_ID = "global.anthropic.claude-opus-4-8"
MODEL_MAX_TOKENS = 32_000
MODEL_CONTEXT_WINDOW = 1_000_000
MODEL_THINKING_CONFIG = {
    "thinking": {"type": "adaptive"},
    "output_config": {"effort": "high"},
}
MODEL_CACHE_CONFIG = {"strategy": "auto", "ttl": "1h"}
MODEL_CACHE_TOOLS = {"type": "default", "ttl": "1h"}
MODEL_SYSTEM_CACHE_POINT = {"cachePoint": {"type": "default", "ttl": "1h"}}
MODEL_READ_TIMEOUT_SECONDS = 300

# Model-call retry posture (issue: model-layer retry gap). botocore's default is the "legacy"
# retry mode with a small attempt budget and narrow error coverage; "adaptive" adds exponential
# backoff + client-side rate limiting, which rides out multi-minute Bedrock ServiceUnavailable /
# Throttling windows instead of surfacing them after a handful of attempts. These cover failures
# *before/at request time*; a drop mid-EventStream is handled one level up by the run-level retry
# in ``serve.agentcore_app._run`` (see ``strandly_harness.core.retries``).
MODEL_BOTO_MAX_ATTEMPTS = 10
MODEL_BOTO_RETRY_MODE = "adaptive"

# Run-level (mid-stream) retry: a long streaming turn that dies to a transient infra error is
# re-invoked with a continuation prompt on the SAME cached per-session agent (history preserved →
# the run resumes instead of restarting). Bounded attempts, exponential backoff with jitter.
RUN_RETRY_MAX_ATTEMPTS = 6
RUN_RETRY_BACKOFF_BASE_SECONDS = 8.0
RUN_RETRY_BACKOFF_MAX_SECONDS = 120.0

# --- subagent model tiers (the `spawn` tool's `model` argument) ---
# A fixed, deliberate subset — Claude family only, no free-form model ids. The TOP agent is always
# the default (Opus 4.8); a spawned subagent may pick a tier that matches its task:
#   default  — Opus 4.8, the harness model. What you get when `model` is omitted.
#   fast     — Haiku 4.5: cheap and quick, for simple mechanical subtasks (routing decisions,
#              formatting, small summaries) where Opus-depth is wasted latency and cost.
#   advanced — Fable 5 (Mythos-class): maximum-depth analysis, for passes where the quality of
#              thought IS the deliverable — adversarial testing, API bar-raising, subtle
#              correctness hunts. NOTE: Fable requires the account's Bedrock data retention set to
#              `provider_data_share` (account-wide, per-region; inputs/outputs leave AWS's boundary
#              and are human-reviewable — public/OSS work only). Where it isn't enabled the invoke
#              fails and the spawn surfaces an error — retry on the default tier per the skills'
#              failed-pass guidance.
# Each tier pins its own max_tokens / context window / thinking effort; cache and read-timeout
# settings are shared (model.py). The tier KEYS are the spawn contract — every skill references
# them — so renaming one is a breaking change.
MODEL_TIER_DEFAULT = "default"
MODEL_TIERS: dict[str, dict[str, Any]] = {
    "default": {
        "model_id": MODEL_ID,
        "max_tokens": MODEL_MAX_TOKENS,
        "context_window": MODEL_CONTEXT_WINDOW,
        "thinking_config": MODEL_THINKING_CONFIG,
    },
    "fast": {
        # Full dated inference-profile id: unlike opus-4-8 / fable-5 (which resolve as bare
        # aliases), Haiku 4.5 is only registered under the versioned id — the short form throws
        # ValidationException: "provided model identifier is invalid".
        "model_id": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "max_tokens": 16_000,
        # Haiku's real window is 200k — do NOT inherit the 1M pin.
        "context_window": 200_000,
        # Haiku 4.5 does NOT support adaptive thinking or output_config.effort (both raise
        # ValidationException — verified against live Bedrock); the fast tier is for cheap
        # mechanical subtasks anyway, so thinking is disabled.
        "thinking_config": {"thinking": {"type": "disabled"}},
    },
    "advanced": {
        "model_id": "global.anthropic.claude-fable-5",
        "max_tokens": MODEL_MAX_TOKENS,
        "context_window": MODEL_CONTEXT_WINDOW,
        "thinking_config": {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}},
    },
}

# --- use_github write allow-list ---
# Hardcoded default owner allow-list for the `use_github` tool's repository-scope guardrail
# (config.py wires this into GitHubSettings.allowed_owners / internal_owners). Strandly may only
# write to repos under these orgs unless STRANDLY_ALLOWED_OWNERS overrides it. Deliberately scoped
# to the Strands orgs — NOT mkmeral/* or agent-of-mkmeral/*.
STRANDS_ORG_OWNERS = ("strands-agents", "strands-labs")

# --- context management ---
# Context management is context_manager="agentic" (set in agent.py): the model manages its own
# history via injected tools, with a SummarizingConversationManager as a reactive overflow safety
# net. We only override the offloader so oversized tool results land in the sandbox FS instead of
# memory. These mirror the SDK "agentic" offloader values (max_result=8000, preview=750) — a higher
# inline threshold than "auto" (1500) since the model decides what to compress.
OFFLOAD_MAX_RESULT_TOKENS = 8_000
OFFLOAD_PREVIEW_TOKENS = 750
OFFLOAD_DIR = "./artifacts"

# --- session ---
SESSION_DIR = "./sessions"
# AgentCore Runtime session ids (instance affinity) must be slash-free and 33–256 chars. The
# Memory session id (what we read back) only needs to be slash-free — these are two different
# ids that happen to derive from the same user-supplied value (see memory.runtime_session_id).
RUNTIME_SESSION_ID_MIN_LEN = 33
# Stable default actor id for AgentCore Memory. A poll must address the SAME actor id the run
# wrote under; the OS ``USER`` is brittle (unset/``root`` in a container, varies per dev), so we
# default to a fixed constant and let ``STRANDLY_ACTOR_ID`` override it. Reader and writer agree.
DEFAULT_ACTOR_ID = "strandly"

# Default GitHub orgs whose members may INVOKE strandly via an @mention (the org-membership invoke
# gate, in addition to the static STRANDLY_MENTION_ALLOWED_AUTHORS list). Overridable per deployment
# via STRANDLY_MENTION_ALLOWED_ORGS. NOTE: this is intentionally DISTINCT from STRANDS_ORG_OWNERS —
# these are orgs you can be a *member of* (who may invoke), not owners you may *write to*.
STRANDS_ORGS = ("strands-agents", "strands-labs")
# Ceiling for how many Memory events a fire-and-forget poll reads back. ``MemoryClient.
# list_events`` returns events oldest-first and truncates to ``max_results`` (default 100),
# so the *default* would drop the FINAL assistant message of any run longer than 100 events
# (routine for a minutes-to-hours task). We request a high ceiling so the read reaches the
# tail (the final answer). 10k events is thousands of turns — far above any realistic run.
MEMORY_MAX_EVENTS = 10_000

# --- goal loop (actor-critic) ---
GOAL_MAX_ATTEMPTS = 3
GOAL_DEFAULT = (
    "Fully satisfy the user's most recent request: every part of what they asked for is done and "
    "verified, not merely attempted or described."
)
# How much of the actor's system prompt (its contract + any activated <active_skills>) to show the
# critic, in characters. Generous: the active skills' instructions are exactly what we want it to
# grade against.
CRITIC_SYSTEM_PROMPT_BUDGET = 8_000

# --- provisioning (AgentCore resources created by `strandly provision`) ---
# Short-term memory: how many days conversation events live (API range 3–365).
MEMORY_EVENT_EXPIRY_DAYS = 30
# Code Interpreter network: PUBLIC so the sandbox can git/pip/gh; SANDBOX would block egress.
CODE_INTERPRETER_NETWORK_MODE = "PUBLIC"

# --- sandbox tool bootstrap ---
# The AgentCore Code Interpreter image is a FIXED Amazon Linux 2023 (no custom image is possible —
# CreateCodeInterpreter exposes no container/ECR field), and it ships WITHOUT git/pytest/etc. and
# runs as a non-root user with no passwordless sudo, so `dnf install` is out. We bootstrap real git
# rootlessly via a static micromamba binary that installs git (+deps) from conda-forge into $HOME on
# the FIRST use of a fresh session. The install dir is under $HOME so it persists for the session's
# life. Idempotent (skipped when git already present) and fail-open (a conda-forge hiccup leaves the
# sandbox usable without git, never crashes the turn). Only fresh sessions pay the ~30-60s cost;
# adopted sessions are already bootstrapped. See memory: agentcore-ci-sandbox-no-git.
SANDBOX_GIT_PREFIX = "$HOME/.gitenv"
# Prepended to PATH on every executeCommand once bootstrapped: each command is a fresh non-login
# shell, so the install dir is NOT on PATH by default (the session FS persists, the shell env does
# not) — and a symlink doesn't help since ~/.local/bin isn't on the default PATH either.
SANDBOX_GIT_BIN = "$HOME/.gitenv/bin"
# Also forced alongside the PATH: the sandbox has no pager (`less`), so a bare `git log`/`diff`/etc.
# fails trying to spawn one. Git is always non-interactive here, so pin the null pager.
SANDBOX_GIT_PAGER_ENV = "GIT_PAGER=cat PAGER=cat"
SANDBOX_MICROMAMBA_URL = "https://micro.mamba.pm/api/micromamba/linux-aarch64/latest"
# conda-forge packages installed into the session on bootstrap (real git, not a shim → push works).
SANDBOX_BOOTSTRAP_PACKAGES = ("git",)
# Commit identity written into the sandbox's global git config at credential bootstrap. Without
# it, `git commit` fails ("Please tell me who you are") on the bare CI image. A generic-but-honest
# bot identity; pushes authenticate as whatever account owns the bootstrapped token regardless.
SANDBOX_GIT_COMMIT_NAME = "strandly"
SANDBOX_GIT_COMMIT_EMAIL = "strandly@users.noreply.github.com"
# Code Interpreter session lifetime, in seconds. The SDK/service default is 900 (15 min), which
# reaps the sandbox mid-run on long build-a-PR tasks (explore → write → test → push) — observed
# killing a real run at ~910s. We pin the service maximum (8h) so a long autonomous run isn't cut
# off by the sandbox; the fire-and-forget async job cap (also 8h) is the real outer bound. NOTE:
# this does NOT make the filesystem persist across separate invokes — a new session still starts
# clean; it only keeps ONE session alive longer. Cross-invoke persistence is a separate concern.
SANDBOX_SESSION_TIMEOUT_SECONDS = 28800
# Long-term memory KB: an S3 Vectors index + a Bedrock KB over Titan embeddings. Titan v2 emits
# 1024-dim float32 vectors; cosine is the recommended metric for text embeddings.
KB_EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
KB_VECTOR_DIMENSION = 1024
KB_VECTOR_DISTANCE_METRIC = "cosine"

# --- run-ledger DynamoDB (dashboard telemetry) ---
# The "recent" GSI index name. Canonical source of truth for a value that's *mirrored* (not
# imported) in three places that can't import this module: the CDK DataStack (separate venv), the
# dashboard Lambda handler (standalone bundle), and the run-ledger writer reads it here. The
# tests/test_infra_constants_sync.py guard asserts all mirrors equal this.
RUN_LEDGER_GSI_NAME = "recent"

# --- mention-log DynamoDB (dashboard "Mentions" tab) ---
# One row per mention the poller processed (dispatched, unauthorized, stale, ...), so the dashboard
# can show every @mention of the agent and whether its author was authorized. Same mirroring rules
# as the run-ledger constants above: the CDK DataStack and the dashboard Lambda copy these values
# (they can't import this module); tests/test_infra_constants_sync.py guards the mirrors.
MENTION_LOG_GSI_NAME = "recent"
MENTION_LOG_GSI_PK_VALUE = "MENTION"

# --- environment detection (local vs Bedrock AgentCore) ---
AGENTCORE_CODE_INTERPRETER_ENV = "AGENTCORE_CODE_INTERPRETER_ID"
AGENTCORE_MEMORY_ENV = "AGENTCORE_MEMORY_ID"
AGENTCORE_RUNTIME_MARKERS = ("BEDROCK_AGENTCORE_RUNTIME", "AGENT_OBSERVABILITY_ENABLED")
KNOWLEDGE_BASE_ENV = "BEDROCK_KB_ID"
