"""Who is using this client: an advisory GitHub login sent as ``X-Strandly-Person``.

The API takes the caller's person from the people-directory row bound to the signed-in Cognito
user. This login is advisory, not authentication: the API ignores one that is not the bound
person's.

``verify`` checks the login against GitHub from this machine (``GET /user`` with a token from
``gh auth token`` or ``$GITHUB_TOKEN``). The token goes only to GitHub: it is never stored, never
logged and never sent to the Strandly API. The cache (``identity.json`` next to the login cache,
``{github_login, verified: gh|manual, at}``) holds no secret.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from strandly_mcp.auth import HttpRequest, auth_path, http_request

PERSON_HEADER = "X-Strandly-Person"
IDENTITY_FILE = "identity.json"
VERIFIED_GH = "gh"
VERIFIED_MANUAL = "manual"
GITHUB_API_URL = "https://api.github.com"
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
GH_TIMEOUT_S = 10.0

# GitHub's login rule: alphanumerics and single inner hyphens, at most 39 characters. Mirrors
# ``strandly.ops.people._GITHUB_LOGIN_RE`` and the dashboard API's header check.
_GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

# ``DashboardClient(person=...)`` default: read the identity cached next to the login.
FROM_CACHE: Any = object()

TokenSource = Callable[[], "tuple[str, str] | None"]


class IdentityError(RuntimeError):
    """The identity could not be set or verified (the message is printed verbatim)."""


def valid_login(login: Any) -> bool:
    return isinstance(login, str) and bool(_GITHUB_LOGIN_RE.match(login))


def identity_path(auth_file: Path | None = None) -> Path:
    """The identity cache next to the login cache (``~/.strandly`` or a generated CLI's own)."""
    return (auth_file or auth_path()).with_name(IDENTITY_FILE)


def load_identity(path: Path | None = None) -> dict[str, Any] | None:
    """The cached identity, or ``None`` when absent, unreadable or not a valid login."""
    try:
        data = json.loads((path or identity_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not valid_login(data.get("github_login")):
        return None
    if data.get("verified") not in (VERIFIED_GH, VERIFIED_MANUAL):
        data["verified"] = VERIFIED_MANUAL
    return data


def save_identity(login: str, verified: str, path: Path | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    """Cache ``login`` (validated) with how it was established; returns the record."""
    if not valid_login(login):
        raise IdentityError(f"{login!r} is not a GitHub login")
    if verified not in (VERIFIED_GH, VERIFIED_MANUAL):
        raise IdentityError(f"unknown verification {verified!r}")
    record = {
        "github_login": login,
        "verified": verified,
        "at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    p = path or identity_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def clear_identity(path: Path | None = None) -> bool:
    try:
        (path or identity_path()).unlink()
        return True
    except FileNotFoundError:
        return False


def person_header(identity: dict[str, Any] | None) -> str | None:
    """The ``X-Strandly-Person`` value for a cached identity (``github:<login>``), else ``None``."""
    if not identity or not valid_login(identity.get("github_login")):
        return None
    return f"github:{identity['github_login']}"


def describe(identity: dict[str, Any] | None) -> str:
    """``@login (verified via gh 2026-11-15)`` / ``@login (set by hand)`` / ``not set``."""
    if not identity:
        return "not set"
    login = identity["github_login"]
    if identity.get("verified") == VERIFIED_GH:
        day = str(identity.get("at") or "")[:10] or "?"
        return f"@{login} (verified via gh {day})"
    return f"@{login} (set by hand)"


def github_token(
    run: Callable[..., Any] = subprocess.run, environ: dict[str, str] | None = None
) -> tuple[str, str] | None:
    """A GitHub token from ``gh auth token``, else ``$GITHUB_TOKEN``: ``(token, source)`` or ``None``."""
    try:
        done = run(["gh", "auth", "token"], capture_output=True, text=True, timeout=GH_TIMEOUT_S, check=False)
        token = (done.stdout or "").strip() if done.returncode == 0 else ""
        if token:
            return token, "gh auth token"
    except (OSError, subprocess.SubprocessError):
        pass
    token = (os.environ if environ is None else environ).get(GITHUB_TOKEN_ENV, "").strip()
    return (token, f"${GITHUB_TOKEN_ENV}") if token else None


def github_login_for(token: str, *, request: HttpRequest = http_request, api_url: str = GITHUB_API_URL) -> str:
    """The login that owns ``token`` (GitHub ``GET /user``). The token goes to GitHub only."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "strandly-cli",
    }
    try:
        status, raw = request("GET", api_url.rstrip("/") + "/user", headers=headers, timeout=15.0)
    except OSError as exc:
        raise IdentityError(f"could not reach GitHub: {exc}") from None
    if status != 200:
        raise IdentityError(f"GitHub refused the token (GET /user returned {status})")
    try:
        login = json.loads(raw).get("login")
    except (ValueError, AttributeError):
        login = None
    if not valid_login(login):
        raise IdentityError("GitHub GET /user returned no login")
    return str(login)


def verify(
    path: Path | None = None,
    *,
    token_source: TokenSource | None = None,
    request: HttpRequest = http_request,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve the login from a local GitHub token and cache it as ``verified: gh``."""
    found = (token_source or github_token)()
    if found is None:
        raise IdentityError(
            "no GitHub token: sign in with `gh auth login` or set $GITHUB_TOKEN "
            "(or set the login by hand with `identity --github <login>`)"
        )
    token, _source = found
    return save_identity(github_login_for(token, request=request), VERIFIED_GH, path, now=now)
