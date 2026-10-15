"""The harness's tool set — fixed, with two capabilities gated on configuration.

Always present (sandbox-routed where they touch files/exec):
- **file/exec**: ``bash`` · ``file_editor`` (``file_editor`` reads via its line-numbered ``view``;
  finding/searching is a ``bash`` call — ``rg``/``grep``/``find`` — inside the sandbox)
- **delegation**: ``spawn`` (subagents through the same factory)
- **reasoning**: ``think``

Gated:
- **``use_github``** — only when a GitHub token is configured (``config.github_enabled``).
  (``use_github`` is the *only* on-demand GitHub surface — the agent uses it for any URL it wants
  to fetch mid-turn. GitHub *thread* enrichment is auto-injected by the ``GitHubContextInjector``
  plugin at the turn boundary, added in ``build_agent`` — there is no separate context-fetch tool.)
- **MCP tools** — the strands-agents docs MCP (always) + a web-search MCP (when configured), added
  as ``MCPClient`` ToolProviders.

(``todo`` is a plugin — tool + re-surface hook — added in ``build_agent``, not here.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from strandly_harness.tools.builtins import make_builtins

if TYPE_CHECKING:
    from strands.sandbox.base import Sandbox

    from strandly_harness.core.config import Config
    from strandly_harness.core.context import RuntimeContext


def build_tools(
    config: Config,
    ctx: RuntimeContext,
    sandbox: Sandbox,
    *,
    spawn_depth: int = 0,
    allow_spawn: bool = True,
) -> list[Any]:
    """Build the tool list for ``Agent(tools=...)``."""
    builtins = make_builtins(sandbox)
    tools: list[Any] = [
        builtins["bash"],
        builtins["file_editor"],
        "strands_tools.think",
    ]

    # GitHub — only when a token is configured. `use_github` is universal GraphQL access and the
    # only on-demand GitHub surface. GitHub *thread* enrichment for issue/PR/discussion URLs is
    # auto-injected by the GitHubContextInjector plugin at the turn boundary (see build_agent), so
    # there is no dedicated context-fetch tool — it would just duplicate `use_github`.
    if config.github_enabled:
        from strandly_harness.tools.github import make_use_github

        tools.append(make_use_github(config.github))

    # MCP tool sources (strands-agents docs always; web-search when configured).
    from strandly_harness.mcp_clients import build_mcp_clients

    tools.extend(build_mcp_clients(config))

    # Subagents — bound so a leaf subagent can't spawn further. The parent's sandbox is passed in
    # so spawned subagents SHARE it (same session/files) rather than each starting a new AgentCore
    # session that would evict the parent's — see make_spawn / build_agent.
    if allow_spawn and spawn_depth < 1:
        from strandly_harness.tools.spawn import make_spawn

        tools.append(make_spawn(config, ctx, sandbox, depth=spawn_depth))

    return tools


__all__ = ["build_tools"]
