"""MCP server adapter — expose the agent as a single MCP tool (``ask_agent``).

Runs a full turn and returns the final text (streaming events collapsed). Requires the ``mcp``
extra. Unattended, so no HITL.
"""

from __future__ import annotations

from typing import Any

from strandly_harness.core.config import Config
from strandly_harness.core.context import RuntimeContext
from strandly_harness.serve.turn import run_turn


async def run_collect(config: Config, user_input: str, session_id: str | None) -> str:
    """Run one turn and concatenate the assistant text (the MCP tool's return value)."""
    ctx = RuntimeContext(session_id=session_id)
    chunks: list[str] = []
    async for ev in run_turn(config, user_input, ctx):
        if ev.kind == "text" and ev.text:
            chunks.append(ev.text)
        elif ev.kind == "error":
            return f"[error] {ev.text}"
    return "".join(chunks)


def build_server(config: Config) -> Any:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("strandly-harness")

    @server.tool(name="ask_agent")
    async def ask_agent(input: str, session_id: str | None = None) -> str:
        """Ask the Strandly agent. Provide a session_id to continue a conversation."""
        return await run_collect(config, input, session_id)

    return server


def serve_mcp(config: Config) -> None:
    build_server(config).run()
