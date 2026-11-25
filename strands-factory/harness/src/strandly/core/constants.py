"""Fixed choices for the harness — opinionated, not configurable.

Everything here is a deliberate decision, not a knob. What varies by deployment lives in
``core/config.py``; what varies per request (session id, cwd) is an argument to ``build_agent``.
"""

from __future__ import annotations

# --- model: harness model strings ("provider/name"); the harness and the SDK's model info supply
# thinking, caching, output and context limits ---
#: The top agent runs the harness default model (``strands_harness.defaults.DEFAULT_MODEL``,
#: Claude Opus 5.5 on Anthropic's Bedrock client) unless STRANDLY_MODEL names another
#: (config.model), at this effort unless STRANDLY_EFFORT names another (config.effort).
EFFORT_DEFAULT = "medium"
#: Subagent tiers (the ``subagent`` tool's ``model`` argument; skills name them): a model string
#: and effort, or ``None`` for both to inherit the top agent's. Fable models need the account's
#: Bedrock retention setting to be ``provider_data_share``.
MODEL_TIER_DEFAULT = "default"
MODEL_TIERS: dict[str, tuple[str | None, str | None]] = {
    "default": (None, None),
    "fast": ("bedrock/global.anthropic.claude-haiku-5-5", "off"),
    "advanced": ("bedrock/global.anthropic.claude-fable-5-2", "max"),
}
#: The web_fetch summarizer (the harness default for Bedrock is an older Haiku).
WEB_FETCH_MODEL = "bedrock/global.anthropic.claude-haiku-5-5"
#: A chat's ``model`` / ``effort`` payload keys and length limits. The harness model resolution is
#: the only judge of a choice; these limits only keep oversize text out of logs and the ledger.
CHAT_MODEL_KEY = "model"
CHAT_EFFORT_KEY = "effort"
CHAT_MODEL_MAX_CHARS = 256
CHAT_EFFORT_MAX_CHARS = 32

#: botocore retry budget for the session store's S3 client ("adaptive" mode).
SESSION_STORE_MAX_ATTEMPTS = 10

# Model-call retry (core/retries.py): a model call that fails on throttling or a transient error,
# including a stream dropped mid-response, is re-issued with the history intact. Total attempts per
# call, and the backoff (8, 16, 32, 64, 120 s).
MODEL_RETRY_MAX_ATTEMPTS = 6
MODEL_RETRY_INITIAL_DELAY_SECONDS = 8
MODEL_RETRY_MAX_DELAY_SECONDS = 120

# The longest one agent run can live: the agent runtime's session maxLifetime (8 h, the AgentCore
# microVM maximum; it counts from the session's start, not the run's). Mirrors
# infra/stacks/common.py (tests/test_infra_constants_sync.py). The stuck-run reaper closes a
# `running` ledger row only once it is older than this, because until then the process that writes
# its terminal row may be alive.
AGENT_MAX_LIFETIME_SECONDS = 28_800

# --- the `subagent` tool ---
# Delegation depth (the harness's `max_depth`): the top agent may delegate, its subagents may not.
SUBAGENT_MAX_DEPTH = 1
# The action tools a `subagent` call's `tools` list — or a role's `allowed-tools:` frontmatter — may
# restrict a subagent to. Naming a subset is a real capability gate, not a request: `build_agent`
# filters the harness's builtins to it and `toolset.build_tools` skips anything not named, so a role
# scoped without `write`/`edit` has no file-mutation tools and one scoped without `use_github` has no
# GitHub API at all. It is a capability cut, NOT a jail — `shell`, where a scope keeps it, is still a
# shell. Always present regardless of scope: the read-only lookup providers (docs / web-search MCP)
# and the plugin-delivered tools (`skills`, `search_memory`).
# The vocabulary is the same as a SKILL.md's `allowed-tools:` field, so skills and role prompts
# describe scope identically.
SUBAGENT_SCOPABLE_TOOLS = frozenset(
    {
        "shell",
        "read",
        "write",
        "edit",
        "web_fetch",
        "programmatic_tool_caller",
        "use_github",
        "use_slack",
        "subagent",
    }
)
# Names a scope may legally CONTAIN but that the harness does not gate: they come from plugins /
# the memory manager, which are wired independently of the tool list. Accepted (not an error) so
# that copying a SKILL.md's `allowed-tools:` line into a role doesn't break the call; they have no
# effect. (`subagent` is scopable above but inert for a subagent: it is withheld beyond depth 0.)
# Retired names, still accepted so an older role or skill that lists them keeps working; they grant
# nothing: `think`, `todo_write`, and the memory write tools `add_memory` / `add_personal_memory`
# (long-term memory is written only by extraction).
SUBAGENT_UNGATED_TOOLS = frozenset(
    {"skills", "todo_write", "search_memory", "think", "add_memory", "add_personal_memory"}
)
# The harness AgentSkills plugin's tool: loads one skill's full instructions as its result.
SKILLS_TOOL_NAME = "skills"
# The SDK "auto" context-manager pipeline, rebuilt in core/agent.py so `skills` results are exempt
# from the truncation: tool results over the threshold become a preview, summarization at the
# utilization.
CONTEXT_TRUNCATE_THRESHOLD_TOKENS = 1_500
CONTEXT_TRUNCATE_PREVIEW_TOKENS = 750
CONTEXT_SUMMARIZE_UTILIZATION = "85%"
# A user message's text block over this many tokens (an oversized paste: a log, a dump) is cut to
# a head-and-tail preview and kept whole in the stash for `retrieve_context`. 50k tokens is about
# 200 KB of text: far above any typed ask or a normal pasted file, 5% of the top model's 1M window,
# so only inputs that would dominate every later request (and overflow a subagent tier's smaller
# window) are offloaded. Override with STRANDLY_USER_TEXT_OFFLOAD_TOKENS.
CONTEXT_USER_TEXT_THRESHOLD_TOKENS = 50_000
CONTEXT_USER_TEXT_PREVIEW_TOKENS = 4_000

# --- background tools (the SDK's Background Tasks, via the harness's `background_tasks` policy) ---
# Which tools the model may dispatch to the background (`_background_execution: true` on the call).
# Fixed, not configurable: `shell` (test suites, builds, clones, waiting on CI). The harness runs
# every `subagent` call in the background regardless, so it is not listed here. Everything else is
# fast enough that detaching it only adds a delivery round trip, and the framework's own tools
# (`strands_manage_background_task`, `skills`) must never detach — a task that manages
# tasks is a loop waiting to happen. Passed as the policy's `agentic` list, so nothing else even
# carries the flag in its schema.
BACKGROUND_TOOL_NAMES = frozenset({"shell"})
# Physically executing at once (`max_concurrency`). Beyond this, admitted tasks queue. 4 is the SDK
# default too, and well under the sandbox runtime's concurrency comfort.
BACKGROUND_MAX_CONCURRENCY = 4
# Per-task wall clock (`timeout`; the SDK default is unbounded). A background `subagent` legitimately
# runs for many minutes (long builds, deep reviews on long-running sessions); 2 h is generous for
# that and still bounded, because the SDK waits for live
# tasks at the end of the turn (`wait_for_completion`) — an unbounded task would mean an unbounded
# turn.
BACKGROUND_TASK_TIMEOUT_SECONDS = 7_200

# --- use_github write allow-list ---
# Hardcoded default owner allow-list for the `use_github` tool's repository-scope guardrail
# (config.py wires this into GitHubSettings.allowed_owners / internal_owners). Strandly may only
# write to repos under these orgs unless STRANDLY_ALLOWED_OWNERS overrides it. Deliberately scoped
# to the Strands orgs — NOT example-maintainer/* or example-user/*.
STRANDS_ORG_OWNERS: tuple[str, ...] = ()

# --- context management ---
# Context management is context_manager="auto" (set in agent.py): the SDK preset — tool results
# over 1,500 tokens eagerly cut to a 750-token preview (original behind `retrieve_context`), then
# summarization at 85% utilization.

# --- session (short-term memory: SnapshotSessionManager over a Storage — memory/session.py) ---
# Local / dev store root when no session bucket is configured (CLI, local dev, tests). Deliberately
# NOT an implicit S3 pick when credentials exist: a local `chat --session-id gh-…` must never write
# over a deployed session. Yields ./.strandly/session/… and ./.strandly/context/….
LOCAL_SESSION_STORE_DIR = "./.strandly"
# Storage key layout, relative to the store root (the session bucket's root when deployed): the
# SDK's own (SnapshotSessionManager namespaces under "session", the context manager's stash under
# "context"). The verbatim transcript is AgentCore Memory events, not objects here. Mirrored by
# the dashboard read Lambda (dashboard/api/handler.py — guarded by tests/test_infra_constants_sync)
# and by the bucket's lifecycle rules (infra DataStack).
SESSION_STORE_SESSION_PREFIX = "session"
SESSION_STORE_CONTEXT_PREFIX = "context"
# The SDK's default agent id — Strandly never sets one, so every session's single agent lives here.
SESSION_STORE_AGENT_ID = "default"
SESSION_STORE_LATEST_SNAPSHOT = "snapshot_latest.json"
# Upper bound on transcript events a reader loads for one session (ListEvents pages); readers keep
# the NEWEST this many and flag the rest truncated. One event is one message (median session ~240).
TRANSCRIPT_MAX_MESSAGES = 2_000
# AgentCore Runtime session ids (instance affinity) must be slash-free and 33–256 chars. The
# Memory session id (what we read back) only needs to be slash-free — these are two different
# ids that happen to derive from the same user-supplied value (see memory.runtime_session_id).
RUNTIME_SESSION_ID_MIN_LEN = 33

# Default GitHub orgs whose members may INVOKE strandly via an @mention (the org-membership invoke
# gate, in addition to the static STRANDLY_MENTION_ALLOWED_AUTHORS list). Overridable per deployment
# via STRANDLY_MENTION_ALLOWED_ORGS. NOTE: this is intentionally DISTINCT from STRANDS_ORG_OWNERS —
# these are orgs you can be a *member of* (who may invoke), not owners you may *write to*.
STRANDS_ORGS: tuple[str, ...] = ()

# --- goal loop (actor-critic) ---
GOAL_MAX_ATTEMPTS = 3
GOAL_DEFAULT = (
    "Fully satisfy the user's most recent request: every part of what they asked for is done and "
    "verified, not merely attempted or described."
)
# How much of the actor's system prompt (its contract + the <available_skills> listing) to show the
# critic, in characters.
CRITIC_SYSTEM_PROMPT_BUDGET = 8_000
# The critic splits this budget evenly across the engaged skills' GOALS.md. Every GOALS.md must fit
# its share when this many skills with goals are engaged at once (enforced by tests).
GOALS_MAX_ENGAGED_SKILLS = 3

# --- provisioning (AgentCore resources created by `strandly provision`) ---
# AgentCore Memory: how many days transcript events live (API range 3–365). Long-term records
# extracted from them do not expire with the events.
MEMORY_EVENT_EXPIRY_DAYS = 90
# Memory actors (memory/scopes.py): every team run writes under one actor; a Slack DM under the
# person's own (``slack-user-<UID>``, the same name the migrated knowledge-base records use); a
# portal/CLI/ACP run of a known person under their Slack actor, else under ``person-<person_id>``.
MEMORY_TEAM_ACTOR = "team"
MEMORY_PERSONAL_ACTOR_PREFIX = "slack-user-"
MEMORY_PERSON_ACTOR_PREFIX = "person-"
# Long-term memory strategies on the Memory resource (infra BackendStack) and the namespaces they
# extract into. ``{actorId}`` is AgentCore's placeholder; each recall store resolves it.
MEMORY_FACTS_STRATEGY = "facts"
MEMORY_FACTS_NAMESPACE_TEMPLATE = "/facts/{actorId}/"
MEMORY_PREFERENCES_STRATEGY = "preferences"
MEMORY_PREFERENCES_NAMESPACE_TEMPLATE = "/preferences/{actorId}/"
# Transcript events (memory/transcript.py): one CreateEvent per message. A payload item holds at
# most 100,000 characters (the API limit); a message longer than TRANSCRIPT_EVENT_MAX_ITEMS items
# is cut with an explicit marker. The dashboard clips tool output far below this, so ~1M
# characters per message keeps everything a reader shows while keeping each event small.
TRANSCRIPT_ITEM_MAX_CHARS = 100_000
TRANSCRIPT_EVENT_MAX_ITEMS = 10
# Strings longer than this are clipped (with a marker) when a JSON message would not fit at all.
TRANSCRIPT_STRING_CLIP_CHARS = 20_000
# CreateEvent rejects a payload over 102,400 bytes of JSON. An event whose payload serializes to
# more than TRANSCRIPT_EVENT_OFFLOAD_BYTES (the limit less ~12% for metadata and encoding
# differences) is stored whole at ``context/<sid>/transcript/<seq>.json`` in the session store and
# recorded as a preview whose strings are clipped to TRANSCRIPT_PREVIEW_CLIP_CHARS, with metadata
# ``offloaded`` = the full payload's size. The dashboard reads the full payload back.
TRANSCRIPT_EVENT_MAX_BYTES = 102_400
TRANSCRIPT_EVENT_OFFLOAD_BYTES = 90_000
TRANSCRIPT_PREVIEW_CLIP_CHARS = 2_000
# Bounded retry of one event write: attempts, and the wall-clock budget across them. With the
# client's own timeouts (connect 1 s, read 3 s, no SDK retries) one message never holds the agent
# loop for much more than the budget plus one call.
TRANSCRIPT_WRITE_ATTEMPTS = 3
TRANSCRIPT_WRITE_BUDGET_SECONDS = 4.0
# The tiny object, next to the snapshot, that tells a reader which actor a session's transcript
# events are under: ``session/<sid>/memory-actor.json`` → {"actor": …, "sessionId": …}.
SESSION_STORE_ACTOR_MARKER = "memory-actor.json"
# --- sandbox: a SECOND AgentCore Runtime, driven by InvokeAgentRuntimeCommand ---
# The agent's `shell`/`read`/`write`/`edit` tools execute inside a *separate* AgentCore Runtime (the
# "sandbox runtime"), not the runtime hosting the agent. Two reasons, in order of importance:
#   1. BLAST RADIUS. The sandbox runtime has its own execution role with (deliberately) almost no
#      permissions — no Bedrock, no Secrets Manager, no Memory, no KB, no DynamoDB. Everything the
#      agent *runs* happens there; everything the agent *is trusted for* (model calls, the config
#      secret, GitHub writes via `use_github`) stays in the agent runtime. A prompt injection that
#      reaches a shell now reaches a box that can do nothing.
#   2. IT IS A REAL BOX. A custom container image (git, ripgrep, uv, node — see Dockerfile.sandbox)
#      on AgentCore Runtime **instances**: one EC2 instance per session, with a persistent
#      per-session EBS volume (40 GiB) mounted at SANDBOX_MOUNT_PATH that survives idle stop/resume
#      and is deleted only by DeleteCapacityProviderSession (the sliding session TTL below). So no
#      per-session tool bootstrap, and a repo cloned on one invocation is still there on the next
#      invocation of the same session. docs/operations.md.
# The mount path must be under /mnt with exactly one level (a service constraint on the volume's
# `mountPath`), and it is also the default cwd of every command we send.
SANDBOX_MOUNT_PATH = "/mnt/workspace"
SANDBOX_WORKDIR = SANDBOX_MOUNT_PATH
# Session-id namespace for the sandbox. The sandbox session is keyed on the harness session so the
# filesystem follows a conversation across invocations, but it is a DIFFERENT id space from the
# agent runtime's own affinity id — prefixed so the two can never collide.
SANDBOX_SESSION_PREFIX = "sandbox"
# Where the files that built-in skills ship (scripts, references, role prompts) are pushed inside
# the sandbox, at its first use (skills/loader.py).
SANDBOX_SKILLS_DIR = f"{SANDBOX_MOUNT_PATH}/.strandly/skills"
# `InvokeAgentRuntimeCommand` service limits, mirrored here so we clamp instead of eating an opaque
# ValidationException: the command body is 1 byte–64 KB and `timeout` is 1–3600 seconds.
SANDBOX_COMMAND_MAX_BYTES = 65_536
SANDBOX_COMMAND_TIMEOUT_MIN = 1
SANDBOX_COMMAND_TIMEOUT_MAX = 3_600
# Socket read timeout for the sandbox's `bedrock-agentcore` client. `InvokeAgentRuntimeCommand` streams
# a command's output, and a command that prints nothing (`sleep`, a quiet build, a long `pytest -q`)
# leaves the socket silent for as long as it runs. botocore's default read timeout is 60s, which killed
# every such command at ~60s with "Read timed out" no matter what `timeout` the caller asked for. The
# service enforces the command budget itself (at most SANDBOX_COMMAND_TIMEOUT_MAX), so the client must
# out-wait the longest budget it can send, plus headroom for the service to deliver the timeout result.
SANDBOX_CLIENT_READ_TIMEOUT_SECONDS = SANDBOX_COMMAND_TIMEOUT_MAX + 60
SANDBOX_CLIENT_CONNECT_TIMEOUT_SECONDS = 10
# A pooled connection idle longer than this is closed instead of reused. A request written onto a
# connection that the path dropped while it sat idle dies at once (`ConnectionClosedError` in ~10 ms,
# never seen by the service), and it cannot be told apart from a command the service received, so it
# fails as "completion unknown". The pool is LIFO: a burst (a subagent starting) takes connections
# idle since the previous burst, minutes ago. The bound is well under the usual idle timeouts (NAT
# 350 s, load balancers 60 s and up); a new connection costs one TLS handshake.
SANDBOX_CLIENT_MAX_IDLE_SECONDS = 20.0
# TCP keepalive timing for the same sockets. botocore's `tcp_keepalive` only sets SO_KEEPALIVE, whose
# Linux default first probe is after 2 hours, too late to keep a path's idle state alive.
SANDBOX_CLIENT_KEEPALIVE_IDLE_SECONDS = 10
SANDBOX_CLIENT_KEEPALIVE_INTERVAL_SECONDS = 5
SANDBOX_CLIENT_KEEPALIVE_PROBES = 3
# Timeout applied when a caller passes none. Deliberately generous (a `pytest` run or a cold `uv
# sync` routinely passes 5 minutes; a cold Rust/clippy build can pass 15) but NOT the 3600 ceiling: an unbounded default is how a wedged
# command silently burns an hour of a turn.
SANDBOX_COMMAND_TIMEOUT_DEFAULT = 1_800
# Client-side grace on top of the service-enforced timeout. The service returns TIMED_OUT by
# itself; this only guards the case where the HTTP/2 stream never completes at all, so it must be
# comfortably LONGER than the service budget or it would pre-empt the service's own (better) answer.
SANDBOX_TRANSPORT_GRACE_SECONDS = 90.0
# `write_file` chunk size, in base64 characters. The whole write is one shell command, so a naive
# single-shot write of a >48 KB file would blow SANDBOX_COMMAND_MAX_BYTES; we append in chunks
# instead. Sized well under the limit to leave room for the shell wrapper + quoting.
SANDBOX_WRITE_CHUNK_CHARS = 40_000
# Bounded retry for the two documented-transient failures of the command API:
# RetryableConflictException (HTTP 409 — "Session operation in progress", i.e. the session is being
# provisioned or torn down) and ThrottlingException (25 TPS). Short backoff, since a provisioning
# session becomes ready in seconds.
SANDBOX_RETRY_MAX_ATTEMPTS = 5
SANDBOX_RETRY_BACKOFF_BASE_SECONDS = 1.0
SANDBOX_RETRY_BACKOFF_MAX_SECONDS = 15.0
# Session lifecycle for the sandbox runtime (`lifecycleConfiguration`). Idle timeout is raised well
# above the 900s default because a long autonomous turn can legitimately go minutes between shell
# calls (a model call at effort=high plus a big GitHub read), and the box must not go away
# mid-turn. maxLifetime is the Runtime-instances maximum (14 days).
# Neither loses the filesystem: an idle-stopped or expired session keeps its EBS volume, and the
# next invoke of the same session id re-attaches it (only the sliding TTL below deletes it).
SANDBOX_IDLE_TIMEOUT_SECONDS = 3_600
SANDBOX_MAX_LIFETIME_SECONDS = 1_209_600
# Pinned on every command: the image has no pager, so a bare `git log`/`diff` would fail trying to
# spawn one. Git is always non-interactive here.
SANDBOX_GIT_PAGER_ENV = "GIT_PAGER=cat PAGER=cat"
# Commit identity written into the sandbox's git config on session bootstrap. Without it `git
# commit` fails ("Please tell me who you are"). Matches the actual GitHub bot account
# (selected by the operator profile) — a push authenticates as whoever owns the
# bootstrapped token regardless, so this just keeps authorship consistent with that account.
#: Key carrying the sandbox's git credential in the **wake payload** (``InvokeAgentRuntime``), and the
#: key the sandbox answers with. The credential travels this way rather than inside a shell command
#: because AgentCore Runtime writes every executed command to the sandbox's CloudWatch log group —
#: "AgentCore Runtime sends the request ID and the input command to your agent's CloudWatch Logs log
#: group" — which would put the token in plaintext logs. Invoke *payloads* are not logged by the
#: service (and CloudTrail logs no payloads at all), and ``BedrockAgentCoreApp`` does not log request
#: bodies. Mirrored in ``sandbox_main.py``; guarded by ``tests/test_infra_constants_sync.py``.
SANDBOX_GIT_CREDENTIAL_KEY = "gitCredential"
SANDBOX_GIT_CREDENTIAL_STATUS_KEY = "gitCredentialStatus"
SANDBOX_GIT_CREDENTIAL_WRITTEN = "written"

#: Printed by the git bootstrap when a credential was expected but is not on disk — the one failure
#: the wake response cannot see, because it would mean the command shell's ``$HOME`` differs from the
#: entrypoint process's. Carries no secret, so it is safe in the (logged) command and in our logs.
SANDBOX_GIT_CREDENTIAL_MISSING_MARKER = "strandly-sandbox: git credential missing"

SANDBOX_GIT_COMMIT_NAME = "Strandly"
SANDBOX_GIT_COMMIT_EMAIL = "strandly@localhost"

# ---- sandbox reliability --------------------------------------
# Every command runs with a heartbeat that writes one SANDBOX_HEARTBEAT_MARKER byte to
# stderr every SANDBOX_HEARTBEAT_INTERVAL_SECONDS. A command that prints nothing for minutes
# (`sleep 500`, a quiet test run) otherwise leaves the output stream idle, and production saw those
# streams cut with no completion event. The marker is stripped before any output reaches a caller.
SANDBOX_HEARTBEAT_INTERVAL_SECONDS = 30
SANDBOX_HEARTBEAT_MARKER = "\x06"
# Package caches and build output live ON the workspace volume, under SANDBOX_CACHE_ROOT: the
# volume is a real per-session EBS disk that survives idle stop/resume, while /tmp (the instance's
# root volume) is wiped whenever the instance stops. So caches stay warm across invocations of a
# session. Everything under SANDBOX_CACHE_ROOT is regenerable: the workspace prune deletes it whole.
SANDBOX_CACHE_ROOT = f"{SANDBOX_MOUNT_PATH}/.cache"
SANDBOX_CACHE_ENV: dict[str, str] = {
    "XDG_CACHE_HOME": SANDBOX_CACHE_ROOT,
    "PIP_CACHE_DIR": f"{SANDBOX_CACHE_ROOT}/pip",
    "UV_CACHE_DIR": f"{SANDBOX_CACHE_ROOT}/uv",
    "npm_config_cache": f"{SANDBOX_CACHE_ROOT}/npm",
    "npm_config_store_dir": f"{SANDBOX_CACHE_ROOT}/pnpm-store",
    "YARN_CACHE_FOLDER": f"{SANDBOX_CACHE_ROOT}/yarn",
    "CARGO_TARGET_DIR": f"{SANDBOX_CACHE_ROOT}/cargo-target",
    "GOCACHE": f"{SANDBOX_CACHE_ROOT}/go-build",
    "GOMODCACHE": f"{SANDBOX_CACHE_ROOT}/go-mod",
    # Go otherwise writes the module cache read-only, which a plain `rm -rf` (the prune) cannot
    # delete; -modcacherw keeps it owner-writable.
    "GOFLAGS": "-modcacherw",
    "MYPY_CACHE_DIR": f"{SANDBOX_CACHE_ROOT}/mypy",
    "RUFF_CACHE_DIR": f"{SANDBOX_CACHE_ROOT}/ruff",
    "PYTHONPYCACHEPREFIX": f"{SANDBOX_CACHE_ROOT}/pycache",
}
# --- the sandbox sessions' sliding TTL (sandbox/ttl.py; docs/operations.md, "The sandbox") ---
# AgentCore Runtime instances sessions have no TTL: MaxLifetime (14 days) only STOPS a session and
# keeps its volume; only DeleteCapacityProviderSession deletes it. So the harness (re)schedules a
# one-time EventBridge Scheduler schedule that deletes the session this long after its LAST USE: the
# wake and every command (shell, file read/write) slide it, off the event loop. A run that uses its
# sandbox therefore never loses it to the TTL; only a sandbox untouched for this long is deleted.
SANDBOX_SESSION_TTL_SECONDS = 5 * 24 * 3600
# A sessionless (ephemeral) invoke gets a fresh session id that no later run will reuse. Its schedule
# is set ONCE, at the first wake, and not slid: AGENT_MAX_LIFETIME_SECONDS + 1 day from then. The run
# started before that wake and cannot outlive AGENT_MAX_LIFETIME_SECONDS, so the delete can never hit
# a live run (however long it leaves the sandbox idle), and the volume is still bounded to ~32 h.
SANDBOX_EPHEMERAL_SESSION_TTL_SECONDS = AGENT_MAX_LIFETIME_SECONDS + 24 * 3600
# One process re-schedules the same session at most this often after a SUCCESSFUL upsert (every
# command asks; the schedule only needs to move once in a while — 5 days of headroom vs 1 hour).
SANDBOX_TTL_BUMP_INTERVAL_SECONDS = 3600
# After a FAILED upsert, the next command retries once this much time has passed (not an hour later:
# a session with no schedule leaks its volume forever).
SANDBOX_TTL_FAILURE_RETRY_SECONDS = 60
# How many sessions the per-process throttle remembers (LRU). Evicting one costs one extra upsert.
SANDBOX_TTL_THROTTLE_MAX_SESSIONS = 4096
# Schedule name = prefix + sha256(capacity provider id + session id)[:40] (Scheduler: <=64 chars).
SANDBOX_TTL_SCHEDULE_PREFIX = "ttl-"
SANDBOX_TTL_SCHEDULE_HASH_CHARS = 40
# The Scheduler universal target: the bedrock-agentcore data-plane API, called by Scheduler itself
# (no Lambda). Service = the AWS SDK service identifier, action = camelCase API name.
SANDBOX_TTL_TARGET_ARN = "arn:aws:scheduler:::aws-sdk:bedrockagentcore:deleteCapacityProviderSession"
# Target retries (Scheduler: 0-185 attempts; 60-86400 s event age).
SANDBOX_TTL_RETRY_ATTEMPTS = 5
SANDBOX_TTL_RETRY_MAX_AGE_SECONDS = 6 * 3600
# At wake (and after repeated storage failures), prune regenerable directories once the workspace
# volume's `df` usage passes this fraction of its real size. `df` is real on the EBS volume, so no
# slow `du` over a 40 GiB tree.
SANDBOX_WORKSPACE_PRUNE_FRACTION = 0.85
# Directory names that a package manager or build re-creates on demand. Pruned from the mount (and
# only the mount) when it is nearly full or failing writes. Matched by exact name only, and a
# match is still kept when it is tracked by git, holds a `.git`, or is a symlink. A bare `target/`
# is NOT on the list (some repos keep source there); Cargo builds into SANDBOX_CACHE_ROOT via
# CARGO_TARGET_DIR, which the prune empties whole.
# `CACHEDIR.TAG` is deliberately not a signal: a pull request can commit one anywhere.
SANDBOX_WORKSPACE_REGENERABLE_DIRS = (
    "node_modules",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".next",
    ".turbo",
    ".gradle",
)
# Text in a failed command's stderr that means the *storage* failed, not the command.
SANDBOX_STORAGE_ERROR_MARKERS = (
    "No space left on device",
    "Disk quota exceeded",
    "Input/output error",
)
# Consecutive storage-failed commands before the sandbox probes the workspace and recovers
# (prune, then restart; a workspace that still rejects writes makes the sandbox unavailable).
SANDBOX_STORAGE_ERROR_THRESHOLD = 3
# Wake attempts per session id when the service reports a transient failure, including
# "Runtime initialization time exceeded" (a cold start over the 120 s init limit).
SANDBOX_WAKE_MAX_ATTEMPTS = 3
SANDBOX_WAKE_RETRY_DELAY_SECONDS = 15.0
# Wall-clock bound on one wake's retries: no new attempt starts once it (and its delay) would end
# past this. Two 120 s init timeouts fit; a third does not. Exhausting it opens the circuit.
SANDBOX_WAKE_BUDGET_SECONDS = 240.0
# The wake's own InvokeAgentRuntime client (``sandbox_wake_client_config``): an Instances wake (EC2
# boot + EBS attach) answers in 53–83 s, so botocore's 60 s default read timeout cut it off and
# re-sent it. One send per attempt (botocore ``total_max_attempts=1``) — the adapter's wake loop
# above is the only retry layer, so a slow wake is never duplicated underneath it.
SANDBOX_WAKE_READ_TIMEOUT_SECONDS = 300
SANDBOX_WAKE_CONNECT_TIMEOUT_SECONDS = 10
# An unhealthy sandbox session (502 from the runtime) is restarted at most once per this window. While a
# restart has just failed, commands fail fast for SANDBOX_CIRCUIT_OPEN_SECONDS.
SANDBOX_RESTART_COOLDOWN_SECONDS = 600.0
SANDBOX_CIRCUIT_OPEN_SECONDS = 60.0
# After a command platform failure (exit -1, or completion unknown), calls share one harmless health
# probe and otherwise fail fast without sending. A further probe is allowed once this long has passed
# since the last one, for that failure or a later one: a session that is fine again (a transport
# blip, slots freed) recovers, and a broken one costs one read-only probe per window, not per call.
SANDBOX_PLATFORM_PROBE_INTERVAL_SECONDS = 30.0
# `read_file` slices: large single-command base64 reads came back corrupt in production (files over
# ~100 KB). Each slice is 48 KiB of file (64 KiB of base64) with its own sha256.
SANDBOX_READ_CHUNK_BYTES = 49_152
SANDBOX_READ_MAX_ATTEMPTS = 3
SANDBOX_READ_CONCURRENCY = 4
# Largest file `read_file` returns whole (~340 slice commands). Bigger is refused with directions to
# read a range: the slices share the runtime's command rate limit, and the agent holds it in memory.
SANDBOX_READ_MAX_BYTES = 16 * 1024 * 1024

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

# --- work-product DynamoDB (portal review/triage artifacts) ---
# One row per review/triage/first-response artifact the agent produces (see
# ops/work_product.py). Two GSIs, both querying newest-first like the run-ledger's
# ``recent`` GSI, but partitioned instead of constant:
#   by_status  — partition = ``status`` ("draft"/"curated"/"posted"), sort = ``updated_at``. Powers
#                the portal "Today" inbox: Query status="draft" newest-first, no Scan.
#   by_session — partition = ``session_id`` (gh-<owner>-<repo>-<h8>-<kind>-<n>), sort =
#                ``created_at``. Powers per-item history: every artifact for one GitHub item.
# Same mirroring rules as the run-ledger / mention-log constants above: the CDK DataStack and the
# dashboard Lambda copy these values (they can't import this module); tests/test_infra_constants_sync
# guards the mirrors.
WORK_PRODUCT_STATUS_GSI_NAME = "by_status"
WORK_PRODUCT_STATUS_GSI_PK_ATTR = "status"
WORK_PRODUCT_SESSION_GSI_NAME = "by_session"
WORK_PRODUCT_SESSION_GSI_PK_ATTR = "session_id"

# --- people directory DynamoDB (ingress allow-list + cross-surface identity) ---
# One row per person who may trigger the agent (ops/people.py), with whichever identities they have.
# Two SPARSE GSIs, each keyed on one identity attribute, so an ingress resolves its caller with one
# Query: by_github on ``github_login`` (stored lowercase), by_slack on ``slack_user_id``. Same
# mirroring rules as above: the CDK DataStack copies these names (it can't import this module);
# tests/test_infra_constants_sync guards the mirrors. The table is deliberately NOT in the runtime's
# config secret and the runtime exec role has no grant on it — it is read by the ingress Lambdas only.
PEOPLE_GITHUB_GSI_NAME = "by_github"
PEOPLE_SLACK_GSI_NAME = "by_slack"
# Keyed on ``cognito_sub``: the portal user bound to the person (the dashboard API resolves the
# signed-in caller through it).
PEOPLE_COGNITO_GSI_NAME = "by_cognito"

# Runtime payload key the dashboard API sets to ``true`` (a JSON boolean, nothing else counts) on a
# chat launch it received as a direct Lambda invoke, i.e. with no API Gateway or authorizer context,
# whose ``X-Strandly-Person`` named an enabled person. The runtime then takes the person fields
# without a verified ``portalUser`` (``core/context.person_of``). The API builds every runtime
# payload field by field and never copies this key from a request body or header.
DIRECT_INVOKE_KEY = "directInvoke"

# --- environment detection (local vs Bedrock AgentCore) ---
AGENTCORE_SANDBOX_RUNTIME_ENV = "STRANDLY_SANDBOX_RUNTIME_ARN"
AGENTCORE_MEMORY_ENV = "AGENTCORE_MEMORY_ID"
#: Env var the harness sets on **every** runtime it deploys (``serve/deploy.py``), unconditionally, so
#: a process can tell from the inside that it is a deployed AgentCore Runtime rather than a laptop.
#: That distinction is a security control, not a nicety: it is what makes "no sandbox configured"
#: fatal in production and merely loud locally (``sandbox/select.py``).
AGENTCORE_RUNTIME_MARKER_ENV = "BEDROCK_AGENTCORE_RUNTIME"

#: Every signal that means "this process is a deployed AgentCore Runtime". More than one on purpose:
#: ``AGENT_OBSERVABILITY_ENABLED`` (the service's own, set by the toolkit and by our deploy) keeps the
#: guard working for a runtime deployed before the marker above existed, or by the toolkit directly.
AGENTCORE_RUNTIME_MARKERS = (AGENTCORE_RUNTIME_MARKER_ENV, "AGENT_OBSERVABILITY_ENABLED")

#: Strands' OTEL semantic-convention opt-in var. ``strandly deploy`` deliberately does **not** set it:
#: AgentCore Evaluations/Insights parse the *legacy* GenAI message shape and fail every session with
#: ``AgentSpanMappingException: Failed to parse user_query`` under ``gen_ai_latest_experimental``. The
#: legacy conventions already export tool input and output (as ``otel-rt-logs`` records on the
#: runtime log group), so nothing is lost. Pass it via ``--env`` only if you accept losing evals.
OTEL_SEMCONV_OPT_IN_ENV = "OTEL_SEMCONV_STABILITY_OPT_IN"
