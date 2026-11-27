"""``strandly.ops.github``: same-origin token redirects and fail-open run-lifecycle reactions.

Two real local HTTP servers stand in for ``api.github.com`` and a foreign host; every GitHub-token
call site outside ``sandbox/`` and ``memory/`` is driven against them through its real request
code. Reaction tests mock the shared opener (``github._OPENER.open``).
"""

from __future__ import annotations

import contextlib
import http.server
import json
import threading
import urllib.error
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pytest

from strandly.ops import github
from strandly.ops.lambdas.mention_poller import handler as poller

TOKEN = "ghp_test_token"


@dataclass
class _Server:
    base: str
    seen: dict[str, str | None] = field(default_factory=dict)  # path -> Authorization header


def _serve(host: str, routes: dict[str, str]) -> tuple[http.server.HTTPServer, _Server]:
    """A server that records each request's Authorization and redirects per ``routes``."""
    state = _Server(base="")

    class Handler(http.server.BaseHTTPRequestHandler):
        def _handle(self) -> None:
            path = self.path.split("?")[0]
            state.seen[path] = self.headers.get("Authorization")
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            target = routes.get(path)
            if target is not None:
                self.send_response(302 if "other" in path else 301)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps({"ok": True, "data": {}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _handle  # noqa: N815 — http.server's naming

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    state.base = f"http://{host}:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    return server, state


@pytest.fixture
def servers() -> Iterator[tuple[_Server, _Server]]:
    """``(api, foreign)``, differing by hostname (127.0.0.1 vs localhost) as well as port.

    On ``api``: ``/same/x`` 301s to ``/landed/x`` on itself; ``/other/x`` 302s to ``foreign``'s
    ``/landed/x``; ``/bounce/x`` 302s to ``foreign``'s ``/back/x``, which 302s to ``api``'s
    ``/returned/x``.
    """
    api_routes: dict[str, str] = {}
    foreign_routes: dict[str, str] = {}
    api_srv, api = _serve("127.0.0.1", api_routes)
    foreign_srv, foreign = _serve("localhost", foreign_routes)
    api_routes.update(
        {
            "/same/x": f"{api.base}/landed/x",
            "/other/x": f"{foreign.base}/landed/x",
            "/bounce/x": f"{foreign.base}/back/x",
        }
    )
    foreign_routes["/back/x"] = f"{api.base}/returned/x"
    try:
        yield api, foreign
    finally:
        for srv in (api_srv, foreign_srv):
            srv.shutdown()
            srv.server_close()


def _get(url: str, token: str | None = TOKEN) -> None:
    import urllib.request

    with github.open_github(urllib.request.Request(url), token, timeout=10) as resp:
        resp.read()


def test_token_is_kept_on_a_same_origin_redirect(servers):
    api, _foreign = servers
    _get(f"{api.base}/same/x")
    assert api.seen["/same/x"] == f"Bearer {TOKEN}"
    assert api.seen["/landed/x"] == f"Bearer {TOKEN}"  # e.g. a renamed repository


def test_token_is_dropped_on_a_redirect_to_another_host(servers):
    api, foreign = servers
    _get(f"{api.base}/other/x")
    assert api.seen["/other/x"] == f"Bearer {TOKEN}"
    assert foreign.seen["/landed/x"] is None


def test_token_is_not_restored_after_a_hop_through_another_host(servers):
    api, foreign = servers
    _get(f"{api.base}/bounce/x")
    assert foreign.seen["/back/x"] is None
    assert api.seen["/returned/x"] is None


def test_no_token_means_no_authorization(servers):
    api, _foreign = servers
    _get(f"{api.base}/same/x", token=None)
    assert api.seen["/same/x"] is None and api.seen["/landed/x"] is None


def test_a_different_port_on_the_same_host_is_another_origin():
    assert github._origin("https://api.github.com/x") == github._origin(
        "https://API.github.com:443/y"
    )
    assert github._origin("https://api.github.com/x") != github._origin(
        "https://api.github.com:8443/x"
    )
    assert github._origin("https://api.github.com/x") != github._origin(
        "http://api.github.com/x"
    )


# ---------------------------------------------------------------------------
# Every call site, through its real request code.
# ---------------------------------------------------------------------------


def _tools_request(monkeypatch, api: _Server, path: str) -> None:
    from strandly.tools import github as gh

    gh._request("GET", f"{api.base}{path}", TOKEN)


def _tools_rest_call(monkeypatch, api: _Server, path: str) -> None:
    from strandly.tools import github as gh
    from strandly.tools import github_client

    monkeypatch.setattr(github_client, "GITHUB_REST_URL", api.base)
    gh._rest_call("GET", path, TOKEN)


def _poller_request(monkeypatch, api: _Server, path: str) -> None:
    from strandly.ops.lambdas.mention_poller import handler

    monkeypatch.setattr(handler, "GITHUB_API", api.base)
    handler._request("GET", f"{api.base}{path}", TOKEN)


def _audit_request(monkeypatch, api: _Server, path: str) -> None:
    from strandly.ops.lambdas.mention_poller import audit

    audit._request("GET", f"{api.base}{path}", TOKEN)


def _reaction(monkeypatch, api: _Server, path: str) -> None:
    from strandly.ops import github

    monkeypatch.setattr(github, "GITHUB_API", api.base)
    assert github.add_reaction(f"{api.base}{path}", TOKEN, "rocket") is True


CALL_SITES: dict[str, Callable[..., None]] = {
    "tools.github._request": _tools_request,
    "tools.github._rest_call": _tools_rest_call,
    "mention_poller.handler._request": _poller_request,
    "mention_poller.audit._request": _audit_request,
    "ops.github.add_reaction": _reaction,
}


@pytest.mark.parametrize("call", list(CALL_SITES.values()), ids=list(CALL_SITES))
def test_every_call_site_keeps_the_token_on_the_same_host(monkeypatch, servers, call):
    api, _foreign = servers
    call(monkeypatch, api, "/same/x")
    assert api.seen["/landed/x"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("call", list(CALL_SITES.values()), ids=list(CALL_SITES))
def test_every_call_site_drops_the_token_on_another_host(monkeypatch, servers, call):
    api, foreign = servers
    call(monkeypatch, api, "/other/x")
    assert api.seen["/other/x"] == f"Bearer {TOKEN}"
    assert "/landed/x" in foreign.seen and foreign.seen["/landed/x"] is None


def _capture_urlopen(monkeypatch, calls):
    def fake_urlopen(req, timeout=None):
        calls.append(req)
        return contextlib.nullcontext()

    monkeypatch.setattr(github._OPENER, "open", fake_urlopen)


def test_vocabulary_matches_poller_ack():
    # One lifecycle vocabulary: the poller's \U0001f440 must be the shared REACTION_STARTED, and the three
    # signals must be distinct valid GitHub reaction contents.
    assert poller.ACK_REACTION == github.REACTION_STARTED == "eyes"
    assert github.REACTION_COMPLETED == "rocket"
    assert github.REACTION_FAILED == "confused"
    assert (
        len({github.REACTION_STARTED, github.REACTION_COMPLETED, github.REACTION_FAILED})
        == 3
    )


def test_add_reaction_posts_content(monkeypatch):
    calls: list = []
    _capture_urlopen(monkeypatch, calls)
    ok = github.add_reaction(
        "https://api.github.com/repos/o/r/issues/comments/9/reactions", "tok", "rocket"
    )
    assert ok is True
    assert len(calls) == 1
    req = calls[0]
    assert req.get_method() == "POST"
    assert req.full_url == "https://api.github.com/repos/o/r/issues/comments/9/reactions"
    assert json.loads(req.data.decode()) == {"content": "rocket"}
    assert req.get_header("Authorization") == "Bearer tok"
    assert req.get_header("Content-type") == "application/json"


def test_add_reaction_noop_on_empty_url_or_token(monkeypatch):
    calls: list = []
    _capture_urlopen(monkeypatch, calls)
    assert github.add_reaction("", "tok", "rocket") is False
    assert github.add_reaction("https://api.github.com/x/reactions", "", "rocket") is False
    assert calls == []


def test_add_reaction_guards_non_github_url(monkeypatch):
    # A doctored reactions URL must never receive the token — and must not raise (fail-open).
    calls: list = []
    _capture_urlopen(monkeypatch, calls)
    ok = github.add_reaction("https://evil.example.com/reactions", "tok", "confused")
    assert ok is False
    assert calls == []  # never reached the network


def test_add_reaction_fail_open_on_network_error(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(github._OPENER, "open", boom)
    ok = github.add_reaction("https://api.github.com/x/reactions", "tok", "confused")
    assert ok is False  # logged + swallowed, never raises
