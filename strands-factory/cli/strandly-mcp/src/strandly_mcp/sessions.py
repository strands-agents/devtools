"""Session ids — how a conversation with the deployed agent is named and shared.

The deployed Strandly keys every conversation on the GitHub item it happens on::

    gh-<owner>-<repo>-<h8>-<kind>-<number>      kind in {issue, pr, disc}

lowercased, where ``h8`` is the first 8 hex characters of the SHA-256 of the lowercased
``owner/repo``, so two repositories whose hyphenated names read the same still get different ids.

The mention poller, GitHub Actions (``strandly invoke``), the dashboard chat and this MCP proxy
all derive the same id for the same item, so a local agent can *join* that thread by passing the
item's URL as ``session``. The scheme itself lives in the vendored ``_session_primitives``.
"""

from __future__ import annotations

import re
import uuid
from urllib.parse import urlparse

from strandly_mcp._session_primitives import (
    KIND_DISCUSSION,
    KIND_ISSUE,
    KIND_PR,
    canonical_session_id,
)

# GitHub URL path segment → canonical kind.
_URL_KINDS = {"issues": KIND_ISSUE, "pull": KIND_PR, "discussions": KIND_DISCUSSION}

# /<owner>/<repo>/(issues|pull|discussions)/<number>[/anything]  — a trailing sub-page such as
# `/pull/1951/files` still names the same item.
_GITHUB_PATH = re.compile(r"^/([^/]+)/([^/]+)/(issues|pull|discussions)/(\d+)(?:/.*)?$")

GITHUB_URL_SHAPE = "https://github.com/<owner>/<repo>/(issues|pull|discussions)/<number>"


def _github_url_session_id(value: str) -> str | None:
    """The thread id for a GitHub item URL; ``None`` when ``value`` is not a github.com URL.

    Tolerates ``www.``, a missing scheme, a trailing slash, ``?query`` and ``#fragment``. A
    github.com URL that does not name an issue / PR / discussion raises ``ValueError``.
    """
    candidate = value if "://" in value else f"https://{value}"
    url = urlparse(candidate)
    host = url.netloc.lower().removeprefix("www.")
    if host != "github.com":
        return None
    match = _GITHUB_PATH.match(url.path.rstrip("/") or "/")
    if match is None or int(match[4]) <= 0:
        raise ValueError(f"not a GitHub issue / PR / discussion URL: {value!r} — expected {GITHUB_URL_SHAPE}")
    owner, repo, segment, number = match.groups()
    return canonical_session_id(f"{owner}/{repo}", _URL_KINDS[segment], int(number))


def resolve_session(value: str | None, default: str) -> str:
    """The thread a tool call joins, from the caller's ``session`` argument.

    - ``None`` / blank → ``default`` (this process's private thread);
    - a GitHub issue / PR / discussion URL → that item's canonical ``gh-…`` id;
    - anything else → verbatim (a named thread, or an id copied from ``session()`` output).
    """
    text = (value or "").strip()
    if not text:
        return default
    return _github_url_session_id(text) or text


def default_session_id() -> str:
    """A fresh private ``mcp-…`` id — what tools use when the caller passes no ``session``."""
    return f"mcp-{uuid.uuid4().hex[:12]}"
