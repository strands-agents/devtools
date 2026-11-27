"""``GitHubContextInjector``: enrich the GitHub threads a turn references into the model's input.

The block reaches the model through the SDK's ``ContextInjector`` with ``trigger="userTurn"``: one
``injected`` block on the user's ask, which the history keeps apart from the user's words and the
model reads only while that ask is the latest user message. The system prompt never changes, so its
prompt-cache prefix stays byte-stable across the turn's model calls. The plugin needs no token (see
:mod:`.fetch`), so it is always registered; ``use_github`` covers fetching other URLs mid-turn.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from strands.hooks import BeforeInvocationEvent
from strands.plugins import Plugin, hook
from strands.vended_plugins.context_injector import ContextInjector

from strandly.plugins.github_threads.fetch import (
    GraphQLFn,
    RestFn,
    build_github_context,
    parse_github_url,
)

if TYPE_CHECKING:
    from strands.agent.agent import Agent
    from strands.types.content import Message, Messages
    from strands.vended_plugins.context_injector import InjectionContext

    from strandly.core.config import GitHubSettings

logger = logging.getLogger(__name__)

# Scrape https://github.com/... URLs from free text. Trailing punctuation is stripped afterwards so
# a URL ending a sentence ("…/issues/12.") still parses.
_GITHUB_URL_RE = re.compile(r"https?://(?:www\.)?github\.com/[^\s<>()\[\]'\"`]+")
_TRAILING_PUNCT = ".,;:!?\"')]}>"

# The injected block is wrapped in this element so the model knows the content is reference context.
_OPEN = "<github-context>"
_CLOSE = "</github-context>"
_PREAMBLE = (
    "The following GitHub thread(s) referenced this turn have been enriched for you "
    "(body + comments + reviews/review-threads + linked items + replies). This is reference "
    "context for this turn, not part of the user's message."
)


class GitHubContextInjector(Plugin):
    """Adds enriched GitHub-thread context to the user's ask as an injected block."""

    name = "github-context-injector"

    def __init__(
        self,
        gh: GitHubSettings,
        *,
        event: dict[str, Any] | None = None,
        max_urls: int = 5,
        graphql: GraphQLFn | None = None,
        rest: RestFn | None = None,
        token: str | None = None,
    ) -> None:
        """Initialize the injector.

        Args:
            gh: The harness's GitHub settings (token env names; reused for token resolution).
            event: The per-invocation ``ctx.event`` payload, if any — read for an explicit
                ``githubUrls`` key.
            max_urls: Cap on enriched URLs per turn (dedup happens first). Default 5.
            graphql: GraphQL network seam override (``github._graphql``-shaped). Defaults to the
                real one; tests inject a fake to stay hermetic.
            rest: REST network seam override (``github._rest_get``-shaped) used by the anonymous
                (no-token) fallback. Defaults to the real one; tests inject a fake.
            token: Explicit token override. Defaults to ``None`` → resolved from the environment via
                ``github._get_token`` at render time. When no token resolves, enrichment falls back
                to the anonymous REST path (public issues/PRs); it is **not** required.
        """
        self._gh = gh
        self._event = event if isinstance(event, dict) else None
        self._max_urls = max(1, int(max_urls))
        self._graphql = graphql
        self._rest = rest
        self._token = token
        # The invocation state of the call in flight (``InjectionContext`` carries none), for the
        # payload's ``githubUrls``. Safe as instance state: turns on one session are serialized
        # under the session lock (``serve/turn.py``), so exactly one call reads it at a time.
        self._invocation_state: dict[str, Any] | None = None
        # ``userTurn``: the block belongs on the ask, never on a tool-result message, where it would
        # add a lasting copy per tool round. The injector writes at most one block per message
        # under its name, so a retried model call neither writes nor fetches again.
        self._injector = ContextInjector(self._render_for, name=f"{self.name}:injector", trigger="userTurn")
        super().__init__()

    def init_agent(self, agent: Agent) -> None:
        """Register the composed context injector's middleware on this agent."""
        self._injector.init_agent(agent)

    @hook  # type: ignore[call-overload]  # SDK hook() overloads don't model bound (self, event) methods
    def _on_invocation(self, event: BeforeInvocationEvent) -> None:
        self._invocation_state = event.invocation_state

    def _render_for(self, context: InjectionContext) -> str | None:
        """The wrapped block for the ask in ``context``, or ``None`` (the injector skips on empty)."""
        return self._render(context.messages, self._invocation_state)

    # ---- rendering (reuses the pure core, fail-soft) -----------------------------------------

    def _render(self, messages: Messages, invocation_state: dict[str, Any] | None) -> str | None:
        """Collect URLs + enrich them into a wrapped block. Fail-soft: errors become a short note."""
        try:
            urls = self._collect_urls(messages, invocation_state)
            if not urls:
                return None
            # Token-optional: with a token we enrich fully via GraphQL; without one the pure core
            # falls back to the anonymous REST API for public issues/PRs (discussions are
            # GraphQL-only → a short "needs a token" note). A token is used when present, never
            # required.
            token = self._token if self._token is not None else self._resolve_token()
            text = build_github_context(
                urls,
                token=token or "",
                graphql=self._resolve_graphql(),
                rest=self._resolve_rest(),
            )
            if not text:
                return None
            return self._wrap(text)
        except Exception as e:  # noqa: BLE001 — enrichment must never crash the turn
            logger.warning("github context injection failed: %s", e)
            return self._wrap(f"⚠️ GitHub context could not be enriched ({type(e).__name__}).")

    def _collect_urls(self, messages: Messages, invocation_state: dict[str, Any] | None) -> list[str]:
        """Explicit payload URLs first, then URLs scraped from the latest user message.

        Validated with :func:`parse_github_url`, de-duplicated per thread (kind/owner/repo/number),
        and capped at ``max_urls`` (first occurrence wins, so a fragment-bearing explicit URL is
        preferred over a later bare mention of the same thread).
        """
        candidates = [*self._explicit_urls(invocation_state), *_scrape_user_message(messages)]
        seen: set[tuple[str, str, str, int]] = set()
        out: list[str] = []
        for raw in candidates:
            ref = parse_github_url(raw)
            if ref is None:
                continue
            key = (ref.kind, ref.owner, ref.repo, ref.number)
            if key in seen:
                continue
            seen.add(key)
            out.append(raw.strip())
            if len(out) >= self._max_urls:
                break
        return out

    def _explicit_urls(self, invocation_state: dict[str, Any] | None) -> list[str]:
        """Pull a ``githubUrls`` (str or list) from the invoke payload and/or ``ctx.event``."""
        out: list[str] = []
        for source in (self._event, invocation_state):
            if not isinstance(source, dict):
                continue
            value = source.get("githubUrls")
            if isinstance(value, str):
                out.append(value)
            elif isinstance(value, (list, tuple)):
                out.extend(str(v) for v in value if v)
        return out

    # ---- seams (lazy so tests can monkeypatch github._graphql) -------------------------------

    def _resolve_graphql(self) -> GraphQLFn:
        if self._graphql is not None:
            return self._graphql
        from strandly.tools.github import _graphql

        return _graphql

    def _resolve_rest(self) -> RestFn:
        if self._rest is not None:
            return self._rest
        from strandly.tools.github import _rest_get

        return _rest_get

    def _resolve_token(self) -> str | None:
        from strandly.tools.github import _get_token

        return _get_token(self._gh, False)

    @staticmethod
    def _wrap(text: str) -> str:
        return f"{_OPEN}\n{_PREAMBLE}\n\n{text}\n{_CLOSE}"


def _scrape_user_message(messages: Messages) -> list[str]:
    """Scrape GitHub URLs from the latest user message's text (trailing punctuation trimmed)."""
    text = ""
    for message in reversed(messages):
        if message.get("role") == "user":
            text = _message_text(message)
            break
    if not text:
        return []
    return [m.rstrip(_TRAILING_PUNCT) for m in _GITHUB_URL_RE.findall(text)]


def _message_text(message: Message) -> str:
    """Join the text of a message's content blocks (ignoring tool-use/result blocks)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return "\n".join(parts)
