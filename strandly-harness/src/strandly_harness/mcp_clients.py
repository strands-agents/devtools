"""External MCP servers the agent consumes as tools.

Two MCP clients, both ``ToolProvider``s the SDK starts on agent construction and tears down on
cleanup:

- **strands-agents** (always on) — the Strands docs/knowledge MCP (``uvx strands-agents-mcp-server``,
  stdio). Needs no secret; gives the agent access to Strands documentation + best practices.
- **web-search** (gated) — added only when ``STRANDLY_SEARCH_MCP_URL`` is configured (an HTTP/
  Streamable-HTTP MCP endpoint), with the optional ``STRANDLY_SEARCH_MCP_TOKEN`` as a bearer.

Requires the ``mcp`` extra. ``build_mcp_clients`` returns the clients to add to ``Agent(tools=…)``.
"""

from __future__ import annotations

import logging
import shutil
from typing import Any

from strandly_harness.core.config import Config

logger = logging.getLogger(__name__)


def _resolve_uv() -> list[str] | None:
    """The command prefix that runs a ``uvx`` tool, PATH-independently, or ``None`` if uv is absent.

    Prefer the ``uv`` **Python package**'s :func:`uv.find_uv_bin` (returns the absolute path to the
    bundled ``uv`` binary) so we don't depend on ``uvx`` being on ``PATH`` — in the AgentCore Runtime
    image the console scripts install outside the process ``PATH``, so ``shutil.which("uvx")`` fails
    even though ``uv`` is installed. ``uvx X`` == ``uv tool run X``. Fall back to a ``uvx`` on PATH
    (local dev), else ``None``.
    """
    try:
        import uv  # the pip package, present via the `mcp` extra / requirements.txt

        return [uv.find_uv_bin(), "tool", "run"]
    except Exception:  # noqa: BLE001 — uv missing/old; fall back to PATH lookup
        uvx = shutil.which("uvx")
        return [uvx] if uvx else None


def _stdio_client(command: str, args: list[str], *, prefix: str) -> Any:
    from mcp import StdioServerParameters, stdio_client
    from strands.tools.mcp import MCPClient

    params = StdioServerParameters(command=command, args=args)
    return MCPClient(lambda: stdio_client(params), prefix=prefix)


def _http_client(url: str, token: str | None, *, prefix: str) -> Any:
    from mcp.client.streamable_http import streamablehttp_client
    from strands.tools.mcp import MCPClient

    headers = {"Authorization": f"Bearer {token}"} if token else None
    return MCPClient(lambda: streamablehttp_client(url, headers=headers), prefix=prefix)


def build_mcp_clients(config: Config) -> list[Any]:
    """Build the MCP clients for this run (strands-agents always; web-search when configured).

    The SDK *eagerly starts* each MCP client when the agent is constructed, so a stdio client whose
    command isn't installed (e.g. ``uv`` missing) would crash agent construction. ``uv`` is a
    declared runtime dependency (``requirements.txt`` / the ``mcp`` extra) and is resolved
    PATH-independently via :func:`_resolve_uv`, so the docs MCP normally loads; we still guard so a
    stripped local environment degrades to "no docs MCP" with a warning instead of crashing.
    """
    clients: list[Any] = []
    uv_cmd = _resolve_uv()
    if uv_cmd:
        # uv_cmd is e.g. ["/path/to/uv", "tool", "run"] or ["/path/to/uvx"]; append the server name.
        clients.append(
            _stdio_client(uv_cmd[0], [*uv_cmd[1:], "strands-agents-mcp-server"], prefix="strands")
        )
    else:
        logger.warning(
            "uv/uvx not found; skipping the strands-agents docs MCP. `uv` should be installed "
            "(it's in requirements.txt / the `mcp` extra) — install it (https://docs.astral.sh/uv/) "
            "to enable the docs MCP."
        )
    if config.search_mcp_url:
        clients.append(
            _http_client(config.search_mcp_url, config.search_mcp_token, prefix="web")
        )
    return clients
