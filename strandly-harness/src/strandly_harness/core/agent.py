"""The agent factory — the one place a Strands ``Agent`` is built.

Opinionated: the model (Opus 4.8), tools, plugins, prompt, and context strategy are fixed. What
varies is configured via ``Config`` (Secrets Manager or ``.env``) and gated on what's present:
the GitHub tool, the web-search MCP, the AgentCore sandbox, and the AgentCore session each turn on
only when their secret is configured — otherwise local fallbacks. A fresh agent is built per
request; the session manager rehydrates conversation state.

Async because built-in skills are pushed into a non-local sandbox before the skills plugin loads.
"""

from __future__ import annotations

from typing import Any

from strandly_harness.core.config import Config
from strandly_harness.core.constants import MODEL_SYSTEM_CACHE_POINT, MODEL_TIER_DEFAULT
from strandly_harness.core.context import RuntimeContext
from strandly_harness.core.model import build_model
from strandly_harness.core.prompt.compose import compose
from strandly_harness.memory.knowledge_base import build_memory_manager
from strandly_harness.memory.offload import build_offloader
from strandly_harness.memory.session import build_session_manager
from strandly_harness.plugins.agentcore_session import AgentCoreSessionPlugin
from strandly_harness.plugins.event_context import EventContext
from strandly_harness.plugins.github_threads.plugin import GitHubContextInjector
from strandly_harness.plugins.goal import build_goal_loop
from strandly_harness.sandbox.select import build_sandbox
from strandly_harness.tools.toolset import build_tools


async def build_agent(
    config: Config,
    ctx: RuntimeContext,
    *,
    system_prompt: str = "",
    hitl: bool = False,
    model: Any | None = None,
    model_tier: str = MODEL_TIER_DEFAULT,
    sandbox: Any | None = None,
    spawn_depth: int = 0,
) -> Any:
    """Build the Strandly agent.

    Args:
        config: Resolved deployment config (creds + gated capabilities).
        ctx: Per-invocation runtime context (cwd, session id, event).
        system_prompt: Optional layer on top of the global prompt (subagents pass their own).
            The global prompt is always prepended via ``compose``.
        hitl: Human-in-the-loop — approve/interrupt every tool (CLI).
        model: Optional pre-built model (tests inject a fake). Takes precedence over
            ``model_tier`` when set.
        model_tier: Which ``MODEL_TIERS`` entry to build ("default" / "fast" / "advanced").
            The top agent always uses the default; ``spawn`` passes a subagent's chosen tier.
        sandbox: Optional pre-built sandbox to reuse instead of building a fresh one. ``spawn``
            passes the *parent's* sandbox so a subagent shares its working directory, files, and —
            critically — its AgentCore Code Interpreter **session**. Building a fresh sandbox per
            subagent starts a new CI session, and the service's per-instance session cap means that
            new session evicts the parent's (its next tool call then fails with "session ... is not
            active"). Sharing the one session both honors ``spawn``'s "shares the sandbox" contract
            and lets a subagent see files the parent wrote (e.g. a cloned repo). ``None`` (the top
            agent) builds one.
        spawn_depth: Internal — subagent recursion depth (subagents pass depth+1).
            Also gates the goal loop: it is attached only at depth 0 (the top agent),
            never on spawned subagents (issue #357).
    """
    from strands import Agent

    sandbox = sandbox if sandbox is not None else build_sandbox(config)
    resolved_model = model if model is not None else build_model(config, tier=model_tier)
    tools = build_tools(config, ctx, sandbox, spawn_depth=spawn_depth)

    # Plugins: skills (sandbox-aware), event-context injection, goal loop, todo, offloader, and —
    # on AgentCore — the warm-session-persistence plugin.
    from strandly_harness.skills.loader import build_skills_plugin
    from strandly_harness.tools.todo import TodoPlugin

    # The goal loop (actor-critic) runs ONLY at the top level (spawn_depth == 0). Spawned subagents
    # deliberately do NOT get it: the top agent always carries the goal loop and owns
    # convergence/quality on a subagent's output, so a second loop inside the subagent is
    # redundant. It is also *invisible* to the orchestrator — `spawn` returns only the subagent's
    # final text (`str(result)`), never its loop attempts or stop_reason — so an in-subagent loop
    # would add latency/cost (extra critic passes) without surfacing any pass/fail signal the
    # orchestrator could act on. Keep convergence at the highest level. (issue #357)
    plugins: list[Any] = [
        await build_skills_plugin(sandbox),
        EventContext(sandbox),
        *([build_goal_loop()] if spawn_depth == 0 else []),
        TodoPlugin(),
        build_offloader(sandbox),
    ]
    # GitHub URL context injector — auto-enriches issue/PR/discussion URLs (from the invoke payload
    # or the latest user message) into the model's input ephemerally, via two hooks. This is the
    # *only* surface for GitHub-thread enrichment; for any URL the agent wants on demand mid-turn it
    # just uses `use_github`. Registered unconditionally (NOT gated on a token): a token is used
    # when present (full GraphQL enrichment), but public issues/PRs fall back to the anonymous REST
    # API when none is configured — a token is not required to read a public thread (issue #346).
    plugins.append(GitHubContextInjector(config.github, event=ctx.event))
    # Session persistence ONLY at the top level (spawn_depth == 0), like the session manager and
    # goal loop below. The plugin restores/records the sandbox's Code Interpreter session across
    # invocations and drives warm-up/adoption — the *top* agent owns that lifecycle. A subagent now
    # SHARES the parent's sandbox (see build_agent's `sandbox` arg), so attaching this to a subagent
    # would have it call warm_up/adopt on the parent's live session (a no-op at best, meddling with
    # the parent's session state at worst). Keep it on the owner only.
    if config.use_agentcore_sandbox and spawn_depth == 0:
        plugins.append(AgentCoreSessionPlugin())

    interventions = _interventions(hitl)
    # Session manager ONLY at the top level (spawn_depth == 0), like the goal loop above. A spawned
    # subagent is ephemeral and isolated — `spawn` runs it and keeps only its final text — so it must
    # NOT persist to a session. Critically, subagents are built with a context-less RuntimeContext
    # (session_id/session_key None), which `_session_id` collapses to the literal "session"; with a
    # session manager attached, every concurrent subagent would bind the SAME AgentCore Memory
    # session (memory_id, "session", actor_id), interleave their tool-use/tool-result blocks into one
    # shared history, and the session manager's orphan-repair would emit more toolResults than
    # toolUses — which ConverseStream rejects ("toolResult blocks … exceed toolUse blocks"). Keeping
    # subagent sessions in-process (None) avoids the shared-session corruption entirely.
    session_manager = build_session_manager(config, ctx) if spawn_depth == 0 else None
    # Long-term memory (search_memory/add_memory + per-turn recall injection) when a writable KB is
    # configured, else None — a fresh agent per request, like everything else here.
    memory_manager = build_memory_manager(config, ctx)

    trace_attributes: dict[str, Any] = {"service.name": "strandly-harness"}
    if ctx.session_id:
        trace_attributes["session.id"] = ctx.session_id

    agent = Agent(
        model=resolved_model,
        system_prompt=compose(system_prompt),
        # We own rendering via events.translate + the surface renderer; silence the SDK's
        # default stdout callback handler so streamed text isn't printed twice.
        callback_handler=None,
        tools=tools,
        plugins=[p for p in plugins if p is not None],
        interventions=interventions or None,
        # context_manager="agentic": the model drives its own context management via injected
        # tools (summarize/truncate/pin) with live token-usage feedback; the
        # SummarizingConversationManager (summary_ratio=0.3) is only a reactive overflow safety net
        # — no proactive compression. We already include our own sandbox-routed ContextOffloader in
        # `plugins`, so the agentic path sees one present and won't append its in-memory one.
        context_manager="agentic",
        session_manager=session_manager,
        memory_manager=memory_manager,
        sandbox=sandbox,
        trace_attributes=trace_attributes,
    )

    # Recompose the system prompt now that the agent exists: plugins (skill/todo) and the memory
    # manager (search_memory/add_memory) register their tools during construction, so agent.
    # tool_names is the authoritative set. The capabilities section is built from it, so the prompt
    # describes exactly the tools this agent has — no "if you have X", no tool it lacks.
    #
    # Set as content blocks with a trailing cache point so the stable base prompt is cached: the
    # SystemPromptSkills plugin appends its volatile <active_skills> block after this each turn (its
    # reinject only ever removes/re-appends that one text block), so the cache point stays between
    # the stable base and the churning skills block.
    agent.system_prompt = [
        {
            "text": compose(
                system_prompt,
                tool_names=agent.tool_names,
                # The AgentCore sandbox's filesystem doesn't survive across separate invocations;
                # the local sandbox is the user's real disk (persists). Tell the agent to persist
                # work out of an ephemeral sandbox only in the former case.
                ephemeral_sandbox=config.use_agentcore_sandbox,
            )
        },
        MODEL_SYSTEM_CACHE_POINT,
    ]
    return agent


def _interventions(hitl: bool) -> list[Any]:
    """Human-in-the-loop approval before each tool, or none (unattended)."""
    if not hitl:
        return []
    from strands.vended_interventions.hitl import HumanInTheLoop

    return [HumanInTheLoop(ask="stdio")]
