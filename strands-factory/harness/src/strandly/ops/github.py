"""Standard-library GitHub REST helpers shared by the poller Lambda and the runtime.

Every request that carries a token opens through :func:`open_github`, which re-attaches
``Authorization`` to a redirect only on the same scheme, host and port: urllib otherwise either
copies the header to every redirect (GitHub sends logs and artifacts to pre-signed blob-storage
URLs) or drops it on every redirect (including the same-host 301 for a renamed repository).

Mention runs signal their lifecycle as reactions on the mention (silent: no notification):
``eyes`` when dispatched, ``rocket`` on success, ``confused`` on terminal failure. A hard kill
leaves only ``eyes``; the run ledger stays the source of truth.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any
from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or _DEFAULT_PORTS.get(scheme)


class _SameOriginAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects like urllib, carrying ``Authorization`` only to the same origin."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        auth = req.get_header("Authorization")
        # urllib copies ordinary headers onto ``new``; never let one carry the token off-origin.
        new.headers.pop("Authorization", None)
        new.unredirected_hdrs.pop("Authorization", None)
        if auth and _origin(new.full_url) == _origin(req.full_url):
            new.add_unredirected_header("Authorization", auth)
        return new


_OPENER = urllib.request.build_opener(_SameOriginAuthRedirect)


def open_github(req: urllib.request.Request, token: str | None, *, timeout: float) -> Any:
    """Open ``req`` with ``Authorization: Bearer <token>`` (anonymous when ``token`` is empty).

    The caller still guards the initial URL; this only governs where the token goes after a
    redirect. Returns the response object (a context manager), like ``urllib.request.urlopen``.
    """
    if token:
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    return _OPENER.open(req, timeout=timeout)


logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
_USER_AGENT = "strandly-reactions/1.0"

# GitHub reaction ``content`` values (https://docs.github.com/rest/reactions).
REACTION_STARTED = "eyes"  # 👀 dispatch accepted — work has started
REACTION_COMPLETED = "rocket"  # 🚀 run finished cleanly
REACTION_FAILED = "confused"  # 😕 run failed terminally — don't expect a response


def is_github_api_url(url: str) -> bool:
    """Only ``https://api.github.com/...`` URLs are addressable.

    The reactions URL travels through API payloads and the dispatch payload; this guard ensures a
    doctored URL can never make us send the token to an arbitrary host.
    """
    return url.startswith(f"{GITHUB_API}/") or url == GITHUB_API


def add_reaction(reactions_url: str, token: str, content: str) -> bool:
    """POST one reaction onto ``reactions_url``; best-effort. True iff the POST succeeded.

    No-op (``False``) on an empty URL or token. Never raises: ``OSError`` (``URLError`` / a read
    timeout) and ``ValueError`` (the URL guard) are logged and swallowed — a failed reaction must
    never fail the run it narrates.
    """
    if not reactions_url or not token:
        return False
    try:
        if not is_github_api_url(reactions_url):
            raise ValueError(f"refusing to send token to non-GitHub-API URL: {reactions_url!r}")
        req = urllib.request.Request(
            reactions_url, data=json.dumps({"content": content}).encode(), method="POST"
        )
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", _USER_AGENT)
        req.add_header("Content-Type", "application/json")
        # The guard above covers the first hop; open_github keeps the token off any redirect host.
        with open_github(req, token, timeout=30):
            return True
    except (OSError, ValueError) as e:
        logger.warning("reaction %r failed for %s: %s", content, reactions_url, e)
        return False
