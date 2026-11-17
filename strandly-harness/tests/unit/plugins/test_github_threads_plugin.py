"""Hermetic tests for the ``GitHubContextInjector`` plugin (ephemeral GitHub-thread injection).

No live network: the plugin's ``graphql`` seam is injected as a fake. Covers trigger gating (inject
once per user turn, not on every tool-loop model call), the ephemeral strip (block present during
the model call, absent after / never duplicated or accumulated), URL sourcing from the invoke
payload, ``ctx.event``, and the latest user message, dedup/cap, multi-URL, fetch-once-per-turn
caching, the structured ``system_prompt_content`` path, and fail-soft behavior.
"""

from __future__ import annotations

from typing import Any

import pytest
from strands.hooks import (
    AfterModelCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
)

from strandly_harness.core.config import GitHubSettings
from strandly_harness.plugins.github_threads.plugin import (
    _CLOSE,
    _OPEN,
    GitHubContextInjector,
)

# ---------------------------------------------------------------------------
# Fakes (no AWS / no network)
# ---------------------------------------------------------------------------


class FakeState:
    """Just enough of the agent state API the plugin uses (get/set by key)."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def get(self, key: str) -> Any:
        return self._data.get(key)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value


class FakeAgent:
    """Minimal agent the plugin reads/writes: state, messages, and a system prompt.

    ``structured`` selects the content-block path (``system_prompt_content`` is a list) vs the
    plain-string path (``system_prompt_content`` is ``None``), so both branches are exercised.
    """

    def __init__(self, messages: list[dict[str, Any]] | None = None, *, structured: bool = False):
        self.state = FakeState()
        self.messages = messages or []
        if structured:
            self._content: list[dict[str, Any]] | None = [{"text": "BASE PROMPT"}]
            self.system_prompt: Any = list(self._content)
        else:
            self._content = None
            self.system_prompt = "BASE PROMPT"

    @property
    def system_prompt_content(self) -> list[dict[str, Any]] | None:
        return self._content

    @system_prompt_content.setter
    def system_prompt_content(self, value: list[dict[str, Any]] | None) -> None:
        self._content = value

    # The plugin assigns ``agent.system_prompt = blocks`` (list) on the structured path; mirror the
    # SDK by keeping system_prompt_content in sync so a later read sees the updated blocks.
    def __setattr__(self, name: str, value: Any) -> None:
        if name == "system_prompt" and isinstance(value, list):
            object.__setattr__(self, "_content", list(value))
        object.__setattr__(self, name, value)


def _issue_node(number: int = 1, title: str = "A bug") -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "body": "the body",
        "state": "OPEN",
        "url": f"https://github.com/o/r/issues/{number}",
        "createdAt": "2024-01-01",
        "author": {"login": "alice"},
        "comments": {"totalCount": 0, "nodes": []},
        "timelineItems": {"nodes": []},
    }


def _wrap_issue(node: dict[str, Any]) -> dict[str, Any]:
    return {"data": {"repository": {"issue": node}}}


class FakeGraphQL:
    """A counting fake of ``github._graphql(query, variables, token) -> dict``."""

    def __init__(self, node_by_number: dict[int, dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._nodes = node_by_number or {}

    def __call__(self, query: str, variables: dict[str, Any], token: str) -> dict[str, Any]:
        self.calls.append(variables)
        number = int(variables.get("number", 1))
        node = self._nodes.get(number) or _issue_node(number)
        return _wrap_issue(node)


class FakeREST:
    """A fake of ``github._rest_get(path, token) -> parsed JSON`` for the no-token REST fallback.

    Maps a substring of the request path to a canned response; an unmapped path returns ``{}`` (so a
    missing mapping degrades to a fail-soft note rather than a live call). Records every path so a
    test can assert the request was anonymous (token == "").
    """

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._routes = routes or {}

    def __call__(self, path: str, token: str) -> Any:
        self.calls.append((path, token))
        for needle, value in self._routes.items():
            if needle in path:
                return value
        return {}


def _user_msg(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"text": text}]}


def _make(
    *,
    event: dict[str, Any] | None = None,
    trigger: str = "userTurn",
    max_urls: int = 5,
    graphql: Any = None,
    rest: Any = None,
    token: str | None = "t",
) -> GitHubContextInjector:
    return GitHubContextInjector(
        GitHubSettings(),
        event=event,
        trigger=trigger,
        max_urls=max_urls,
        graphql=graphql or FakeGraphQL(),
        rest=rest if rest is not None else FakeREST(),
        token=token,
    )


def _prompt_text(agent: FakeAgent) -> str:
    sp = agent.system_prompt
    if isinstance(sp, list):
        return "\n".join(b["text"] for b in sp if isinstance(b, dict) and "text" in b)
    return sp or ""


# ---------------------------------------------------------------------------
# Ephemeral injection + strip
# ---------------------------------------------------------------------------


def test_inject_then_strip_is_ephemeral():
    agent = FakeAgent([_user_msg("see https://github.com/o/r/issues/1 please")])
    plugin = _make()

    plugin.reset_turn(BeforeInvocationEvent(agent=agent))
    plugin.inject(BeforeModelCallEvent(agent=agent))
    during = _prompt_text(agent)
    assert _OPEN in during and _CLOSE in during
    assert "🎫 ISSUE o/r#1" in during
    assert during.startswith("BASE PROMPT")  # base prompt preserved

    plugin.strip(AfterModelCallEvent(agent=agent))
    after = _prompt_text(agent)
    assert _OPEN not in after and _CLOSE not in after
    assert after == "BASE PROMPT"  # nothing persisted


def test_no_accumulation_across_repeated_inject_strip():
    agent = FakeAgent([_user_msg("https://github.com/o/r/issues/1")])
    plugin = _make(trigger="everyTurn")
    for _ in range(3):
        plugin.reset_turn(BeforeInvocationEvent(agent=agent))
        plugin.inject(BeforeModelCallEvent(agent=agent))
        plugin.strip(AfterModelCallEvent(agent=agent))
    assert _prompt_text(agent) == "BASE PROMPT"


def test_before_hook_self_cleans_if_after_skipped():
    # Even if the after-hook never runs, the next before-hook strips the stale block first, so the
    # block can never accumulate or duplicate.
    agent = FakeAgent([_user_msg("https://github.com/o/r/issues/1")])
    plugin = _make(trigger="everyTurn")
    plugin.reset_turn(BeforeInvocationEvent(agent=agent))
    plugin.inject(BeforeModelCallEvent(agent=agent))
    plugin.inject(BeforeModelCallEvent(agent=agent))  # no after-strip between calls
    assert _prompt_text(agent).count(_OPEN) == 1


# ---------------------------------------------------------------------------
# Trigger gating: userTurn vs everyTurn
# ---------------------------------------------------------------------------


def test_userturn_injects_once_not_midloop():
    agent = FakeAgent([_user_msg("https://github.com/o/r/issues/1")])
    gql = FakeGraphQL()
    plugin = _make(graphql=gql)  # default userTurn

    plugin.reset_turn(BeforeInvocationEvent(agent=agent))
    # First model call of the turn: inject.
    plugin.inject(BeforeModelCallEvent(agent=agent))
    assert _OPEN in _prompt_text(agent)
    plugin.strip(AfterModelCallEvent(agent=agent))

    # Mid-loop model call (after a tool result): userTurn must NOT re-inject.
    plugin.inject(BeforeModelCallEvent(agent=agent))
    assert _OPEN not in _prompt_text(agent)
    plugin.strip(AfterModelCallEvent(agent=agent))
    assert len(gql.calls) == 1  # enrichment fetched exactly once for the turn


def test_everyturn_injects_each_call_but_fetches_once():
    agent = FakeAgent([_user_msg("https://github.com/o/r/issues/1")])
    gql = FakeGraphQL()
    plugin = _make(trigger="everyTurn", graphql=gql)

    plugin.reset_turn(BeforeInvocationEvent(agent=agent))
    plugin.inject(BeforeModelCallEvent(agent=agent))
    assert _OPEN in _prompt_text(agent)
    plugin.strip(AfterModelCallEvent(agent=agent))

    plugin.inject(BeforeModelCallEvent(agent=agent))  # mid-loop: everyTurn re-injects
    assert _OPEN in _prompt_text(agent)
    plugin.strip(AfterModelCallEvent(agent=agent))

    assert len(gql.calls) == 1  # but the block was cached — fetched only once this turn


def test_reset_turn_refetches_next_turn():
    agent = FakeAgent([_user_msg("https://github.com/o/r/issues/1")])
    gql = FakeGraphQL()
    plugin = _make(graphql=gql)

    plugin.reset_turn(BeforeInvocationEvent(agent=agent))
    plugin.inject(BeforeModelCallEvent(agent=agent))
    plugin.strip(AfterModelCallEvent(agent=agent))

    # New turn: cache is reset, so enrichment is re-derived.
    plugin.reset_turn(BeforeInvocationEvent(agent=agent))
    plugin.inject(BeforeModelCallEvent(agent=agent))
    plugin.strip(AfterModelCallEvent(agent=agent))
    assert len(gql.calls) == 2


# ---------------------------------------------------------------------------
# URL sourcing
# ---------------------------------------------------------------------------


def test_urls_from_invoke_payload():
    agent = FakeAgent([_user_msg("no urls here")])
    gql = FakeGraphQL()
    plugin = _make(graphql=gql)
    plugin.reset_turn(BeforeInvocationEvent(agent=agent))
    plugin.inject(
        BeforeModelCallEvent(
            agent=agent,
            invocation_state={"githubUrls": ["https://github.com/o/r/issues/7"]},
        )
    )
    assert "🎫 ISSUE o/r#7" in _prompt_text(agent)
    assert gql.calls == [{"owner": "o", "name": "r", "number": 7}]


def test_urls_from_ctx_event():
    agent = FakeAgent([_user_msg("no urls here")])
    gql = FakeGraphQL()
    plugin = _make(event={"githubUrls": "https://github.com/o/r/issues/8"}, graphql=gql)
    plugin.reset_turn(BeforeInvocationEvent(agent=agent))
    plugin.inject(BeforeModelCallEvent(agent=agent))
    assert "🎫 ISSUE o/r#8" in _prompt_text(agent)


def test_urls_scraped_from_user_message():
    agent = FakeAgent(
        [_user_msg("please look at https://github.com/o/r/issues/9, thanks!")]  # trailing comma
    )
    gql = FakeGraphQL()
    plugin = _make(graphql=gql)
    plugin.reset_turn(BeforeInvocationEvent(agent=agent))
    plugin.inject(BeforeModelCallEvent(agent=agent))
    assert "🎫 ISSUE o/r#9" in _prompt_text(agent)
    # Trailing punctuation must be trimmed so the URL parses to #9 (not "9,").
    assert gql.calls == [{"owner": "o", "name": "r", "number": 9}]


def test_dedup_same_thread_across_sources():
    # Same thread via payload (with fragment) and message text — fetched once, first occurrence wins.
    agent = FakeAgent([_user_msg("https://github.com/o/r/issues/1 again")])
    gql = FakeGraphQL()
    plugin = _make(
        event={"githubUrls": ["https://github.com/o/r/issues/1#issuecomment-5"]}, graphql=gql
    )
    plugin.reset_turn(BeforeInvocationEvent(agent=agent))
    plugin.inject(BeforeModelCallEvent(agent=agent))
    assert len(gql.calls) == 1


def test_cap_limits_url_count():
    urls = [f"https://github.com/o/r/issues/{n}" for n in range(1, 11)]
    agent = FakeAgent([_user_msg(" ".join(urls))])
    gql = FakeGraphQL()
    plugin = _make(graphql=gql, max_urls=3)
    plugin.reset_turn(BeforeInvocationEvent(agent=agent))
    plugin.inject(BeforeModelCallEvent(agent=agent))
    assert len(gql.calls) == 3  # capped


def test_multi_url_renders_all():
    agent = FakeAgent(
        [_user_msg("https://github.com/o/r/issues/1 and https://github.com/o/r/issues/2")]
    )
    gql = FakeGraphQL()
    plugin = _make(graphql=gql)
    plugin.reset_turn(BeforeInvocationEvent(agent=agent))
    plugin.inject(BeforeModelCallEvent(agent=agent))
    text = _prompt_text(agent)
    assert "🎫 ISSUE o/r#1" in text and "🎫 ISSUE o/r#2" in text
    assert "\n\n---\n\n" in text  # the pure core's block separator


# ---------------------------------------------------------------------------
# Structured (content-block) system prompt path
# ---------------------------------------------------------------------------


def test_structured_system_prompt_content_path():
    agent = FakeAgent([_user_msg("https://github.com/o/r/issues/1")], structured=True)
    plugin = _make()
    plugin.reset_turn(BeforeInvocationEvent(agent=agent))
    plugin.inject(BeforeModelCallEvent(agent=agent))
    blocks = agent.system_prompt_content
    assert blocks is not None and any("🎫 ISSUE o/r#1" in b.get("text", "") for b in blocks)

    plugin.strip(AfterModelCallEvent(agent=agent))
    blocks_after = agent.system_prompt_content
    assert blocks_after == [{"text": "BASE PROMPT"}]  # exact block removed, base preserved


# ---------------------------------------------------------------------------
# Fail-soft
# ---------------------------------------------------------------------------


def test_no_urls_no_injection():
    agent = FakeAgent([_user_msg("just a plain message, no links")])
    plugin = _make()
    plugin.reset_turn(BeforeInvocationEvent(agent=agent))
    plugin.inject(BeforeModelCallEvent(agent=agent))
    assert _prompt_text(agent) == "BASE PROMPT"  # nothing injected


def test_non_github_url_ignored():
    agent = FakeAgent([_user_msg("see https://gitlab.com/o/r/issues/1")])
    gql = FakeGraphQL()
    plugin = _make(graphql=gql)
    plugin.reset_turn(BeforeInvocationEvent(agent=agent))
    plugin.inject(BeforeModelCallEvent(agent=agent))
    assert _prompt_text(agent) == "BASE PROMPT"
    assert gql.calls == []  # never hit the network for a non-GitHub URL


def test_failsoft_on_fetch_error_renders_note_not_crash():
    def boom(query: str, variables: dict[str, Any], token: str) -> dict[str, Any]:
        raise RuntimeError("HTTP 503")

    agent = FakeAgent([_user_msg("https://github.com/o/r/issues/1")])
    plugin = _make(graphql=boom)
    plugin.reset_turn(BeforeInvocationEvent(agent=agent))
    plugin.inject(BeforeModelCallEvent(agent=agent))  # must not raise
    text = _prompt_text(agent)
    # The pure core's per-URL fail-soft note, wrapped in our block.
    assert _OPEN in text and "GitHub context unavailable for o/r#1" in text
    plugin.strip(AfterModelCallEvent(agent=agent))
    assert _prompt_text(agent) == "BASE PROMPT"


def test_no_token_falls_back_to_rest_anonymously():
    # No token → the plugin enriches public issues via the anonymous REST seam (token passed as "").
    rest = FakeREST(
        {
            "/issues/1/comments": [
                {"id": 5, "user": {"login": "bob"}, "body": "a rest comment", "created_at": "x"}
            ],
            "/issues/1": {
                "number": 1,
                "title": "A public bug",
                "body": "the rest body",
                "state": "open",
                "user": {"login": "alice"},
                "html_url": "https://github.com/o/r/issues/1",
            },
        }
    )
    agent = FakeAgent([_user_msg("https://github.com/o/r/issues/1")])
    plugin = _make(token="", rest=rest, graphql=lambda *a: pytest.fail("graphql must not be called"))
    plugin.reset_turn(BeforeInvocationEvent(agent=agent))
    plugin.inject(BeforeModelCallEvent(agent=agent))
    text = _prompt_text(agent)
    assert _OPEN in text
    assert "🎫 ISSUE o/r#1: A public bug" in text
    assert "the rest body" in text
    assert "Comment #1** by @bob" in text
    assert "Enriched anonymously via GitHub REST" in text
    # Every REST call was anonymous (token == "").
    assert rest.calls and all(tok == "" for _, tok in rest.calls)
    plugin.strip(AfterModelCallEvent(agent=agent))
    assert _prompt_text(agent) == "BASE PROMPT"


def test_no_token_discussion_renders_needs_token_note():
    # Discussions are GraphQL-only; without a token they get a short "needs a token" note (no call).
    agent = FakeAgent([_user_msg("https://github.com/o/r/discussions/7")])
    plugin = _make(
        token="",
        rest=lambda *a: pytest.fail("rest must not be called for a discussion"),
        graphql=lambda *a: pytest.fail("graphql must not be called"),
    )
    plugin.reset_turn(BeforeInvocationEvent(agent=agent))
    plugin.inject(BeforeModelCallEvent(agent=agent))
    text = _prompt_text(agent)
    assert _OPEN in text
    assert "GitHub context unavailable for o/r#7" in text
    assert "configure a GitHub token to enrich discussions" in text


def test_bad_trigger_falls_back_to_userturn():
    plugin = _make(trigger="bogus")
    assert plugin._trigger == "userTurn"


# ---------------------------------------------------------------------------
# Wiring: the plugin is constructed when GitHub is enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plugin_wired_when_github_enabled(fake_model, tmp_path):
    from strands.hooks import BeforeModelCallEvent

    from strandly_harness.core.agent import build_agent
    from strandly_harness.core.config import Config
    from strandly_harness.core.context import RuntimeContext

    agent = await build_agent(
        Config(values={"STRANDLY_GITHUB_TOKEN": "ghp_x"}),
        RuntimeContext(cwd=str(tmp_path)),
        model=fake_model,
    )
    owners = {
        type(getattr(cb, "__self__", None)).__name__
        for cb in agent.hooks.get_callbacks_for(BeforeModelCallEvent(agent=agent))
    }
    assert "GitHubContextInjector" in owners


@pytest.mark.asyncio
async def test_plugin_present_without_token(fake_model, tmp_path):
    # Token-optional (issue #346): the injector is registered even with no token configured, so it
    # can enrich public issues/PRs via the anonymous REST fallback.
    from strands.hooks import BeforeModelCallEvent

    from strandly_harness.core.agent import build_agent
    from strandly_harness.core.config import Config
    from strandly_harness.core.context import RuntimeContext

    agent = await build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model
    )
    owners = {
        type(getattr(cb, "__self__", None)).__name__
        for cb in agent.hooks.get_callbacks_for(BeforeModelCallEvent(agent=agent))
    }
    assert "GitHubContextInjector" in owners
