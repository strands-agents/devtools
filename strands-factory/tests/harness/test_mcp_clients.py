from __future__ import annotations

import gc
import importlib
import io
import logging
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from mcp.client.stdio import DEFAULT_INHERITED_ENV_VARS
from packaging.requirements import Requirement

from strandly.core.config import Config
from strandly.mcp_clients import _http_client, _stdio_client

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]

# The SDK's both-`mcp`-majors seam `_http_client` opens (1.x `streamablehttp_client` vs 2.x
# `streamable_http_client`); patching it is what lets these tests pass on either major.
_TRANSPORT = "strands.tools.mcp._compat.streamable_http_transport"


def _cfg(**values) -> Config:
    return Config(values=values)


def _transport_lambda(**kwargs) -> callable:
    """Capture the zero-arg lambda `_http_client` hands MCPClient, without touching MCPClient."""
    with patch("strands.tools.mcp.MCPClient") as mock_mcp_client:
        _http_client(**kwargs)
        (transport_callable,), _ = mock_mcp_client.call_args
        return transport_callable


def test_stdio_client_defaults_continue_on_error_true():
    # No MCP is load-bearing: a broken third-party server (uvx fetches it fresh every run, no
    # pin) must degrade to "no tools", not crash agent construction.
    with patch("strands.tools.mcp.MCPClient") as mock_mcp_client:
        _stdio_client("uv", ["tool", "run", "strands-agents-mcp-server"], prefix="strands")
        _, kwargs = mock_mcp_client.call_args
        assert kwargs["continue_on_error"] is True


def test_http_client_bearer_continue_on_error_true():
    with patch("strands.tools.mcp.MCPClient") as mock_mcp_client:
        _http_client(url="https://mcp.example/search", token="tok", prefix="web")
        _, kwargs = mock_mcp_client.call_args
        assert kwargs["continue_on_error"] is True


def test_http_client_sigv4_continue_on_error_true():
    with patch("strands.tools.mcp.MCPClient") as mock_mcp_client:
        _http_client(
            url="https://gw.example/mcp", token=None, prefix="web", sigv4_region="us-east-1"
        )
        _, kwargs = mock_mcp_client.call_args
        assert kwargs["continue_on_error"] is True


def test_build_mcp_clients_docs_mcp_continue_on_error_true():
    from strandly import mcp_clients as mod

    with patch.object(mod, "_resolve_uv", return_value=["/usr/bin/uv", "tool", "run"]), patch(
        "strands.tools.mcp.MCPClient"
    ) as mock_mcp_client:
        mod.build_mcp_clients(_cfg())
        _, kwargs = mock_mcp_client.call_args
        assert kwargs["continue_on_error"] is True


def test_http_client_bearer_auth_passes_headers():
    with patch(_TRANSPORT) as mock_client:
        transport = _transport_lambda(url="https://mcp.example/search", token="tok", prefix="web")
        transport()
        _, kwargs = mock_client.call_args
        assert kwargs["headers"] == {"Authorization": "Bearer tok"}
        assert "auth" not in kwargs


def test_http_client_no_token_passes_no_headers():
    with patch(_TRANSPORT) as mock_client:
        transport = _transport_lambda(url="https://mcp.example/search", token=None, prefix="web")
        transport()
        _, kwargs = mock_client.call_args
        assert kwargs["headers"] is None


def test_http_client_sigv4_passes_auth_not_headers():
    with patch(_TRANSPORT) as mock_client:
        transport = _transport_lambda(
            url="https://gw.example/mcp", token=None, prefix="web", sigv4_region="us-east-1"
        )
        transport()
        _, kwargs = mock_client.call_args
        assert "headers" not in kwargs
        from strandly.core.sigv4_httpx import SigV4HttpxAuth

        assert isinstance(kwargs["auth"], SigV4HttpxAuth)
        assert kwargs["auth"]._service == "bedrock-agentcore"
        assert kwargs["auth"]._region == "us-east-1"


def test_build_mcp_clients_uses_sigv4_when_configured():
    from strandly import mcp_clients as mod

    config = _cfg(
        STRANDLY_SEARCH_MCP_URL="https://gw.example/mcp",
        STRANDLY_SEARCH_MCP_AUTH="sigv4",
        STRANDLY_SEARCH_MCP_REGION="us-east-1",
    )
    with patch.object(mod, "_resolve_uv", return_value=None), patch.object(
        mod, "_http_client"
    ) as mock_http:
        mod.build_mcp_clients(config)
        mock_http.assert_called_once_with(
            "https://gw.example/mcp", None, prefix="web", sigv4_region="us-east-1"
        )


def test_build_mcp_clients_uses_bearer_when_not_sigv4():
    from strandly import mcp_clients as mod

    config = _cfg(
        STRANDLY_SEARCH_MCP_URL="https://mcp.example/search",
        STRANDLY_SEARCH_MCP_TOKEN="tok",
    )
    with patch.object(mod, "_resolve_uv", return_value=None), patch.object(
        mod, "_http_client"
    ) as mock_http:
        mod.build_mcp_clients(config)
        mock_http.assert_called_once_with(
            "https://mcp.example/search", "tok", prefix="web", sigv4_region=None
        )


def test_docs_mcp_uvx_fallback_runs_an_exact_pinned_release(monkeypatch):
    """Without the installed server, uvx runs the pinned release on the agent's interpreter."""
    from strandly import mcp_clients as mod

    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/python/lib")
    with patch.object(mod, "_load_docs_server", return_value=None), patch.object(
        mod, "_resolve_uv", return_value=["/usr/bin/uv", "tool", "run"]
    ), patch.object(mod, "_stdio_client") as stdio:
        mod.build_mcp_clients(_cfg())
    command, args = stdio.call_args.args
    assert command == "/usr/bin/uv"
    assert args == ["tool", "run", "--from", mod.DOCS_MCP_REQUIREMENT, "strands-agents-mcp-server"]
    env = stdio.call_args.kwargs["env"]
    assert env["UV_PYTHON"] == sys.executable
    assert env["LD_LIBRARY_PATH"] == "/opt/python/lib"
    name, _, version = mod.DOCS_MCP_REQUIREMENT.partition("==")
    assert name == "strands-agents-mcp-server"
    assert version and all(part.isdigit() for part in version.split(".")), version


def test_docs_mcp_skipped_when_neither_installed_nor_uv(caplog):
    from strandly import mcp_clients as mod

    with patch.object(mod, "_load_docs_server", return_value=None), patch.object(
        mod, "_resolve_uv", return_value=None
    ), caplog.at_level(logging.WARNING, logger=mod.__name__):
        assert mod.build_mcp_clients(_cfg()) == []
    assert "docs MCP skipped" in caplog.text


def test_docs_mcp_fallback_pin_matches_the_lock():
    """The uvx fallback and the installed server are the same release."""
    from strandly import mcp_clients as mod

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    extra = {r.name: r for r in map(Requirement, project["optional-dependencies"]["mcp"])}
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {p["name"]: p["version"] for p in lock["package"]}
    assert str(extra[mod.DOCS_MCP_COMMAND].specifier) == f"=={locked[mod.DOCS_MCP_COMMAND]}"
    assert mod.DOCS_MCP_REQUIREMENT == f"{mod.DOCS_MCP_COMMAND}=={locked[mod.DOCS_MCP_COMMAND]}"


def test_docs_mcp_runs_the_installed_server_in_process(caplog):
    from strandly import mcp_clients as mod

    with patch.object(mod, "_resolve_uv") as resolve_uv, patch.object(
        mod, "_stdio_client"
    ) as stdio, patch.object(mod, "_in_process_client", wraps=mod._in_process_client) as in_proc, (
        caplog.at_level(logging.INFO, logger=mod.__name__)
    ):
        clients = mod.build_mcp_clients(_cfg())
    resolve_uv.assert_not_called()
    stdio.assert_not_called()
    (server,), kwargs = in_proc.call_args
    assert server is importlib.import_module("strands_mcp_server.server").mcp
    assert kwargs == {"prefix": "strands"}
    assert len(clients) == 1
    assert f"docs MCP launch: path=in_process python={sys.executable}" in caplog.text


def test_docs_mcp_broken_install_falls_back_to_uvx(caplog):
    from strandly import mcp_clients as mod

    with patch.object(mod.importlib, "import_module", side_effect=ImportError("boom")), patch.object(
        mod, "_resolve_uv", return_value=["/usr/bin/uv", "tool", "run"]
    ), patch.object(mod, "_stdio_client") as stdio, caplog.at_level(logging.INFO, logger=mod.__name__):
        mod.build_mcp_clients(_cfg())
    assert stdio.call_args.args[0] == "/usr/bin/uv"
    assert "docs MCP in-process import failed" in caplog.text
    assert "docs MCP launch: path=uvx" in caplog.text


def test_uvx_child_env_is_an_allow_list(monkeypatch):
    """The loader path reaches the uvx child; credentials and the parent's PYTHONPATH do not."""
    from strandly import mcp_clients as mod

    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/python/lib")
    monkeypatch.setenv("PYTHONHOME", "/opt/python")
    monkeypatch.setenv("PYTHONPATH", "/otel/auto_instrumentation")
    secrets = {
        "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "AWS_SESSION_TOKEN": "session",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN": "container",
        "GITHUB_TOKEN": "ghp_example",
        "STRANDLY_SECRETS_ARN": "arn:aws:secretsmanager:us-east-1:111111111111:secret:x",
        "SLACK_BOT_TOKEN": "xoxb-example",
        "OTEL_EXPORTER_OTLP_HEADERS": "authorization=secret",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    env = mod._child_env(UV_PYTHON=sys.executable)
    assert env["LD_LIBRARY_PATH"] == "/opt/python/lib"
    assert env["PYTHONHOME"] == "/opt/python"
    assert "PYTHONPATH" not in env
    assert not set(secrets) & set(env)
    assert all(value not in env.values() for value in secrets.values())
    assert set(env) <= {*DEFAULT_INHERITED_ENV_VARS, *mod.CHILD_ENV_ALLOWLIST, "UV_PYTHON"}


def test_uvx_child_env_omits_unset_loader_path(monkeypatch):
    from strandly import mcp_clients as mod

    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    assert "LD_LIBRARY_PATH" not in mod._child_env()


def test_tool_less_clients_are_named(caplog):
    from strandly import mcp_clients as mod

    with patch("strands.tools.mcp.MCPClient", side_effect=lambda *a, **k: MagicMock()):
        docs = _stdio_client("python", ["-m", "x"], prefix="strands")
        web = _http_client(url="https://mcp.example/search", token=None, prefix="web")
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        missing = mod.warn_tool_less_clients([docs, web, object()], ["strands_search_docs", "shell"])
    assert missing == ["web"]
    assert "MCP server contributed no tools: prefix=web" in caplog.text


LLMS_TXT = b"- [Quickstart](https://strandsagents.com/docs/quickstart/index.md): Start here\n"
DOCS_TOOLS = {"strands_search_docs", "strands_fetch_doc", "strands_find_integrations"}


@pytest.fixture
def offline_docs(monkeypatch):
    """Serve llms.txt in place of strandsagents.com, on a fresh copy of the server's caches.

    Returns:
        The list of URLs the server requested.
    """
    from strands_mcp_server.utils import cache

    requested: list[str] = []

    def offline_urlopen(req, *args, **kwargs):
        url = getattr(req, "full_url", req)
        requested.append(url)
        if url == "https://strandsagents.com/llms.txt":
            time.sleep(0.05)  # widens the window two racing first loads would share
            return io.BytesIO(LLMS_TXT)
        raise OSError(f"network disabled in tests: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", offline_urlopen)
    monkeypatch.setattr(cache, "_INDEX", None)
    monkeypatch.setattr(cache, "_URL_CACHE", {})
    monkeypatch.setattr(cache, "_URL_TITLES", {})
    monkeypatch.setattr(cache, "_LINKS_LOADED", False)
    return requested


def _docs_client() -> Any:
    from strandly import mcp_clients as mod

    (client,) = mod.build_mcp_clients(_cfg())
    return client


def _search(client: Any, tool_use_id: str = "t1") -> dict:
    return client.call_tool_sync(tool_use_id, "search_docs", {"query": "quickstart"})


def test_in_process_docs_server_lists_and_calls_its_tools(offline_docs):
    """The installed server, in-process, lists the docs tools and answers a search offline."""
    with _docs_client() as client:
        names = {tool.tool_name for tool in client.list_tools_sync()}
        result = _search(client)
    assert DOCS_TOOLS <= names
    assert result["status"] == "success"
    assert "https://strandsagents.com/docs/quickstart/index.md" in result["content"][0]["text"]
    assert offline_docs.count("https://strandsagents.com/llms.txt") == 1


def test_in_process_docs_server_never_spawns_a_process(offline_docs):
    """The runtime's interpreter cannot start a child Python; the docs tools load without one."""
    import asyncio
    import subprocess

    import anyio

    def no_child(*args, **kwargs):
        raise OSError("libpython3.12.so.1.0: cannot open shared object file")

    with patch.object(subprocess, "Popen", side_effect=no_child), patch.object(
        asyncio, "create_subprocess_exec", side_effect=no_child
    ), patch.object(anyio, "open_process", side_effect=no_child), patch.object(
        os, "posix_spawn", side_effect=no_child
    ), _docs_client() as client:
        names = {tool.tool_name for tool in client.list_tools_sync()}
        assert _search(client)["status"] == "success"
    assert DOCS_TOOLS <= names


def test_in_process_tool_calls_on_one_client_run_concurrently(offline_docs, monkeypatch):
    """A docs tool's blocking fetch does not stall the client's loop: two fetches overlap.

    The server's tools are sync; mcp 1.x would run them on the event loop the server shares
    with the client, so a second call could not start until the first returned.
    """
    pages = [f"https://strandsagents.com/docs/page{i}/index.md" for i in (1, 2)]
    both_fetching = threading.Barrier(2, timeout=5)
    offline = urllib.request.urlopen

    def overlapping_urlopen(req, *args, **kwargs):
        url = getattr(req, "full_url", req)
        if url in pages:
            both_fetching.wait()  # breaks, failing the fetch, unless the other call is in flight
            return io.BytesIO(f"# Page\n\nBody of {url}\n".encode())
        return offline(req, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", overlapping_urlopen)
    with _docs_client() as client, ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(
                lambda i: client.call_tool_sync(f"f{i}", "fetch_doc", {"uri": pages[i]}),
                range(2),
            )
        )
    for page, result in zip(pages, results, strict=True):
        assert result["status"] == "success", result
        assert f"Body of {page}" in str(result["content"])


def test_two_in_process_clients_work_concurrently_and_stop_cleanly(offline_docs, caplog):
    """Each agent build gets its own session; the first index load runs once; nothing leaks."""
    before = set(threading.enumerate())
    clients = [_docs_client(), _docs_client()]
    with caplog.at_level(logging.DEBUG, logger="asyncio"), ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda c: c.start(), clients))
        results = list(pool.map(lambda pair: _search(*pair), zip(clients, ["a", "b"], strict=True)))
        names = list(pool.map(lambda c: {t.tool_name for t in c.list_tools_sync()}, clients))
        list(pool.map(lambda c: c.stop(None, None, None), clients))
    gc.collect()
    assert [r["toolUseId"] for r in results] == ["a", "b"]
    assert all(r["status"] == "success" for r in results)
    assert all(DOCS_TOOLS <= n for n in names)
    assert offline_docs.count("https://strandsagents.com/llms.txt") == 1
    from strands_mcp_server.utils import cache

    assert len(cache.get_index().docs) == 1
    assert set(threading.enumerate()) <= before
    assert "Task was destroyed" not in caplog.text
    assert "never awaited" not in caplog.text


def test_loading_the_docs_server_leaves_the_root_logger_alone(monkeypatch):
    """The server's FastMCP calls logging.basicConfig at import; the root logger stays ours."""
    from strandly import mcp_clients as mod

    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    monkeypatch.delitem(sys.modules, "strands_mcp_server.server")
    server = mod._load_docs_server()
    assert server is sys.modules["strands_mcp_server.server"].mcp
    assert root.handlers == []
    assert root.level == logging.WARNING
