"""Harness-native subagent spawning.

A subagent is just another agent built through ``build_agent`` from the *same* ``Config`` with a
``system_prompt`` layer applied on top of the global prompt. It therefore inherits the harness's
sandbox, approval gates, context management, and skills — and, crucially, the **global prompt**
(``build_agent`` always prepends it via ``compose``) — unlike the raw ``strands_tools``
``use_agent``, which builds a bare agent that bypasses all of that.

``make_spawn(config, ctx, depth)`` returns a ``spawn`` tool bound to the parent's config and a
recursion depth. The tool:
  1. resolves ``system_prompt`` to the subagent's prompt text — a **file path** (resolved against
     ``ctx.cwd``, e.g. a skill's system-prompt file) or literal text;
  2. resolves ``model`` to one of the fixed tiers in ``constants.MODEL_TIERS`` ("default" =
     Opus 4.8, "fast" = Haiku 4.5, "advanced" = Fable 5) — a configured Claude-family subset,
     never a free-form model id — and builds the subagent via
     ``build_agent(config, sub_ctx, system_prompt=..., model_tier=..., spawn_depth+1)``
     (the global prompt is prepended automatically);
  3. runs it in an isolated context (fresh context, no session) and returns its final text.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from strands import tool

from strandly_harness.core.constants import MODEL_TIER_DEFAULT, MODEL_TIERS
from strandly_harness.core.context import RuntimeContext

if TYPE_CHECKING:
    from strandly_harness.core.config import Config

# Maximum spawn nesting: the top agent may spawn, its subagents may not.
_MAX_DEPTH = 1

# invocation_state key carrying the current spawn depth (so subagents can enforce the limit).
_DEPTH_KEY = "_spawn_depth"

_SPAWN_DESCRIPTION = (
    "Spawn an isolated subagent to handle a focused subtask and return its final text. "
    # What it actually spawns — so the model reasons about it correctly.
    "The subagent is a fresh agent with its own context: it does NOT see this conversation, "
    "your files-in-memory, or prior turns. It shares the sandbox (same working directory and "
    "files) and the same approval/authorization gates. "
    # The key behavioral nudge: set context, because the subagent starts blind.
    "Because it starts blind, your `prompt` must be self-contained: state the goal and the "
    "necessary background, and point it at where to look (file paths, identifiers, commands) "
    "so it can discover the rest itself with its own tools. "
    # Model tiers: match depth/cost to the task.
    "Pick the subagent's model with `model`: 'advanced' (Fable 5, maximum-depth analysis) for "
    "passes where the quality of thought IS the deliverable — adversarial testing, API "
    "bar-raising, subtle-correctness hunts, gnarly debugging; 'fast' (Haiku, cheap and quick) "
    "for simple mechanical subtasks — routing decisions, formatting, small summaries; omit it "
    "(or 'default') for everything else (Opus, the harness model). "
    # The skill-driven pattern.
    "Give the subagent a `system_prompt`: pass a FILE PATH to a system-prompt "
    "markdown (e.g. a skill's prompt like `skills/code-review/assets/roles/reviewer.md`) and it is loaded "
    "as the subagent's system prompt, or pass literal prompt text. The harness's global prompt "
    "is always applied on top of it. Use it to parallelize or to get an independent, "
    "differently-scoped pass; you own synthesizing the result."
)


def _resolve_system_prompt(ctx: RuntimeContext, system_prompt: str) -> str:
    """A ``system_prompt`` naming an existing file is loaded; otherwise it's literal prompt text.

    File paths are resolved against ``ctx.cwd`` first, then against the packaged built-in skills
    directory (so ``skills/port/assets/roles/planner.md`` resolves even when cwd is a different repo).
    Falls back to treating the value as literal text when it isn't a readable file. This is the
    subagent's prompt *layer* — ``build_agent`` prepends the global prompt.
    """
    if not system_prompt:
        return ""
    # Try cwd-relative first (user-provided or project-local prompts).
    try:
        path = Path(ctx.cwd) / system_prompt
        if path.is_file():
            return path.read_text().strip()
    except OSError:
        pass
    # Try the packaged skills directory (handles "skills/<name>/role.md" from any cwd).
    if system_prompt.startswith("skills/"):
        from strandly_harness.skills.loader import builtin_skills_dir

        try:
            packaged = builtin_skills_dir() / system_prompt.removeprefix("skills/")
            if packaged.is_file():
                return packaged.read_text().strip()
        except OSError:
            pass
    return system_prompt


def make_spawn(
    config: Config, ctx: RuntimeContext, sandbox: Any = None, depth: int = 0
) -> Any:
    """Return a ``spawn`` tool bound to ``settings``/``ctx``/``sandbox`` at recursion ``depth``.

    ``sandbox`` is the parent agent's sandbox; the spawned subagent reuses it (rather than building
    its own) so they share one working directory, file set, and — on AgentCore — one Code
    Interpreter session. See :func:`build_agent`'s ``sandbox`` arg for why a per-subagent sandbox is
    harmful (it evicts the parent's session).
    """
    from strandly_harness.core.agent import build_agent  # local import avoids an import cycle

    @tool(name="spawn", description=_SPAWN_DESCRIPTION)
    async def spawn(prompt: str, system_prompt: str = "", model: str = "") -> str:
        """Spawn a subagent for a focused subtask and return its result.

        Args:
            prompt: The task for the subagent (its user message). Must be self-contained — the
                subagent does not see this conversation.
            system_prompt: The subagent's system prompt (layered under the harness's global
                prompt). Either a PATH to a markdown file (loaded as the prompt, e.g. a skill's
                system-prompt file) or literal prompt text.
            model: The subagent's model tier — "fast" (Haiku: simple mechanical subtasks),
                "advanced" (Fable 5: deep-dive analyses like adversarial testing or API
                bar-raising), or "default"/omitted (Opus, the harness model). Only these
                configured tiers are accepted — not arbitrary model ids.
        """
        if depth >= _MAX_DEPTH:
            return (
                f"Error: spawn depth limit reached (max_depth={_MAX_DEPTH}); "
                "this subagent may not spawn further subagents."
            )
        tier = model or MODEL_TIER_DEFAULT
        if tier not in MODEL_TIERS:
            valid = ", ".join(sorted(MODEL_TIERS))
            return (
                f"Error: unknown model tier {tier!r}. Pass one of the configured tiers "
                f"({valid}) or omit `model` for the default — arbitrary model ids are not "
                "accepted."
            )
        resolved_prompt = _resolve_system_prompt(ctx, system_prompt)
        # Isolated subagent: fresh context, no shared session id, one level deeper.
        sub_ctx = RuntimeContext(
            cwd=ctx.cwd,
            session_id=None,
            session_key=None,
            event=ctx.event,
            metadata={**ctx.metadata, _DEPTH_KEY: depth + 1},
        )
        sub_agent = await build_agent(
            config,
            sub_ctx,
            system_prompt=resolved_prompt,
            model_tier=tier,
            sandbox=sandbox,
            spawn_depth=depth + 1,
        )
        result = await sub_agent.invoke_async(prompt)
        return str(result)

    return spawn
