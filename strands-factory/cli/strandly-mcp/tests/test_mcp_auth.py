"""The Cognito login/token-cache seam behind the default `serve` transport (strandly_mcp/auth.py).

Everything network is faked through the ``request`` seam (``(status, body)`` tuples); the one
end-to-end login test runs a real loopback HTTP server on an ephemeral port with a thread standing
in for the browser redirect — no Cognito, no AWS, no sockets beyond 127.0.0.1.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest
from _home_isolation import _isolated_home  # noqa: F401  (autouse: tmp HOME)

from strandly_mcp import auth


def _token_response(**overrides):
    data = {"access_token": "at-new", "expires_in": 3600, "token_type": "Bearer"}
    data.update(overrides)
    return 200, json.dumps(data).encode()


# ---- cache file ---------------------------------------------------------------------------------


def test_save_and_load_roundtrip_at_0600(tmp_path):
    path = tmp_path / "nested" / "auth.json"  # parent dirs are created
    auth.save_auth({"access_token": "at", "api_url": "https://api"}, path)
    assert (path.stat().st_mode & 0o777) == 0o600  # holds a refresh token — key-file perms
    assert auth.load_auth(path) == {"access_token": "at", "api_url": "https://api"}


def test_load_missing_or_corrupt_means_not_logged_in(tmp_path):
    assert auth.load_auth(tmp_path / "absent.json") is None
    corrupt = tmp_path / "auth.json"
    corrupt.write_text("{not json")
    assert auth.load_auth(corrupt) is None
    not_a_dict = tmp_path / "list.json"
    not_a_dict.write_text("[1]")
    assert auth.load_auth(not_a_dict) is None


def test_clear_auth_reports_whether_a_session_existed(tmp_path):
    path = tmp_path / "auth.json"
    assert auth.clear_auth(path) is False
    auth.save_auth({}, path)
    assert auth.clear_auth(path) is True and not path.exists()


# ---- token refresh ------------------------------------------------------------------------------

_SESSION = {
    "api_url": "https://api.example.com",
    "cognito_domain": "https://pool.auth.us-west-2.amazoncognito.com",
    "client_id": "client-1",
    "access_token": "at-cached",
    "refresh_token": "rt-1",
    "expires_at": 10_000.0,
}


def test_ensure_access_token_uses_the_cache_when_fresh():
    calls = []
    token, session, refreshed = auth.ensure_access_token(
        dict(_SESSION), request=lambda *a, **k: calls.append(a) or (500, b""), now=lambda: 1000.0
    )
    assert token == "at-cached" and refreshed is False and not calls  # zero HTTP


def test_ensure_access_token_refreshes_near_expiry():
    captured = {}

    def fake(method, url, *, headers=None, data=None, **kw):
        captured.update(method=method, url=url, fields=parse_qs(data.decode()))
        return _token_response()

    # 30s to expiry < the 60s lead → refresh.
    token, session, refreshed = auth.ensure_access_token(
        dict(_SESSION), request=fake, now=lambda: 9_970.0
    )
    assert token == "at-new" and refreshed is True
    assert captured["url"] == "https://pool.auth.us-west-2.amazoncognito.com/oauth2/token"
    assert captured["fields"]["grant_type"] == ["refresh_token"]
    assert captured["fields"]["client_id"] == ["client-1"]
    assert captured["fields"]["refresh_token"] == ["rt-1"]
    assert "client_secret" not in captured["fields"]  # public client — never a secret
    # Cognito doesn't re-issue the refresh token on this grant — the cached one is kept.
    assert session["refresh_token"] == "rt-1"
    assert session["expires_at"] == 9_970.0 + 3600


def test_refresh_rejection_is_a_not_logged_in_error():
    with pytest.raises(auth.NotLoggedInError, match="strandly-mcp login"):
        auth.refresh_auth(dict(_SESSION), request=lambda *a, **k: (400, b'{"error":"invalid_grant"}'))


def test_refresh_5xx_is_transient_not_a_logout():
    with pytest.raises(auth.AuthError) as e:
        auth.refresh_auth(dict(_SESSION), request=lambda *a, **k: (502, b"bad gateway"))
    assert not isinstance(e.value, auth.NotLoggedInError)  # the cached session stays usable


def test_refresh_without_a_refresh_token_says_run_login():
    with pytest.raises(auth.NotLoggedInError, match="strandly-mcp login"):
        auth.refresh_auth({**_SESSION, "refresh_token": None}, request=lambda *a, **k: (200, b"{}"))


# ---- PKCE plumbing ------------------------------------------------------------------------------


def test_authorize_url_carries_the_s256_challenge():
    url = auth.build_authorize_url(
        "https://pool.auth.us-west-2.amazoncognito.com/",
        "client-1",
        "http://localhost:5173/",
        "chal-abc",
        "state-xyz",
    )
    parsed = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert url.startswith("https://pool.auth.us-west-2.amazoncognito.com/oauth2/authorize?")
    assert q == {
        "response_type": "code",
        "client_id": "client-1",
        "redirect_uri": "http://localhost:5173/",
        "scope": "openid email profile",
        "code_challenge_method": "S256",
        "code_challenge": "chal-abc",
        "state": "state-xyz",
    }


def test_pkce_pair_is_rfc7636_shaped():
    verifier, challenge = auth.pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert challenge and "=" not in challenge and "+" not in challenge and "/" not in challenge


def test_login_uses_only_the_dedicated_loopback_redirect():
    # Production app clients register no Vite dev port, so the CLI must never redirect to one.
    assert auth.LOGIN_REDIRECTS == ((8765, "http://localhost:8765/callback"),)


def test_exchange_code_wraps_a_transport_failure_in_an_actionable_error():
    # A DNS/refused/TLS failure during the code exchange must surface as AuthError (which
    # login_command prints verbatim), not as a raw URLError traceback in the user's terminal —
    # the same wrapping refresh_auth and discover_from_portal already do.
    import urllib.error

    def down(method, url, *, headers=None, data=None, **kw):
        raise urllib.error.URLError("connection refused")

    with pytest.raises(auth.AuthError, match="token endpoint"):
        auth.exchange_code(
            "https://pool.example.com", "c-1", "code", "verif", "http://localhost:5173/", request=down
        )


def test_exchange_code_posts_the_authorization_code_grant():
    captured = {}

    def fake(method, url, *, headers=None, data=None, **kw):
        captured.update(url=url, fields=parse_qs(data.decode()))
        return _token_response(refresh_token="rt-new", id_token="idt")

    tokens = auth.exchange_code(
        "https://pool.example.com", "client-1", "code-9", "verif", "http://localhost:5173/", request=fake
    )
    assert tokens["access_token"] == "at-new" and tokens["refresh_token"] == "rt-new"
    assert captured["fields"]["grant_type"] == ["authorization_code"]
    assert captured["fields"]["code"] == ["code-9"]
    assert captured["fields"]["code_verifier"] == ["verif"]
    assert "client_secret" not in captured["fields"]


# ---- the login flow (real loopback server, fake browser + token endpoint) ------------------------


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fake_browser_hitting(expect_state_holder: dict):
    """A "browser" that follows the authorize URL by redirecting straight back to the loopback."""

    def open_browser(url: str) -> None:
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        expect_state_holder.update(q)
        redirect = q["redirect_uri"] + "?" + f"code=code-42&state={q['state']}"

        def hit():
            urllib.request.urlopen(redirect, timeout=10).read()

        threading.Thread(target=hit, daemon=True).start()

    return open_browser


def test_login_end_to_end_caches_the_session(tmp_path):
    port = _free_port()
    path = tmp_path / "auth.json"
    seen_authorize: dict = {}
    exchanged = {}

    def fake_request(method, url, *, headers=None, data=None, **kw):
        exchanged.update(url=url, fields=parse_qs(data.decode()))
        return _token_response(refresh_token="rt-42", id_token="idt-42")

    session = auth.login(
        api_url="https://api.example.com/",
        cognito_domain="https://pool.example.com",
        client_id="client-1",
        request=fake_request,
        open_browser=_fake_browser_hitting(seen_authorize),
        redirects=((port, f"http://127.0.0.1:{port}/"),),
        timeout=30,
        path=path,
        now=lambda: 5_000.0,
    )
    # The exchanged code + verifier round-tripped through the loopback redirect.
    assert exchanged["fields"]["code"] == ["code-42"]
    assert seen_authorize["code_challenge_method"] == "S256"
    # Session persisted at 0600 with the coordinates for silent refresh + the API base.
    assert session["refresh_token"] == "rt-42" and session["api_url"] == "https://api.example.com"
    assert session["expires_at"] == 5_000.0 + 3600
    assert auth.load_auth(path) == session
    assert (path.stat().st_mode & 0o777) == 0o600


def test_login_rejects_a_state_mismatch(tmp_path):
    port = _free_port()

    def evil_browser(url: str) -> None:
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        redirect = q["redirect_uri"] + "?code=stolen&state=WRONG"
        threading.Thread(
            target=lambda: urllib.request.urlopen(redirect, timeout=10).read(), daemon=True
        ).start()

    with pytest.raises(auth.AuthError, match="state mismatch"):
        auth.login(
            api_url="https://api",
            cognito_domain="https://pool.example.com",
            client_id="c",
            request=lambda *a, **k: pytest.fail("must never exchange a mismatched-state code"),
            open_browser=evil_browser,
            redirects=((port, f"http://127.0.0.1:{port}/"),),
            timeout=30,
            path=tmp_path / "auth.json",
        )


def test_login_surfaces_a_denied_signin(tmp_path):
    port = _free_port()

    def denying_browser(url: str) -> None:
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        redirect = q["redirect_uri"] + "?error=access_denied&error_description=nope"
        threading.Thread(
            target=lambda: urllib.request.urlopen(redirect, timeout=10).read(), daemon=True
        ).start()

    with pytest.raises(auth.AuthError, match="nope"):
        auth.login(
            api_url="https://api",
            cognito_domain="https://pool.example.com",
            client_id="c",
            open_browser=denying_browser,
            redirects=((port, f"http://127.0.0.1:{port}/"),),
            timeout=30,
            path=tmp_path / "auth.json",
        )


def test_bind_loopback_falls_past_a_taken_port():
    import socket

    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken_port = taken.getsockname()[1]
    free = _free_port()
    try:
        server, redirect = auth._bind_loopback(
            (
                (taken_port, f"http://127.0.0.1:{taken_port}/"),
                (free, f"http://127.0.0.1:{free}/callback"),
            )
        )
    finally:
        taken.close()
    try:
        assert redirect.endswith(f":{free}/callback")
        assert server.expected_path == "/callback"
    finally:
        server.server_close()


def test_bind_loopback_reports_all_taken_ports():
    import socket

    s1 = socket.socket()
    s1.bind(("127.0.0.1", 0))
    port = s1.getsockname()[1]
    try:
        with pytest.raises(auth.AuthError, match=str(port)):
            auth._bind_loopback(((port, f"http://127.0.0.1:{port}/"),))
    finally:
        s1.close()


# ---- portal discovery + CLI commands --------------------------------------------------------------


def test_discover_from_portal_reads_config_json():
    cfg = {"apiBase": "https://api.example.com", "cognitoDomain": "https://pool", "cognitoClientId": "c-1"}

    def fake(method, url, **kw):
        assert method == "GET" and url == "https://portal.example.com/config.json"
        return 200, json.dumps(cfg).encode()

    assert auth.discover_from_portal("https://portal.example.com/", request=fake) == {
        "api_url": "https://api.example.com",
        "cognito_domain": "https://pool",
        "client_id": "c-1",
    }


def test_discover_from_portal_accepts_the_clientId_spelling():
    # What the prod portal actually serves (dashboard_stack.py's second config.json writer).
    cfg = {"apiBase": "https://api", "region": "us-west-2", "clientId": "c-2", "cognitoDomain": "https://pool"}
    got = auth.discover_from_portal("https://portal", request=lambda *a, **k: (200, json.dumps(cfg).encode()))
    assert got == {"api_url": "https://api", "cognito_domain": "https://pool", "client_id": "c-2"}


def test_discover_from_portal_rejects_an_incomplete_config():
    with pytest.raises(auth.AuthError, match="missing"):
        auth.discover_from_portal(
            "https://portal", request=lambda *a, **k: (200, b'{"apiBase": "https://api"}')
        )


def test_login_command_without_coordinates_is_actionable(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(auth.ENV_API_URL, raising=False)
    monkeypatch.delenv(auth.ENV_COGNITO_DOMAIN, raising=False)
    monkeypatch.delenv(auth.ENV_CLIENT_ID, raising=False)
    # default_portal_url=None: a source checkout / a wheel built without a portal baked in.
    assert auth.login_command(path=tmp_path / "auth.json", default_portal_url=None) == 1
    err = capsys.readouterr().err
    assert "--api-url" in err and "--portal-url" in err and auth.ENV_API_URL in err


def test_login_command_resolves_from_env_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv(auth.ENV_API_URL, "https://api.example.com")
    monkeypatch.setenv(auth.ENV_COGNITO_DOMAIN, "https://pool.example.com")
    monkeypatch.setenv(auth.ENV_CLIENT_ID, "client-env")
    path = tmp_path / "auth.json"
    port = _free_port()
    holder: dict = {}
    monkeypatch.setattr(auth, "LOGIN_REDIRECTS", ((port, f"http://127.0.0.1:{port}/"),))

    rc = auth.login_command(
        request=lambda *a, **k: _token_response(refresh_token="rt"),
        open_browser=_fake_browser_hitting(holder),
        path=path,
    )
    assert rc == 0
    cached = auth.load_auth(path)
    assert cached["client_id"] == "client-env" and cached["api_url"] == "https://api.example.com"


def test_login_command_cached_coordinates_survive_a_relogin(tmp_path, monkeypatch):
    # First login persisted the coordinates; a later bare `login` reuses them.
    monkeypatch.delenv(auth.ENV_API_URL, raising=False)
    monkeypatch.delenv(auth.ENV_COGNITO_DOMAIN, raising=False)
    monkeypatch.delenv(auth.ENV_CLIENT_ID, raising=False)
    path = tmp_path / "auth.json"
    auth.save_auth(
        {"api_url": "https://api.old", "cognito_domain": "https://pool.old", "client_id": "c-old"},
        path,
    )
    port = _free_port()
    monkeypatch.setattr(auth, "LOGIN_REDIRECTS", ((port, f"http://127.0.0.1:{port}/"),))
    holder: dict = {}
    rc = auth.login_command(
        request=lambda *a, **k: _token_response(refresh_token="rt"),
        open_browser=_fake_browser_hitting(holder),
        path=path,
    )
    assert rc == 0
    assert holder["client_id"] == "c-old"  # the cached coordinates drove the authorize URL


def test_logout_revokes_best_effort_and_clears(tmp_path, capsys):
    path = tmp_path / "auth.json"
    auth.save_auth(
        {
            "cognito_domain": "https://pool.example.com",
            "client_id": "c-1",
            "refresh_token": "rt-1",
        },
        path,
    )
    revoked = {}

    def fake(method, url, *, headers=None, data=None, **kw):
        revoked.update(url=url, fields=parse_qs(data.decode()))
        return 200, b""

    assert auth.logout_command(path=path, request=fake) == 0
    assert revoked["url"] == "https://pool.example.com/oauth2/revoke"
    assert revoked["fields"]["token"] == ["rt-1"]
    assert not path.exists()


def test_logout_is_fine_when_never_logged_in(tmp_path, capsys):
    assert auth.logout_command(path=tmp_path / "auth.json") == 0
    assert "already logged out" in capsys.readouterr().out


# ---- the baked portal default (_build_info.PORTAL_URL, written at release-build time) ----------


def _portal_config_then_tokens(seen: list):
    """A request fake: the portal's config.json, then token endpoint responses."""

    def fake(method, url, *, headers=None, data=None, **kw):
        seen.append(url)
        if url.endswith("/config.json"):
            body = {
                "apiBase": "https://api.baked",
                "cognitoDomain": "https://pool.baked",
                "cognitoClientId": "c-baked",
            }
            return 200, json.dumps(body).encode()
        return _token_response(refresh_token="rt")

    return fake


def test_login_command_falls_back_to_the_baked_portal(tmp_path, monkeypatch):
    for key in (auth.ENV_API_URL, auth.ENV_COGNITO_DOMAIN, auth.ENV_CLIENT_ID):
        monkeypatch.delenv(key, raising=False)
    port = _free_port()
    monkeypatch.setattr(auth, "LOGIN_REDIRECTS", ((port, f"http://127.0.0.1:{port}/"),))
    seen: list = []
    holder: dict = {}
    rc = auth.login_command(
        request=_portal_config_then_tokens(seen),
        open_browser=_fake_browser_hitting(holder),
        path=tmp_path / "auth.json",
        default_portal_url="https://portal.baked/",
    )
    assert rc == 0
    assert seen[0] == "https://portal.baked/config.json"  # discovered from the baked portal
    assert holder["client_id"] == "c-baked"


def test_baked_portal_never_overrides_explicit_or_cached_coordinates(tmp_path, monkeypatch):
    # Everything resolves from the cache → the baked portal is not even consulted.
    for key in (auth.ENV_API_URL, auth.ENV_COGNITO_DOMAIN, auth.ENV_CLIENT_ID):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "auth.json"
    auth.save_auth(
        {"api_url": "https://api.old", "cognito_domain": "https://pool.old", "client_id": "c-old"},
        path,
    )
    port = _free_port()
    monkeypatch.setattr(auth, "LOGIN_REDIRECTS", ((port, f"http://127.0.0.1:{port}/"),))
    seen: list = []
    holder: dict = {}
    rc = auth.login_command(
        request=_portal_config_then_tokens(seen),
        open_browser=_fake_browser_hitting(holder),
        path=path,
        default_portal_url="https://portal.baked",
    )
    assert rc == 0
    assert not any(u.endswith("/config.json") for u in seen)
    assert holder["client_id"] == "c-old"


def test_explicit_portal_url_outranks_the_baked_one(tmp_path, monkeypatch):
    seen: list = []
    port = _free_port()
    monkeypatch.setattr(auth, "LOGIN_REDIRECTS", ((port, f"http://127.0.0.1:{port}/"),))
    rc = auth.login_command(
        portal_url="https://portal.mine",
        request=_portal_config_then_tokens(seen),
        open_browser=_fake_browser_hitting({}),
        path=tmp_path / "auth.json",
        default_portal_url="https://portal.baked",
    )
    assert rc == 0
    assert seen[0] == "https://portal.mine/config.json"


def test_save_auth_tightens_an_existing_loose_file(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text("{}")
    path.chmod(0o644)
    auth.save_auth({"access_token": "at"}, path)
    assert (path.stat().st_mode & 0o777) == 0o600
