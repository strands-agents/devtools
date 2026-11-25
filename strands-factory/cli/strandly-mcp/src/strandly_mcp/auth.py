"""Cognito auth for the dashboard-API MCP transport — ``strandly-mcp login`` / ``logout``.

The deployed dashboard fronts the runtime with an HTTP API behind a **Cognito JWT authorizer**
(harness ``infra/stacks/dashboard_stack.py``): ``POST/GET /api/chat`` launch and poll runs with
the Lambda's own IAM role, so a caller needs a Cognito identity — never AWS credentials. The CLI
reuses that exact identity: ``login`` runs the same Hosted-UI Authorization-Code + PKCE flow the
maintainer portal uses (``dashboard/web-next/src/auth/cognito.ts``), with a local loopback HTTP
server standing in for the SPA redirect. Tokens are cached in ``~/.strandly/auth.json`` (0600);
the proxy silently refreshes the 1-hour access token off the long-lived refresh token, so login is
a rare event.

Deliberately **stdlib-only** (urllib + http.server + webbrowser): no ``requests``, no OAuth
library — this module runs in the thin ``strandly-mcp`` install (``mcp`` + stdlib, nothing else).
The harness's ``strandly login`` is a thin delegate to :func:`login_command`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from strandly_mcp import _build_info

logger = logging.getLogger(__name__)

# First-login configuration can come from flags, these env vars, or --portal-url discovery; once
# logged in the values persist inside auth.json so re-logins need nothing.
ENV_API_URL = "STRANDLY_API_URL"
ENV_COGNITO_DOMAIN = "STRANDLY_COGNITO_DOMAIN"
ENV_CLIENT_ID = "STRANDLY_COGNITO_CLIENT_ID"

# Refresh this long before the (1h) access-token expiry — mirrors the portal's ~1 min lead.
REFRESH_LEAD_S = 60

# Loopback redirect candidates, tried in order. Each must be registered on the app client: the
# Dashboard stack always registers 8765/callback and adds the Vite dev ports (5173, 5174) only in
# development deployments, so the CLI never depends on them.
LOGIN_REDIRECTS: tuple[tuple[int, str], ...] = ((8765, "http://localhost:8765/callback"),)

# Bound the wait for the browser round-trip — an abandoned login must not hang the terminal forever.
LOGIN_TIMEOUT_S = 300.0


class AuthError(RuntimeError):
    """A login/refresh problem the user can act on (message is printed verbatim)."""


LOGIN_HINT = "run `strandly-mcp login`"


class NotLoggedInError(AuthError):
    """No usable cached session — the fix is always the same: :data:`LOGIN_HINT`."""

    def __init__(self, message: str | None = None):
        super().__init__(message or f"not logged in — {LOGIN_HINT}")


def auth_path() -> Path:
    """Where the cached session lives (user-global, like ``runtime.json``)."""
    return Path.home() / ".strandly" / "auth.json"


# ---- HTTP seam -------------------------------------------------------------------------------

# Everything network goes through this one function so tests (and callers) can inject a fake.
HttpRequest = Callable[..., tuple[int, bytes]]


def http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 30.0,
) -> tuple[int, bytes]:
    """One urllib round-trip → ``(status, body)``. HTTP error statuses are returned, not raised.

    Only transport-level failures (DNS, refused connection, TLS) raise — as ``urllib.error.URLError``
    — because there is no status to report; callers wrap those with an actionable message.
    """
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — https URLs only
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---- token cache -----------------------------------------------------------------------------


def load_auth(path: Path | None = None) -> dict[str, Any] | None:
    """The cached session, or ``None`` when absent/unreadable (both mean: not logged in)."""
    p = path or auth_path()
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def save_auth(auth: dict[str, Any], path: Path | None = None) -> Path:
    """Persist the session at 0600 (it holds a long-lived refresh token — treat like a key)."""
    p = path or auth_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Open with 0600 from the first byte (O_CREAT honours ``mode``); a later chmod would leave a
    # window where the refresh token sits at the default umask permissions.
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(auth, indent=2) + "\n")
    p.chmod(0o600)  # an existing file keeps its old mode through O_CREAT — tighten it too
    return p


def clear_auth(path: Path | None = None) -> bool:
    """Delete the cached session; ``True`` if there was one."""
    p = path or auth_path()
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


# ---- OAuth calls (mirror dashboard/web-next/src/auth/cognito.ts) -------------------------------


def _token_endpoint(cognito_domain: str) -> str:
    return cognito_domain.rstrip("/") + "/oauth2/token"


def _post_form(
    url: str, fields: dict[str, str], request: HttpRequest = http_request
) -> tuple[int, bytes]:
    return request(
        "POST",
        url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=urlencode(fields).encode("ascii"),
    )


def exchange_code(
    cognito_domain: str,
    client_id: str,
    code: str,
    verifier: str,
    redirect_uri: str,
    *,
    request: HttpRequest = http_request,
) -> dict[str, Any]:
    """Swap the authorization code for tokens (public client — ``client_id`` only, NO secret)."""
    try:
        status, body = _post_form(
            _token_endpoint(cognito_domain),
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
            request,
        )
    except OSError as e:  # urllib.error.URLError subclasses OSError — DNS/refused/TLS
        raise AuthError(f"could not reach the Cognito token endpoint: {e}") from e
    if status != 200:
        raise AuthError(f"token exchange failed ({status}): {body[:300]!r}")
    return json.loads(body)


def refresh_auth(
    auth: dict[str, Any],
    *,
    request: HttpRequest = http_request,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """A fresh access token off the cached refresh token; the updated session dict is returned.

    A **rejected** refresh (4xx — expired/revoked refresh token) means the session is dead and the
    only fix is a new login, so it raises :class:`NotLoggedInError`; a 5xx or transport error is
    transient and raises plain :class:`AuthError` (the cached session stays valid to retry).
    """
    refresh_token = auth.get("refresh_token")
    if not refresh_token:
        raise NotLoggedInError()
    domain = str(auth.get("cognito_domain") or "")
    client_id = str(auth.get("client_id") or "")
    if not domain or not client_id:
        raise NotLoggedInError(f"cached login is incomplete — {LOGIN_HINT}")
    try:
        status, body = _post_form(
            _token_endpoint(domain),
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": str(refresh_token),
            },
            request,
        )
    except OSError as e:  # urllib.error.URLError subclasses OSError — DNS/refused/TLS
        raise AuthError(f"could not reach the Cognito token endpoint: {e}") from e
    if 400 <= status < 500:
        raise NotLoggedInError(
            f"the login session was rejected ({status}) — it likely expired; {LOGIN_HINT}"
        )
    if status != 200:
        raise AuthError(f"token refresh failed ({status}): {body[:300]!r}")
    data = json.loads(body)
    return {
        **auth,
        "access_token": data["access_token"],
        # Cognito does not re-issue these on a refresh grant — keep the cached ones.
        "id_token": data.get("id_token") or auth.get("id_token"),
        "refresh_token": data.get("refresh_token") or refresh_token,
        "expires_at": now() + float(data.get("expires_in") or 3600),
    }


def ensure_access_token(
    auth: dict[str, Any],
    *,
    request: HttpRequest = http_request,
    now: Callable[[], float] = time.time,
) -> tuple[str, dict[str, Any], bool]:
    """``(access_token, session, refreshed)`` — the cached token when fresh, else a silent refresh."""
    token = auth.get("access_token")
    expires_at = auth.get("expires_at")
    if token and isinstance(expires_at, (int, float)) and expires_at - now() > REFRESH_LEAD_S:
        return str(token), auth, False
    refreshed = refresh_auth(auth, request=request, now=now)
    return str(refreshed["access_token"]), refreshed, True


# ---- PKCE login (browser + loopback redirect) ---------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    """``(verifier, S256 challenge)`` — token_urlsafe emits only PKCE-unreserved characters."""
    verifier = secrets.token_urlsafe(64)  # ~86 chars, inside the RFC 7636 43–128 window
    return verifier, _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def build_authorize_url(
    cognito_domain: str, client_id: str, redirect_uri: str, challenge: str, state: str
) -> str:
    """The Hosted-UI ``/oauth2/authorize`` URL (same shape the portal builds)."""
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid email profile",
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
        }
    )
    return f"{cognito_domain.rstrip('/')}/oauth2/authorize?{query}"


_LANDING_HTML = (
    b"<!doctype html><meta charset='utf-8'><title>strandly login</title>"
    b"<body style='font-family:system-ui;margin:4rem auto;max-width:28rem;text-align:center'>"
    b"<h2>&#10003; Logged in to strandly</h2><p>You can close this tab and return to the terminal.</p>"
)


class _CallbackHandler(BaseHTTPRequestHandler):
    """Catches the ONE redirect (``?code=…&state=…``) on the expected path; 404s everything else."""

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        parsed = urlparse(self.path)
        expected = urlparse(getattr(self.server, "expected_path", "/")).path or "/"
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if parsed.path != expected or not ("code" in params or "error" in params):
            self.send_response(404)
            self.end_headers()
            return  # a favicon probe / stray request — keep waiting for the real redirect
        self.server.result = params  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_LANDING_HTML)

    def log_message(self, *args: Any) -> None:  # silence the default stderr access log
        pass


def _bind_loopback(redirects: tuple[tuple[int, str], ...]) -> tuple[HTTPServer, str]:
    """The first candidate port we can bind, as ``(server, redirect_uri)``.

    The redirect URI must be pre-registered on the app client, so the port cannot be arbitrary —
    when every candidate is taken we fail with the list
    rather than silently picking an unregistered port Cognito would reject anyway.
    """
    for port, redirect_uri in redirects:
        try:
            server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
        except OSError:
            continue
        server.expected_path = urlparse(redirect_uri).path or "/"  # type: ignore[attr-defined]
        server.result = None  # type: ignore[attr-defined]
        return server, redirect_uri
    ports = ", ".join(str(p) for p, _ in redirects)
    raise AuthError(f"no login callback port is free (tried {ports}) — stop what's using them and retry")


def login(
    *,
    api_url: str,
    cognito_domain: str,
    client_id: str,
    request: HttpRequest = http_request,
    open_browser: Callable[[str], Any] | None = None,
    redirects: tuple[tuple[int, str], ...] | None = None,
    timeout: float = LOGIN_TIMEOUT_S,
    path: Path | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """The browser PKCE flow: authorize → loopback redirect → code exchange → cached session."""
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    server, redirect_uri = _bind_loopback(redirects if redirects is not None else LOGIN_REDIRECTS)
    try:
        url = build_authorize_url(cognito_domain, client_id, redirect_uri, challenge, state)
        print("Opening the sign-in page in your browser…", file=sys.stderr)
        print(f"If it does not open, visit:\n  {url}", file=sys.stderr)
        if open_browser is None:
            import webbrowser

            open_browser = webbrowser.open
        open_browser(url)

        server.timeout = 1.0  # poll in 1s slices so the deadline check runs between requests
        deadline = now() + timeout
        while server.result is None:  # type: ignore[attr-defined]
            if now() > deadline:
                raise AuthError(f"timed out waiting for the browser sign-in ({int(timeout)}s)")
            server.handle_request()
        params: dict[str, str] = server.result  # type: ignore[attr-defined]
    finally:
        server.server_close()

    if "error" in params:
        detail = params.get("error_description") or params["error"]
        raise AuthError(f"sign-in was refused: {detail}")
    if params.get("state") != state:
        # A mismatched state is a forged/mixed-up redirect — never exchange its code.
        raise AuthError("sign-in redirect state mismatch — try again")

    tokens = exchange_code(
        cognito_domain, client_id, params["code"], verifier, redirect_uri, request=request
    )
    auth: dict[str, Any] = {
        "api_url": api_url.rstrip("/"),
        "cognito_domain": cognito_domain.rstrip("/"),
        "client_id": client_id,
        "access_token": tokens["access_token"],
        "id_token": tokens.get("id_token"),
        "refresh_token": tokens.get("refresh_token"),
        "expires_at": now() + float(tokens.get("expires_in") or 3600),
    }
    save_auth(auth, path)
    return auth


# ---- config discovery ----------------------------------------------------------------------------


def discover_from_portal(
    portal_url: str, *, request: HttpRequest = http_request
) -> dict[str, str]:
    """Read the API/Cognito coordinates from the deployed portal's public ``config.json``.

    The portal SPA boots from exactly this file (``dashboard_stack.py`` writes ``apiBase``,
    ``cognitoDomain`` and the client id into it), so pointing ``login --portal-url`` at the
    portal is a zero-copy-paste first-time setup. The stack spells the client id
    ``cognitoClientId`` for one portal and ``clientId`` for the other (prod serves the latter),
    so both are accepted.
    """
    url = portal_url.rstrip("/") + "/config.json"
    try:
        status, body = request("GET", url)
    except OSError as e:
        raise AuthError(f"could not fetch {url}: {e}") from e
    if status != 200:
        raise AuthError(f"could not fetch {url} (HTTP {status})")
    try:
        cfg = json.loads(body)
    except ValueError as e:
        raise AuthError(f"{url} is not valid JSON") from e
    out = {
        "api_url": str(cfg.get("apiBase") or ""),
        "cognito_domain": str(cfg.get("cognitoDomain") or ""),
        "client_id": str(cfg.get("cognitoClientId") or cfg.get("clientId") or ""),
    }
    if not all(out.values()):
        raise AuthError(f"{url} is missing apiBase/cognitoDomain/cognitoClientId (or clientId)")
    return out


# ---- CLI entry points ----------------------------------------------------------------------------


def login_command(
    *,
    api_url: str | None = None,
    cognito_domain: str | None = None,
    client_id: str | None = None,
    portal_url: str | None = None,
    request: HttpRequest = http_request,
    open_browser: Callable[[str], Any] | None = None,
    path: Path | None = None,
    default_portal_url: str | None = _build_info.PORTAL_URL,
) -> int:
    """``login`` — resolve the coordinates (flag > --portal-url > env > cached > baked), then PKCE.

    An explicit ``--portal-url`` outranks env/cache on purpose: the user is pointing at a specific
    deployment, so stale cached values must not win over it. The portal URL baked into a released
    wheel (:mod:`strandly_mcp._build_info`) is the *last* resort — used only when nothing else
    resolves, so it never overrides a deliberate choice.
    """
    cached = load_auth(path) or {}
    flags = {"api_url": api_url, "cognito_domain": cognito_domain, "client_id": client_id}
    env_keys = {"api_url": ENV_API_URL, "cognito_domain": ENV_COGNITO_DOMAIN, "client_id": ENV_CLIENT_ID}

    def resolve(discovered: dict[str, str]) -> dict[str, str | None]:
        return {
            k: flags[k] or discovered.get(k) or os.environ.get(env_keys[k]) or cached.get(k)
            for k in flags
        }

    resolved = resolve({})
    if not portal_url and default_portal_url and not all(resolved.values()):
        portal_url = default_portal_url
        print(f"login: using the portal this build was published from: {portal_url}", file=sys.stderr)
    if portal_url:
        try:
            resolved = resolve(discover_from_portal(portal_url, request=request))
        except AuthError as e:
            print(f"login: {e}", file=sys.stderr)
            return 1
    resolved_api = resolved["api_url"]
    resolved_domain = resolved["cognito_domain"]
    resolved_client = resolved["client_id"]
    missing = [
        flag
        for flag, value in (
            ("--api-url", resolved_api),
            ("--cognito-domain", resolved_domain),
            ("--client-id", resolved_client),
        )
        if not value
    ]
    if missing:
        print(
            f"login: missing {', '.join(missing)} — pass the flags, set "
            f"{ENV_API_URL}/{ENV_COGNITO_DOMAIN}/{ENV_CLIENT_ID}, or point --portal-url at the "
            "deployed portal to discover them (values persist after the first login)",
            file=sys.stderr,
        )
        return 1
    try:
        login(
            api_url=str(resolved_api),
            cognito_domain=str(resolved_domain),
            client_id=str(resolved_client),
            request=request,
            open_browser=open_browser,
            path=path,
        )
    except AuthError as e:
        print(f"login: {e}", file=sys.stderr)
        return 1
    print(f"✓ logged in — session cached in {path or auth_path()}")
    return 0


def logout_command(*, path: Path | None = None, request: HttpRequest = http_request) -> int:
    """``logout`` — best-effort revoke the refresh token, then drop the cache."""
    auth = load_auth(path)
    if auth and auth.get("refresh_token") and auth.get("cognito_domain") and auth.get("client_id"):
        try:
            _post_form(
                str(auth["cognito_domain"]).rstrip("/") + "/oauth2/revoke",
                {"token": str(auth["refresh_token"]), "client_id": str(auth["client_id"])},
                request,
            )
        except Exception:  # noqa: BLE001 — revocation is best-effort; the local delete is the point
            logger.debug("refresh-token revocation failed; deleting the local credentials anyway", exc_info=True)
    removed = clear_auth(path)
    print("✓ logged out" if removed else "already logged out")
    return 0
