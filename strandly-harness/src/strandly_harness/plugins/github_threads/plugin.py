"""``GitHubContextInjector`` — auto-enrich GitHub URLs into the model's input, **ephemerally**.

Why a plugin (and not the tool)
-------------------------------
The deployed runtime / mention poller hands the agent only a ``prompt`` + a *parent* issue/PR link.
The thread itself — comments, reviews, review threads with file:line, linked issues, discussion
replies — is missing. Rather than make the agent *call a tool* to pull that thread (issue #346's
first cut shipped ``inject_github_context`` as a settings-aware tool), this plugin does it
**automatically** at the turn boundary, mirroring the TS ``ContextInjector`` vended plugin
(``strands-ts/src/vended-plugins/context-injector/plugin.ts``).

The TS plugin registers ``createInjectionMiddleware`` on ``InvokeModelStage.Input``: middleware
wraps the model call so the injected text augments *that one model call's input* and never persists
into the durable conversation/session, gated by a ``trigger`` (``userTurn`` default / ``everyTurn``).
The Python SDK has **no middleware**, so we reconstruct ephemeral injection with **two hooks**:

- ``BeforeModelCallEvent`` → render the enriched GitHub block and **append it to the system
  prompt** (mirroring :meth:`SystemPromptSkills.reinject`'s ``system_prompt_content`` / string
  handling).
- ``AfterModelCallEvent`` → **strip the exact injected block back out** so it never lands in the
  durable system prompt / session. The before-hook also defensively strips any stale block first,
  so even if an after-hook is skipped (it still fires on model exception/retry, but belt-and-braces)
  the block can never accumulate or duplicate — the same self-cleaning, rebuild-by-exact-match
  discipline :meth:`SystemPromptSkills.reinject` uses for its one-hook variant.

Surface swap, not a rewrite: the enrichment itself is the pure
:func:`strandly_harness.plugins.github_threads.fetch.build_github_context` core, **reused unchanged** (same
parser, per-kind GraphQL queries, deep-link markers, truncation, per-URL fail-soft). The network
seams (``github._graphql`` + ``github._rest_get``) and token resolution (``github._get_token``) are
the same ones the tool uses, so the suite stays network-free.

Token-optional (issue #346 owner feedback: "why do we require a github token to see a public
issue/pr/discussion?"). A token is **used when present** (full GraphQL enrichment) but **not
required**: with no token the pure core falls back to GitHub's anonymous REST API for public
issues/PRs, and discussions (GraphQL-only) inject a short "needs a token" note. The plugin is
therefore registered unconditionally — not gated on a token.

Trigger policy
--------------
Default ``userTurn``: inject **once per user turn**, on the first model call of the invocation —
not on every tool-loop model call (the agent already saw the enriched thread; re-injecting each
loop would just burn tokens). ``everyTurn`` re-injects on every model call. Either way the rendered
block is **cached per turn** in ``agent.state`` so the network enrichment runs at most once per
turn even across multiple model calls. The cache is reset at ``BeforeInvocationEvent``.

URL sources
-----------
1. Explicit ``githubUrls`` from the invoke payload (``invocation_state``) and/or the per-invocation
   ``ctx.event`` passed at construction.
2. GitHub URLs scraped from the latest user message.

URLs are validated with :func:`parse_github_url`, de-duplicated per thread, and capped
(``max_urls``, default 5). Everything is fail-soft: a bad URL becomes a short note (via the pure
core) and any unexpected error renders a short note instead of crashing the turn.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from strands.hooks import (
    AfterModelCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
)
from strands.plugins import Plugin, hook

from strandly_harness.plugins.github_threads.fetch import (
    GraphQLFn,
    RestFn,
    build_github_context,
    parse_github_url,
)

if TYPE_CHECKING:
    from strands.agent.agent import Agent
    from strands.types.content import Message, SystemContentBlock

    from strandly_harness.core.config import GitHubSettings

logger = logging.getLogger(__name__)

_STATE_KEY = "github_context_injector"

# Valid trigger policies (mirrors the TS ContextInjector's userTurn / everyTurn).
_TRIGGERS = ("userTurn", "everyTurn")

# Scrape https://github.com/... URLs from free text. Trailing punctuation is stripped afterwards so
# a URL ending a sentence ("…/issues/12.") still parses.
_GITHUB_URL_RE = re.compile(r"https?://(?:www\.)?github\.com/[^\s<>()\[\]'\"`]+")
_TRAILING_PUNCT = ".,;:!?\"')]}>"

# The injected block is wrapped in this element so the model knows the content is reference context
# (and so the exact wrapper text is what we strip back out afterwards).
_OPEN = "<github-context>"
_CLOSE = "</github-context>"
_PREAMBLE = (
    "The following GitHub thread(s) referenced this turn have been enriched for you "
    "(body + comments + reviews/review-threads + linked items + replies). This is reference "
    "context for this turn only; it is not part of the durable conversation."
)


class GitHubContextInjector(Plugin):
    """Ephemerally inject enriched GitHub-thread context via two hooks (the plugin surface)."""

    name = "github-context-injector"

    def __init__(
        self,
        gh: GitHubSettings,
        *,
        event: dict[str, Any] | None = None,
        trigger: str = "userTurn",
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
            trigger: ``"userTurn"`` (default — inject once per turn) or ``"everyTurn"`` (inject on
                every model call). Anything else falls back to ``"userTurn"``.
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
        self._trigger = trigger if trigger in _TRIGGERS else "userTurn"
        self._max_urls = max(1, int(max_urls))
        self._graphql = graphql
        self._rest = rest
        self._token = token
        super().__init__()

    # ---- per-turn lifecycle ------------------------------------------------------------------

    @hook  # type: ignore[call-overload]  # SDK hook() overloads don't model bound (self, event) methods
    def reset_turn(self, event: BeforeInvocationEvent) -> None:
        """Reset the per-turn cache/flags so enrichment is re-derived fresh each user turn."""
        self._set(event.agent, "rendered", None)
        self._set(event.agent, "resolved", False)
        self._set(event.agent, "injected_this_turn", False)

    # ---- the two hooks that make injection ephemeral -----------------------------------------

    @hook  # type: ignore[call-overload]  # SDK hook() overloads don't model bound (self, event) methods
    def inject(self, event: BeforeModelCallEvent) -> None:
        """Append the enriched GitHub block to the system prompt before the model call.

        Defensively strips any previously injected block first (so nothing accumulates even if an
        after-hook was skipped), then — honoring the trigger policy — appends a freshly resolved
        (cached-per-turn) block.
        """
        agent = event.agent
        self._strip(agent)  # belt-and-braces: never let a stale block accumulate

        if self._trigger == "userTurn" and self._get(agent, "injected_this_turn"):
            return  # already injected this turn; don't re-inject on tool-loop model calls

        block = self._resolve_block(agent, event.invocation_state)
        if not block:
            return
        self._append(agent, block)
        self._set(agent, "injected_this_turn", True)

    @hook  # type: ignore[call-overload]  # SDK hook() overloads don't model bound (self, event) methods
    def strip(self, event: AfterModelCallEvent) -> None:
        """Strip the injected block back out so it never persists into the durable prompt/session."""
        self._strip(event.agent)

    # ---- rendering (reuses the pure core, fail-soft) -----------------------------------------

    def _resolve_block(self, agent: Agent, invocation_state: dict[str, Any] | None) -> str | None:
        """Return the wrapped block to inject this turn (cached), or ``None`` when nothing applies.

        Resolution (URL collection + network enrichment) happens at most once per turn; the result
        — including a "nothing to inject" ``None`` — is cached so ``everyTurn`` reuses it across the
        turn's model calls without re-fetching.
        """
        if self._get(agent, "resolved"):
            cached = self._get(agent, "rendered")
            return cached if isinstance(cached, str) else None

        rendered = self._render(agent, invocation_state)
        self._set(agent, "rendered", rendered)
        self._set(agent, "resolved", True)
        return rendered

    def _render(self, agent: Agent, invocation_state: dict[str, Any] | None) -> str | None:
        """Collect URLs + enrich them into a wrapped block. Fail-soft: errors become a short note."""
        try:
            urls = self._collect_urls(agent, invocation_state)
            if not urls:
                return None
            # Token-optional: with a token we enrich fully via GraphQL; without one the pure core
            # falls back to the anonymous REST API for public issues/PRs (discussions are
            # GraphQL-only → a short "needs a token" note). A token is used when present, never
            # required (issue #346 owner feedback).
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

    def _collect_urls(self, agent: Agent, invocation_state: dict[str, Any] | None) -> list[str]:
        """Explicit payload URLs first, then URLs scraped from the latest user message.

        Validated with :func:`parse_github_url`, de-duplicated per thread (kind/owner/repo/number),
        and capped at ``max_urls`` (first occurrence wins, so a fragment-bearing explicit URL is
        preferred over a later bare mention of the same thread).
        """
        candidates = [*self._explicit_urls(invocation_state), *self._scrape_user_message(agent)]
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

    def _scrape_user_message(self, agent: Agent) -> list[str]:
        """Scrape GitHub URLs from the latest user message's text (trailing punctuation trimmed)."""
        text = ""
        for message in reversed(agent.messages or []):
            if message.get("role") == "user":
                text = _message_text(message)
                break
        if not text:
            return []
        return [m.rstrip(_TRAILING_PUNCT) for m in _GITHUB_URL_RE.findall(text)]

    # ---- system-prompt mutation (mirrors SystemPromptSkills.reinject) ------------------------

    def _append(self, agent: Agent, block_text: str) -> None:
        """Append ``block_text`` to the system prompt, recording it for an exact-match strip."""
        content = agent.system_prompt_content
        if content is not None:
            blocks: list[SystemContentBlock] = list(content)
            blocks.append({"text": block_text})
            agent.system_prompt = blocks
            self._set(agent, "last_injected", block_text)
        else:
            current = agent.system_prompt or ""
            injection = f"\n\n{block_text}" if current else block_text
            agent.system_prompt = f"{current}{injection}" if current else block_text
            self._set(agent, "last_injected", injection if current else block_text)

    def _strip(self, agent: Agent) -> None:
        """Remove the previously injected block (by exact match) and clear the marker."""
        last = self._get(agent, "last_injected")
        if not isinstance(last, str) or not last:
            return
        content = agent.system_prompt_content
        if content is not None:
            blocks: list[SystemContentBlock] = list(content)
            injected: SystemContentBlock = {"text": last}
            if injected in blocks:
                blocks.remove(injected)
                agent.system_prompt = blocks
            else:
                logger.warning("injected github-context block not found in system prompt content")
        else:
            current = agent.system_prompt or ""
            if last in current:
                agent.system_prompt = current.replace(last, "", 1)
        self._set(agent, "last_injected", None)

    # ---- seams (lazy so tests can monkeypatch github._graphql) -------------------------------

    def _resolve_graphql(self) -> GraphQLFn:
        if self._graphql is not None:
            return self._graphql
        from strandly_harness.tools.github import _graphql

        return _graphql

    def _resolve_rest(self) -> RestFn:
        if self._rest is not None:
            return self._rest
        from strandly_harness.tools.github import _rest_get

        return _rest_get

    def _resolve_token(self) -> str | None:
        from strandly_harness.tools.github import _get_token

        return _get_token(self._gh, False)

    @staticmethod
    def _wrap(text: str) -> str:
        return f"{_OPEN}\n{_PREAMBLE}\n\n{text}\n{_CLOSE}"

    # ---- state helpers (same shape as SystemPromptSkills) ------------------------------------

    def _get(self, agent: Agent, key: str) -> Any:
        data = agent.state.get(_STATE_KEY)
        return data.get(key) if isinstance(data, dict) else None

    def _set(self, agent: Agent, key: str, value: Any) -> None:
        data = agent.state.get(_STATE_KEY)
        if data is not None and not isinstance(data, dict):
            raise TypeError(
                f"expected dict for state key '{_STATE_KEY}', got {type(data).__name__}"
            )
        data = dict(data) if isinstance(data, dict) else {}
        data[key] = value
        agent.state.set(_STATE_KEY, data)


def _message_text(message: Message) -> str:
    """Join the text of a message's content blocks (ignoring tool-use/result blocks)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return "\n".join(parts)
