"""Strandly's tools on top of the harness built-ins, each gated on configuration.

The harness provides ``shell``, ``read``, ``write``, ``edit``, ``web_fetch`` and
``programmatic_tool_caller``. This module adds ``subagent`` (depth 0 only), ``use_github`` (a GitHub
token), ``use_slack`` (a Slack token and a Slack-triggered run, or the enabled home channel),
``schedule``, ``notify_team`` and ``set_session_title`` (depth 0, unscoped, when their backends are
configured), and the MCP tool providers. ``allowed_tools`` narrows the set for a scoped subagent
(``constants.SUBAGENT_SCOPABLE_TOOLS``); the read-only MCP lookups stay available.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from strandly.core.constants import SUBAGENT_MAX_DEPTH
from strandly.tools.subagent import SUBAGENT_TOOL_NAME, make_subagent_tool

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from strands.sandbox.base import Sandbox
    from strands_harness.tools.subagent import Preset

    from strandly.core.config import Config
    from strandly.core.context import RuntimeContext


def build_tools(
    config: Config,
    ctx: RuntimeContext,
    sandbox: Sandbox,
    *,
    spawn_depth: int = 0,
    allowed_tools: frozenset[str] | None = None,
    mcp_clients: list[Any] | None = None,
    roles: Mapping[str, Preset] | None = None,
) -> list[Any]:
    """Build Strandly's own tool list for ``create_harness(tools=...)`` (on top of the harness's).

    ``allowed_tools`` restricts the result to the named tools (``None`` = no restriction); the MCP
    providers are always included (read-only lookups — see the module docstring).

    ``mcp_clients`` are the parent's already-built MCP providers, passed in by ``subagent`` so a
    subagent **reuses** them instead of standing up its own — the same sharing rule the sandbox
    follows, and for a stricter reason: building them per-subagent deadlocked the runtime. See
    :func:`strandly.mcp_clients.build_mcp_clients`. ``roles`` are the skills' role presets for
    ``subagent`` (:func:`strandly.tools.subagent.load_roles`).
    """

    def allowed(name: str) -> bool:
        return allowed_tools is None or name in allowed_tools

    tools: list[Any] = []

    # GitHub — only when a token is configured. `use_github` is universal GraphQL access and the
    # only on-demand GitHub surface. GitHub *thread* enrichment for issue/PR/discussion URLs is
    # auto-injected by the GitHubContextInjector plugin at the turn boundary (see build_agent), so
    # there is no dedicated context-fetch tool — it would just duplicate `use_github`.
    if config.github_enabled and allowed("use_github"):
        from strandly.tools.github import make_use_github

        tools.append(make_use_github(config.github))

    # Slack replies keep their existing DM gate. The explicit operator switch additionally
    # allows shared home notifications; neither a token nor a destination alone enables them.
    home_channel = config.slack_home_channel
    scheduled = (ctx.session_id or "").startswith("sched-") or str(
        (ctx.event or {}).get("triggerSource", "")
    ).startswith("scheduled:")
    # A stored/forged shared-channel context cannot bypass the operator switch, and a scheduled
    # run never replies in a DM, whatever its payload carries: it posts only to the home channel.
    reply_channel = ctx.slack_channel_id
    if reply_channel and reply_channel != home_channel and (scheduled or not reply_channel.startswith("D")):
        reply_channel = None
    if config.slack_enabled and (reply_channel or home_channel) and allowed("use_slack"):
        from strandly.tools.slack import make_use_slack

        tools.append(
            make_use_slack(
                config.slack,
                default_channel_id=reply_channel,
                home_channel=home_channel,
                private_dm=ctx.is_slack_dm,
                default_thread_ts=ctx.slack_thread_ts,
                pin_destination=scheduled,
            )
        )

    # MCP tool sources (strands-agents docs always; web-search when configured).
    #
    # A subagent REUSES the parent's providers rather than building its own. Same rule as the
    # sandbox above, but the reason is harder: the SDK *starts* each client during Agent
    # construction, `MCPClient.load_tools` blocks its thread on an untimed cross-thread
    # `.result()`, and `strands.tools.registry` bridges provider registration through a
    # ThreadPoolExecutor — so N concurrent subagent builds each burn a pool thread waiting on a
    # future that needs a thread. Two parallel subagents were enough to deadlock the whole event
    # loop, permanently, with no error. Reuse also drops N-1
    # docs MCP session starts per review, which is the larger everyday win.
    if mcp_clients is not None:
        logger.info("reusing %d parent MCP client(s) for this subagent", len(mcp_clients))
        providers = list(mcp_clients)
    else:
        from strandly.mcp_clients import build_mcp_clients

        providers = build_mcp_clients(config)
    tools.extend(providers)

    # Subagents, only above the depth limit. Children SHARE the parent's sandbox and MCP providers
    # (same session/files, same started clients): a new sandbox is a different, empty workspace,
    # and new MCP clients deadlock the loop (see above).
    # Scheduled jobs: the top agent only, unscoped, and only where the service is configured. Never
    # a subagent's (a delegated task must not leave persistent work behind).
    if spawn_depth == 0 and allowed_tools is None:
        from strandly.tools.schedule import (
            ENV_ACTIONS_SCHEDULE_REPOSITORIES,
            ENV_DEFAULT_TIMEZONE,
            make_schedule_tool,
            schedule_settings,
            service_from_settings,
        )

        settings = schedule_settings(config)
        if settings is not None:
            tools.append(
                make_schedule_tool(
                    ctx,
                    lambda: service_from_settings(settings),
                    default_timezone=config.get(ENV_DEFAULT_TIMEZONE) or "UTC",
                    allowed_owners=config.github.allowed_owners or (),
                    actions_schedule_repositories=tuple(
                        r.strip()
                        for r in (config.get(ENV_ACTIONS_SCHEDULE_REPOSITORIES) or "").split(",")
                        if r.strip()
                    ),
                    home_channel=home_channel,
                )
            )

        # Home-channel notifications: the top agent of any non-DM run once the operator switch,
        # token and channel id are all set. A DM run's private context never reaches the team.
        if home_channel and not (ctx.slack_channel_id or "").startswith("D"):
            from strandly.tools.session_title import ledger_table_factory
            from strandly.tools.slack import make_notify_team

            tools.append(
                make_notify_team(
                    config.slack,
                    home_channel=home_channel,
                    ledger_table=ledger_table_factory(config) if config.run_ledger_enabled else None,
                )
            )

        # Session titles: the top agent of a session, where the run ledger (the title store) exists.
        if config.run_ledger_enabled and ctx.session_id:
            from strandly.tools.session_title import ledger_table_factory, make_session_title_tool

            tools.append(make_session_title_tool(ctx, ledger_table_factory(config)))

    if spawn_depth < SUBAGENT_MAX_DEPTH and allowed(SUBAGENT_TOOL_NAME):
        tools.append(
            make_subagent_tool(
                config,
                ctx,
                sandbox,
                depth=spawn_depth,
                allowed_tools=allowed_tools,
                mcp_clients=providers,
                roles=roles,
            )
        )

    return tools


__all__ = ["build_tools"]
