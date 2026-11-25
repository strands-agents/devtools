"""External MCP servers the agent uses as tools.

The Strands docs MCP (``strands-agents-mcp-server``, locked in the ``mcp`` extra) runs in-process
over memory streams; ``uv tool run`` at ``DOCS_MCP_REQUIREMENT`` launches it when it is not
installed. An optional web-search MCP (``STRANDLY_SEARCH_MCP_URL``) is authenticated by bearer token
or SigV4. The SDK starts both when the agent is built, so a server that fails to start is skipped
rather than failing the build.
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
import logging
import os
import shutil
import sys
import threading
import weakref
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

from strandly.core.config import Config

logger = logging.getLogger(__name__)

DOCS_MCP_COMMAND = "strands-agents-mcp-server"
DOCS_MCP_MODULE = "strands_mcp_server"
# The uvx fallback's pin. uv.lock pins the installed server; a test keeps the two equal, so local
# dev without the `mcp` extra runs the same release as the runtime, never "latest".
DOCS_MCP_REQUIREMENT = f"{DOCS_MCP_COMMAND}==0.4.0"

# What the uvx-launched stdio MCP child inherits beyond mcp's defaults (HOME, LOGNAME, PATH, SHELL,
# TERM, USER): the interpreter's loader path and home, TLS trust, proxies, locale and temp dir. An
# allow-list, so the agent's AWS credentials and tokens never reach a third-party server.
CHILD_ENV_ALLOWLIST = (
    "LD_LIBRARY_PATH",
    "PYTHONHOME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "TMPDIR",
    "LANG",
    "LC_ALL",
)

#: Tool prefix of each client built here, so a client that contributed no tools can be named.
_CLIENT_PREFIXES: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()


def _child_env(**extra: str) -> dict[str, str]:
    """The environment of a stdio MCP child: mcp's defaults, :data:`CHILD_ENV_ALLOWLIST`, ``extra``."""
    from mcp.client.stdio import get_default_environment

    env = get_default_environment()
    env.update({name: os.environ[name] for name in CHILD_ENV_ALLOWLIST if name in os.environ})
    env.update(extra)
    return env


# Seconds the in-process server gets to finish after its input closes before it is cancelled.
_SERVER_SHUTDOWN_GRACE = 2.0

_DOCS_SERVER_LOCK = threading.Lock()


def _load_docs_server() -> Any | None:
    """The installed docs server's ``FastMCP`` (``MCPServer`` on mcp 2.x), or ``None`` if absent."""
    if importlib.util.find_spec(DOCS_MCP_MODULE) is None:
        return None
    with _DOCS_SERVER_LOCK:
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        try:
            # Constructing the FastMCP object at import calls logging.basicConfig, which would put
            # its own handler and level on the root logger; the root logger is the host's.
            server = importlib.import_module(f"{DOCS_MCP_MODULE}.server")
            cache = importlib.import_module(f"{DOCS_MCP_MODULE}.utils.cache")
        except Exception:  # noqa: BLE001 - a broken install falls back to uvx, never fails the build
            logger.warning("docs MCP in-process import failed; trying uvx", exc_info=True)
            return None
        finally:
            root.handlers[:] = handlers
            root.setLevel(level)
        _serialize_index_load(cache)
        _offload_sync_tools(server.mcp)
        # mcp 1.x's lowlevel server logs an INFO "Processing request of type ..." per request.
        lowlevel = logging.getLogger("mcp.server.lowlevel.server")
        if lowlevel.level == logging.NOTSET:
            lowlevel.setLevel(logging.WARNING)
    return server.mcp


def _serialize_index_load(cache: Any) -> None:
    """Make the server's first index load run once when several sessions in this process race it.

    Every in-process session shares the module-level index; two concurrent first ``ensure_ready``
    calls would each load llms.txt and index every page twice.
    """
    if getattr(cache.ensure_ready, "_strandly_serialized", False):
        return
    original = cache.ensure_ready
    lock = threading.Lock()

    def ensure_ready() -> None:
        with lock:
            original()

    ensure_ready._strandly_serialized = True  # type: ignore[attr-defined]
    cache.ensure_ready = ensure_ready


def _offload_sync_tools(server: Any) -> None:
    """Run the server's sync tools on a worker thread, as mcp 2.x's ``MCPServer`` already does.

    mcp 1.x's ``FastMCP`` calls a sync tool on the event loop, which in-process is the MCP client's
    own loop: a ``fetch_doc`` blocked on strandsagents.com would stall every other call on that
    client and the client's timeouts with it.
    """
    import anyio.to_thread

    manager = getattr(server, "_tool_manager", None)  # mcp 1.x only
    for tool in manager.list_tools() if manager is not None else ():
        if tool.is_async:
            continue
        fn = tool.fn

        async def offloaded(*args: Any, _fn: Any = fn, **kwargs: Any) -> Any:
            return await anyio.to_thread.run_sync(functools.partial(_fn, *args, **kwargs))

        tool.fn, tool.is_async = offloaded, True


@asynccontextmanager
async def _in_process_transport(server: Any) -> AsyncIterator[tuple[Any, Any]]:
    """An MCP transport that serves ``server`` on the event loop that enters it.

    ``MCPClient`` enters its transport on its own background loop, so the server's task lives in
    that loop's task group and ends when the client stops: closing the client's write stream ends
    the server's input, and a server that has not returned within the grace period is cancelled.
    """
    import anyio
    from mcp.shared.memory import create_client_server_memory_streams

    # The lowlevel Server behind FastMCP (mcp 1.x) or MCPServer (2.x); mcp's own in-memory
    # helpers read the same private attribute.
    lowlevel = getattr(server, "_lowlevel_server", None) or server._mcp_server
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        server_read, server_write = server_streams
        done = anyio.Event()

        async def serve() -> None:
            try:
                await lowlevel.run(server_read, server_write, lowlevel.create_initialization_options())
            finally:
                done.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(serve)
            try:
                yield client_streams
            finally:
                await client_streams[1].aclose()
                with anyio.move_on_after(_SERVER_SHUTDOWN_GRACE):
                    await done.wait()
                tg.cancel_scope.cancel()


def _resolve_uv() -> list[str] | None:
    """The command prefix that runs a ``uvx`` tool, PATH-independently, or ``None`` if uv is absent.

    Prefer the ``uv`` **Python package**'s :func:`uv.find_uv_bin` (returns the absolute path to the
    bundled ``uv`` binary) so we don't depend on ``uvx`` being on ``PATH`` — in the AgentCore Runtime
    image the console scripts install outside the process ``PATH``, so ``shutil.which("uvx")`` fails
    even though ``uv`` is installed. ``uvx X`` == ``uv tool run X``. Fall back to a ``uvx`` on PATH
    (local dev), else ``None``.
    """
    try:
        import uv  # the pip package, present via the `mcp` extra

        return [uv.find_uv_bin(), "tool", "run"]
    except Exception:  # noqa: BLE001 — uv missing/old; fall back to PATH lookup
        uvx = shutil.which("uvx")
        return [uvx] if uvx else None


# Every client is named (``application_name`` = its tool prefix, reported to the server as
# ``clientInfo.name``): the Strands harness keys MCP clients by that name when it builds its
# ``subagent`` tool — which it constructs on every ``create_harness`` call even though Strandly drops
# the tool — and logs a WARNING per unnamed client per build otherwise.
def _stdio_client(
    command: str,
    args: list[str],
    *,
    prefix: str,
    env: dict[str, str] | None = None,
    continue_on_error: bool = True,
) -> Any:
    from mcp import StdioServerParameters, stdio_client
    from strands.tools.mcp import MCPClient

    params = StdioServerParameters(command=command, args=args, env=env)
    client = MCPClient(
        lambda: stdio_client(params),
        prefix=prefix,
        continue_on_error=continue_on_error,
        application_name=prefix,
    )
    _CLIENT_PREFIXES[client] = prefix
    return client


def _in_process_client(server: Any, *, prefix: str) -> Any:
    from strands.tools.mcp import MCPClient

    client = MCPClient(
        lambda: _in_process_transport(server),
        prefix=prefix,
        continue_on_error=True,
        application_name=prefix,
    )
    _CLIENT_PREFIXES[client] = prefix
    return client


def _http_client(
    url: str, token: str | None, *, prefix: str, sigv4_region: str | None = None
) -> Any:
    from strands.tools.mcp import MCPClient

    # The SDK's own both-majors seam (what `MCPClient(MCPServerConfig(url=...))` opens internally).
    # `mcp` 1.x has `streamablehttp_client(url, headers=, auth=)`; 2.x renamed it to
    # `streamable_http_client(url, http_client=)` on top of `httpx2`, which rejects an `httpx.Auth`
    # such as `SigV4HttpxAuth`. This keeps the 1.x call shape and adapts the auth on 2.x, so it is
    # deliberately not re-implemented here.
    from strands.tools.mcp._compat import streamable_http_transport

    if sigv4_region:
        from strandly.core.sigv4_httpx import SigV4HttpxAuth

        auth = SigV4HttpxAuth(service="bedrock-agentcore", region=sigv4_region)
        client = MCPClient(
            lambda: streamable_http_transport(url, auth=auth),
            prefix=prefix,
            continue_on_error=True,
            application_name=prefix,
        )
        _CLIENT_PREFIXES[client] = prefix
        return client
    headers = {"Authorization": f"Bearer {token}"} if token else None
    client = MCPClient(
        lambda: streamable_http_transport(url, headers=headers),
        prefix=prefix,
        continue_on_error=True,
        application_name=prefix,
    )
    _CLIENT_PREFIXES[client] = prefix
    return client


def _docs_client() -> Any | None:
    """The docs MCP client: the installed server in-process, else ``uv tool run``, else ``None``."""
    server = _load_docs_server()
    if server is not None:
        logger.info("docs MCP launch: path=in_process python=%s", sys.executable)
        return _in_process_client(server, prefix="strands")
    uv_cmd = _resolve_uv()
    if uv_cmd is None:
        logger.warning(
            "docs MCP skipped: %s is not installed and uv/uvx is not found; install the `mcp` "
            "extra to enable it",
            DOCS_MCP_COMMAND,
        )
        return None
    # uv_cmd is e.g. ["/path/to/uv", "tool", "run"] or ["/path/to/uvx"]; append the server.
    env = _child_env(UV_PYTHON=sys.executable)
    logger.info(
        "docs MCP launch: path=uvx python=%s ld_library_path=%s",
        sys.executable,
        "LD_LIBRARY_PATH" in env,
    )
    return _stdio_client(
        uv_cmd[0],
        [*uv_cmd[1:], "--from", DOCS_MCP_REQUIREMENT, DOCS_MCP_COMMAND],
        prefix="strands",
        env=env,
    )


def build_mcp_clients(config: Config) -> list[Any]:
    """Build the MCP clients for this run (strands-agents always; web-search when configured).

    The SDK *eagerly starts* each MCP client when the agent is constructed. The docs MCP is a locked
    dependency (the ``mcp`` extra) served in-process, so a cold start spawns and installs nothing:
    the runtime's interpreter cannot start a child Python, and the server fetches strandsagents.com
    only when a tool is called. Each client has its own session on the shared server object. Without
    the package, ``uv tool run`` (resolved PATH-independently by :func:`_resolve_uv`) runs the same
    pinned release, and with neither the docs MCP is skipped with a warning.

    Every MCP client is built with ``continue_on_error=True``: an external MCP server (a
    third-party package or a remote HTTP endpoint we don't control) can break at any time for
    reasons outside this codebase (a broken interpreter environment, a transient outage), and that
    must never take down the whole agent. Every MCP is optional: a failed client degrades to "no
    tools from that server" (logged as a warning, and named by :func:`warn_tool_less_clients`)
    rather than raising and killing agent construction.
    """
    # Entry/exit logging is deliberate, not decoration: the SDK *starts* each client when the Agent
    # is constructed, and `MCPClient.load_tools` blocks the calling thread on a cross-thread future
    # (`mcp_client.py`'s untimed `.result()`), so a client that never finishes starting takes the
    # whole run with it and leaves NO error behind. A concurrent-subagent hang was diagnosed here purely
    # from *absent* lines. Paired start/finish counts are what answer "which of the N builds didn't
    # return?" — keep both, and keep them at INFO so they survive in prod.
    logger.info("building MCP clients (search_mcp=%s)", bool(config.search_mcp_url))
    clients: list[Any] = []
    docs = _docs_client()
    if docs is not None:
        clients.append(docs)
    if config.search_mcp_url:
        clients.append(
            _http_client(
                config.search_mcp_url,
                config.search_mcp_token,
                prefix="web",
                sigv4_region=config.search_mcp_region if config.search_mcp_sigv4 else None,
            )
        )
    # The paired finish line. Its absence is the whole diagnostic: a build that started and never
    # returned is what a wedged fan-out looks like from the outside.
    logger.info("built %d MCP client(s)", len(clients))
    return clients


def warn_tool_less_clients(providers: Iterable[Any], tool_names: Iterable[str]) -> list[str]:
    """Log a WARNING naming each MCP client built here that contributed no tool to an agent.

    The SDK's own ``MCP server failed to start`` warning does not say which server failed.

    Returns:
        The prefixes of the tool-less clients.
    """
    names = list(tool_names)
    missing = []
    for provider in providers:
        try:
            prefix = _CLIENT_PREFIXES.get(provider)
        except TypeError:  # an unhashable provider is not one of ours
            continue
        if prefix and not any(name.startswith(f"{prefix}_") for name in names):
            missing.append(prefix)
            logger.warning(
                "MCP server contributed no tools: prefix=%s (it failed to start or lists none); "
                "continuing without it",
                prefix,
            )
    return missing
