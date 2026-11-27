"""Build the Strandly agent: ``strands_harness.create_harness`` plus Strandly's capabilities.

The harness supplies the model (from a model string), prompt caching, the built-in tools, the
environment context, context management and its contract prompt. Strandly adds its prompt as
``instructions``, its own tools (including ``subagent``), the packaged skills, the sandbox, the
session store, memory and plugins. Capabilities turn on only when their secret is configured.
See docs/architecture.md.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from strands_harness import BuiltinToolsConfig, Effort

from strandly.core.config import Config
from strandly.core.constants import (
    BACKGROUND_MAX_CONCURRENCY,
    BACKGROUND_TASK_TIMEOUT_SECONDS,
    BACKGROUND_TOOL_NAMES,
    CONTEXT_SUMMARIZE_UTILIZATION,
    CONTEXT_TRUNCATE_PREVIEW_TOKENS,
    CONTEXT_TRUNCATE_THRESHOLD_TOKENS,
    CONTEXT_USER_TEXT_PREVIEW_TOKENS,
    CONTEXT_USER_TEXT_THRESHOLD_TOKENS,
    SKILLS_TOOL_NAME,
    SUBAGENT_MAX_DEPTH,
    WEB_FETCH_MODEL,
)
from strandly.core.context import RuntimeContext
from strandly.core.prompt.compose import compose
from strandly.core.retries import TransientModelRetry
from strandly.mcp_clients import warn_tool_less_clients
from strandly.memory.long_term import build_memory_manager
from strandly.memory.session import build_session_manager, session_id_for
from strandly.memory.session_store import build_storage
from strandly.memory.stash import ReadableStash
from strandly.memory.transcript import build_transcript_recorder
from strandly.plugins.caller_context import CallerContext
from strandly.plugins.environment import SandboxEnvironment
from strandly.plugins.github_threads.plugin import GitHubContextInjector
from strandly.plugins.goal import build_goal_loop
from strandly.plugins.memory_recall import MemoryRecall
from strandly.plugins.tool_metrics import ToolMetrics
from strandly.sandbox.notices import SandboxNotices
from strandly.sandbox.select import build_sandbox
from strandly.tools.subagent import PARENT_SESSION_TRACE_ATTRIBUTE, load_roles
from strandly.tools.toolset import build_tools

logger = logging.getLogger(__name__)

# The harness's default built-in tools minus: ``web_search`` (no native search on Bedrock; the
# web-search MCP covers it) and ``subagent`` (Strandly's own, which shares the parent's sandbox).
_EXCLUDED_BUILTIN_TOOLS = frozenset({"web_search", "subagent"})
# Tools that plugins and harness options register during construction.
_BACKGROUND_MANAGER_TOOL_NAME = "strands_manage_background_task"
_MEMORY_TOOL_NAME = "search_memory"


async def build_agent(config: Config, ctx: RuntimeContext, **kwargs: Any) -> Any:
    """Build the Strandly agent; see :func:`assemble_agent` for the arguments."""
    return assemble_agent(config, ctx, **kwargs)


def check_model(model: str, effort: str) -> None:
    """Build ``model`` at ``effort`` with the harness model resolution ``create_harness`` uses.

    Building makes no API call and reads credentials and endpoints only from the environment, so a
    serving adapter can refuse a caller's choice before a run starts.

    Raises:
        ValueError: An unknown provider, or an effort the model does not offer.
        TypeError: A malformed model argument.
        ImportError: A provider whose client package is not installed.
    """
    from strands_harness import defaults as harness_defaults
    from strands_harness.models import resolve_model

    resolve_model(model, harness_defaults.DEFAULT_MODEL, cast("Effort", effort))


def assemble_agent(
    config: Config,
    ctx: RuntimeContext,
    *,
    system_prompt: str = "",
    model: Any | None = None,
    effort: str | None = None,
    sandbox: Any | None = None,
    spawn_depth: int = 0,
    allowed_tools: frozenset[str] | None = None,
    mcp_clients: list[Any] | None = None,
) -> Any:
    """Assemble the agent. Synchronous, so the ``subagent`` builder calls it for children.

    Args:
        config: Resolved deployment config (credentials and gated capabilities).
        ctx: Per-invocation runtime context (cwd, session id, event).
        system_prompt: A role layered last on Strandly's prompt (subagents pass theirs).
        model: A harness model string or a ``Model`` instance (tests); ``None`` uses the run's
            caller-chosen model (``ctx.model`` at ``ctx.effort``), else ``config.model`` at
            ``config.effort``.
        effort: Harness reasoning effort for ``model`` (subagent tiers set it); ``None`` is the
            effort that goes with the model chosen above, and ``"auto"`` for any other model.
        sandbox: The parent's sandbox, for a subagent; ``None`` builds one. Building it makes no
            API call: the sandbox session wakes at the first shell or file operation.
        spawn_depth: Subagent depth. The goal loop, session persistence, transcript and background
            tasks run at depth 0 only.
        allowed_tools: Build only these tools (a subagent's scope); ``None`` builds the full set.
        mcp_clients: The parent's started MCP providers, which a subagent reuses.
    """
    from strands.storage import InMemoryStorage
    from strands_harness import create_harness
    from strands_harness import defaults as harness_defaults

    from strandly.extensions import skills_source_dir
    from strandly.skills import loader as skill_loader
    from strandly.skills.goals import SkillGoals, builtin_goals
    from strandly.skills.loader import build_skills_plugin
    from strandly.tools.github import resolve_self_login

    if sandbox is None:
        sandbox = build_sandbox(config, ctx)
    builtin_tools = [
        name
        for name in harness_defaults.DEFAULT_BUILTIN_TOOLS
        if name not in _EXCLUDED_BUILTIN_TOOLS and (allowed_tools is None or name in allowed_tools)
    ]

    github_login = resolve_self_login(config.github_token)
    expected_login = config.get("STRANDLY_GITHUB_LOGIN")
    if expected_login and github_login and expected_login.casefold() != github_login.casefold():
        raise ValueError("Configured GitHub identity does not match the credential's user")
    if expected_login and config.github_token and not github_login:
        raise ValueError("Cannot verify configured GitHub identity against its credential")

    packs = tuple(item.strip() for item in (config.get("STRANDLY_SKILL_PACKS") or "").split(",") if item.strip())
    skill_root = skills_source_dir(Path(skill_loader.__file__).parent, packs)
    tools = build_tools(
        config,
        ctx,
        sandbox,
        spawn_depth=spawn_depth,
        allowed_tools=allowed_tools,
        mcp_clients=mcp_clients,
        roles=load_roles(skill_root) if spawn_depth < SUBAGENT_MAX_DEPTH else None,
    )

    plugins: list[Any] = [
        SandboxEnvironment(),
        SandboxNotices(sandbox, depth=spawn_depth),
        CallerContext(),
        ToolMetrics(),
        GitHubContextInjector(config.github, event=ctx.event),
    ]
    # The top agent owns convergence on a subagent's output, and a subagent returns only its final
    # text, so a goal loop inside a subagent would cost critic passes nobody sees.
    if spawn_depth == 0:
        plugins += [SkillGoals(goals=builtin_goals(skill_root)), build_goal_loop()]

    # Only the top agent of a named session persists. Subagents are built with a session-less
    # context: a shared session would interleave concurrent children's histories into one.
    session_id = session_id_for(ctx) if spawn_depth == 0 else None
    session_storage = build_storage(config) if session_id is not None else None
    session_manager = None
    if session_storage is not None:
        session_manager = build_session_manager(config, ctx, session_storage)
        recorder = build_transcript_recorder(config, ctx, cast(str, session_id), session_storage)
        if recorder is not None:
            plugins.append(recorder)
    memory_manager = build_memory_manager(config, ctx)
    if memory_manager is not None:
        plugins.append(MemoryRecall(memory_manager))

    # A subagent must not detach work: the parent awaits its result, and a task outliving it would
    # report into a conversation that no longer exists.
    background_tasks: Any = False
    if spawn_depth == 0:
        background_tasks = {
            "agentic": sorted(BACKGROUND_TOOL_NAMES),
            "max_concurrency": BACKGROUND_MAX_CONCURRENCY,
            "timeout": BACKGROUND_TASK_TIMEOUT_SECONDS,
        }

    # The prompt describes exactly the tools this agent will have, so name the ones the harness
    # and plugins register during construction too.
    tool_names = {*builtin_tools, SKILLS_TOOL_NAME}
    tool_names.update(t.tool_name for t in tools if hasattr(t, "tool_name"))
    if background_tasks:
        tool_names.add(_BACKGROUND_MANAGER_TOOL_NAME)
    if memory_manager is not None:
        tool_names.add(_MEMORY_TOOL_NAME)
    # The person talks to the top agent; a subagent's caller is that agent.
    person = ctx.person if spawn_depth == 0 else None
    instructions = compose(
        system_prompt,
        tool_names,
        background_tools=BACKGROUND_TOOL_NAMES.intersection(tool_names) if background_tasks else (),
        github_login=github_login,
        display_name=config.get("STRANDLY_DISPLAY_NAME") or "Strandly",
        aws_test_scope=config.aws_test_scope,
        spawn_depth=spawn_depth,
        slack_reply=ctx.slack_channel_id is not None,
        slack_home_channel=None if ctx.is_slack_dm else config.slack_home_channel,
        person=person.introduction() if person is not None else None,
    )

    # A mapping, so web_fetch can carry its summarizer model.
    builtin_tools_config: dict[str, Any] = {"*": False, **dict.fromkeys(builtin_tools, True)}
    if "web_fetch" in builtin_tools:
        builtin_tools_config["web_fetch"] = {"model": WEB_FETCH_MODEL}

    trace_attributes: dict[str, Any] = {"service.name": "strands-factory"}
    if ctx.session_id:
        trace_attributes["session.id"] = ctx.session_id
    if ctx.parent_session_id:
        trace_attributes[PARENT_SESSION_TRACE_ATTRIBUTE] = ctx.parent_session_id

    # create_harness starts every MCP provider synchronously; a build with no "agent built" line
    # is how a wedged provider presents.
    logger.info(
        "building agent: depth=%d session=%s sandbox=%s scoped=%s shared_mcp=%s",
        spawn_depth,
        ctx.session_id or "-",
        type(sandbox).__name__,
        bool(allowed_tools),
        mcp_clients is not None,
    )
    if model is None and ctx.model is not None:
        model, effort = ctx.model, effort if effort is not None else (ctx.effort or "auto")
    elif model is None:
        model, effort = config.model, effort if effort is not None else config.effort
    elif effort is None:
        effort = "auto"
    agent = create_harness(
        model=model,
        effort=cast("Effort", effort),
        instructions=instructions,
        builtin_tools=cast("BuiltinToolsConfig", builtin_tools_config),
        background_tasks=background_tasks,
        # SandboxEnvironment in ``plugins`` is the harness's ``environment``, kept from waking the sandbox.
        builtin_plugins=[],
        skills=build_skills_plugin(sandbox, root=skill_root),
        tools=tools,
        plugins=plugins,
        # The harness default would give every agent, subagents included, a local session.
        session=False,
        session_manager=session_manager,
        # Offloaded tool results go to the session store; without it the SDK keeps them in memory
        # and inlines all of them into every snapshot write. Subagents stash in memory.
        storage=ReadableStash(session_storage if session_storage is not None else InMemoryStorage()),
        context_manager=_context_manager(config.user_text_offload_tokens),
        # Long-term memory is Strandly's AgentCore manager, not the harness's local files.
        memory=False,
        memory_manager=memory_manager,
        sandbox=sandbox,
        # Per agent: the strategy counts attempts on itself.
        retry_strategy=TransientModelRetry(),
        callback_handler=None,
        trace_attributes=trace_attributes,
    )
    model_id, reasoning = _model_settings(agent.model)
    logger.info(
        "agent built: depth=%d tools=%d model=%s effort=%s reasoning=%s",
        spawn_depth,
        len(agent.tool_names),
        model_id,
        effort,
        reasoning,
    )
    if mcp_clients is None and allowed_tools is None:
        # This build started its own MCP clients and keeps every tool, so each should contribute.
        warn_tool_less_clients(tools, agent.tool_names)
    return agent


# Request fields the harness maps ``effort`` onto, per provider.
_REASONING_FIELDS = ("thinking", "output_config", "reasoning", "reasoning_effort", "reasoningConfig")


def _model_settings(model: Any) -> tuple[str, str]:
    """The model id and the reasoning request fields (compact JSON, ``-`` when none), for logs."""
    try:
        config = model.get_config()
    except Exception:  # noqa: BLE001 - a log label must never fail the build
        return "-", "-"
    if not isinstance(config, dict):
        return "-", "-"
    # BedrockModel carries them in additional_request_fields, AnthropicModel in params.
    fields = config.get("additional_request_fields") or config.get("params")
    fields = fields if isinstance(fields, dict) else {}
    reasoning = {key: fields[key] for key in _REASONING_FIELDS if key in fields}
    label = json.dumps(reasoning, separators=(",", ":"), default=str) if reasoning else "-"
    return str(config.get("model_id") or "-"), label


def _context_manager(user_text_threshold: int = CONTEXT_USER_TEXT_THRESHOLD_TOKENS) -> Any:
    """The SDK's ``"auto"`` pipeline, except ``skills`` results are never truncated.

    Tool results over the threshold are cut to a preview as they land, as is a user text block over
    ``user_text_threshold``, then the conversation is summarized at high utilization. A
    name-filtered target also covers error results, which the preset's ``"tool_results"`` does not.
    """
    from strands.experimental.context_manager import ContextManager, Offload

    return ContextManager(
        strategies=[
            Offload.truncate(
                [f"!tool::{SKILLS_TOOL_NAME}"], {"preview_tokens": CONTEXT_TRUNCATE_PREVIEW_TOKENS}
            ).when(block_threshold=CONTEXT_TRUNCATE_THRESHOLD_TOKENS),
            Offload.truncate(
                "user_text", {"preview_tokens": CONTEXT_USER_TEXT_PREVIEW_TOKENS}
            ).when(block_threshold=user_text_threshold),
            Offload.summarize("*").when(
                context_utilization=CONTEXT_SUMMARIZE_UTILIZATION, preserve_recent=4
            ),
        ]
    )
