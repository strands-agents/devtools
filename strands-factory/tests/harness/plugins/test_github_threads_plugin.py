"""Hermetic tests for the ``GitHubContextInjector`` plugin (GitHub-thread enrichment per turn).

No live network: the plugin's ``graphql``/``rest`` seams are fakes. The full-path tests drive a real
``Agent`` over a fake model: the block lands once, as an ``injected`` block on the user's ask (never
on a tool-result message), the system prompt is identical on every model call, and a turn fetches
once. The render tests cover URL sourcing (invoke payload, ``ctx.event``, the latest user message),
dedup/cap, multi-URL and fail-soft behavior.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from strands import Agent, tool
from strands.hooks import BeforeInvocationEvent

from scripted_model import ScriptedModel, text_events, tool_use_events
from strandly.core.config import GitHubSettings
from strandly.plugins.github_threads.plugin import (
    _OPEN,
    GitHubContextInjector,
)

# ---------------------------------------------------------------------------
# Fakes (no AWS / no network)
# ---------------------------------------------------------------------------


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
    max_urls: int = 5,
    graphql: Any = None,
    rest: Any = None,
    token: str | None = "t",
) -> GitHubContextInjector:
    return GitHubContextInjector(
        GitHubSettings(),
        event=event,
        max_urls=max_urls,
        graphql=graphql or FakeGraphQL(),
        rest=rest if rest is not None else FakeREST(),
        token=token,
    )


def _render(plugin: GitHubContextInjector, text: str, invocation_state: dict[str, Any] | None = None) -> str | None:
    """What the injector would write for a call whose latest user message is ``text``."""
    plugin._on_invocation(
        BeforeInvocationEvent(agent=SimpleNamespace(), invocation_state=invocation_state or {})  # type: ignore[arg-type]
    )
    return plugin._render_for(SimpleNamespace(messages=[_user_msg(text)]))  # type: ignore[arg-type]


class _Recording(ScriptedModel):
    """Records each call's messages and system prompt."""

    def __init__(self, *responses: list[dict[str, Any]]) -> None:
        super().__init__(*responses)
        self.calls: list[tuple[list[dict[str, Any]], Any]] = []

    async def stream(self, messages, tool_specs=None, system_prompt=None, *args, **kwargs):  # type: ignore[override]
        self.calls.append((messages, kwargs.get("system_prompt_content") or system_prompt))
        async for ev in super().stream(messages, tool_specs, system_prompt, *args, **kwargs):
            yield ev


@tool
def probe() -> str:
    """Return a fixed string."""
    return "probed"


def _injected(agent: Agent) -> list[tuple[int, str]]:
    return [
        (i, b["injected"]["text"]) for i, m in enumerate(agent.messages) for b in m["content"] if "injected" in b
    ]


# ---------------------------------------------------------------------------
# Full path: a real Agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_block_lands_once_on_the_ask_and_the_system_prompt_never_changes():
    gql = FakeGraphQL()
    model = _Recording(tool_use_events("probe", "t1"), text_events("done"))
    agent = Agent(
        model=model, system_prompt="BASE PROMPT", tools=[probe], plugins=[_make(graphql=gql)], callback_handler=None
    )

    await agent.invoke_async("see https://github.com/o/r/issues/1 please")

    [(index, block)] = _injected(agent)
    assert index == 0 and block.startswith(_OPEN) and "🎫 ISSUE o/r#1" in block
    assert agent.messages[0]["content"][0] == {"text": "see https://github.com/o/r/issues/1 please"}
    assert agent.system_prompt == "BASE PROMPT"
    (first, prompt1), (second, prompt2) = model.calls
    assert prompt1 == prompt2  # a byte-stable system prefix for the prompt cache
    assert "🎫 ISSUE o/r#1" in str(first[-1])
    assert "🎫 ISSUE o/r#1" not in str(second)  # the tool round's call reads only the latest user message
    assert len(gql.calls) == 1


@pytest.mark.asyncio
async def test_each_turn_fetches_again_and_a_turn_without_urls_adds_nothing():
    gql = FakeGraphQL()
    agent = Agent(model=_Recording(), plugins=[_make(graphql=gql)], callback_handler=None)

    await agent.invoke_async("https://github.com/o/r/issues/1")
    await agent.invoke_async("again https://github.com/o/r/issues/1")
    await agent.invoke_async("nothing to enrich")

    assert [index for index, _ in _injected(agent)] == [0, 2]
    assert len(gql.calls) == 2


@pytest.mark.asyncio
async def test_urls_from_invoke_payload_reach_the_model():
    gql = FakeGraphQL()
    model = _Recording()
    agent = Agent(model=model, plugins=[_make(graphql=gql)], callback_handler=None)

    await agent.invoke_async("no urls here", invocation_state={"githubUrls": ["https://github.com/o/r/issues/7"]})

    assert "🎫 ISSUE o/r#7" in str(model.calls[0][0][-1])
    assert gql.calls == [{"owner": "o", "name": "r", "number": 7}]


# ---------------------------------------------------------------------------
# URL sourcing
# ---------------------------------------------------------------------------


def test_urls_from_ctx_event():
    plugin = _make(event={"githubUrls": "https://github.com/o/r/issues/8"})
    assert "🎫 ISSUE o/r#8" in (_render(plugin, "no urls here") or "")


def test_urls_scraped_from_user_message():
    gql = FakeGraphQL()
    plugin = _make(graphql=gql)
    text = _render(plugin, "please look at https://github.com/o/r/issues/9, thanks!")  # trailing comma
    assert "🎫 ISSUE o/r#9" in (text or "")
    # Trailing punctuation must be trimmed so the URL parses to #9 (not "9,").
    assert gql.calls == [{"owner": "o", "name": "r", "number": 9}]


def test_dedup_same_thread_across_sources():
    # Same thread via payload (with fragment) and message text — fetched once, first occurrence wins.
    gql = FakeGraphQL()
    plugin = _make(event={"githubUrls": ["https://github.com/o/r/issues/1#issuecomment-5"]}, graphql=gql)
    _render(plugin, "https://github.com/o/r/issues/1 again")
    assert len(gql.calls) == 1


def test_cap_limits_url_count():
    gql = FakeGraphQL()
    plugin = _make(graphql=gql, max_urls=3)
    _render(plugin, " ".join(f"https://github.com/o/r/issues/{n}" for n in range(1, 11)))
    assert len(gql.calls) == 3  # capped


def test_multi_url_renders_all():
    text = _render(_make(), "https://github.com/o/r/issues/1 and https://github.com/o/r/issues/2") or ""
    assert "🎫 ISSUE o/r#1" in text and "🎫 ISSUE o/r#2" in text
    assert "\n\n---\n\n" in text  # the pure core's block separator


def test_no_urls_no_injection():
    assert _render(_make(), "just a plain message, no links") is None


def test_non_github_url_ignored():
    gql = FakeGraphQL()
    assert _render(_make(graphql=gql), "see https://gitlab.com/o/r/issues/1") is None
    assert gql.calls == []  # never hit the network for a non-GitHub URL


# ---------------------------------------------------------------------------
# Fail-soft and the anonymous fallback
# ---------------------------------------------------------------------------


def test_failsoft_on_fetch_error_renders_note_not_crash():
    def boom(query: str, variables: dict[str, Any], token: str) -> dict[str, Any]:
        raise RuntimeError("HTTP 503")

    text = _render(_make(graphql=boom), "https://github.com/o/r/issues/1") or ""  # must not raise
    # The pure core's per-URL fail-soft note, wrapped in our block.
    assert _OPEN in text and "GitHub context unavailable for o/r#1" in text


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
    plugin = _make(token="", rest=rest, graphql=lambda *a: pytest.fail("graphql must not be called"))
    text = _render(plugin, "https://github.com/o/r/issues/1")
    assert _OPEN in text
    assert "🎫 ISSUE o/r#1: A public bug" in text
    assert "the rest body" in text
    assert "Comment #1** by @bob" in text
    assert "Enriched anonymously via GitHub REST" in text
    # Every REST call was anonymous (token == "").
    assert rest.calls and all(tok == "" for _, tok in rest.calls)


def test_no_token_discussion_renders_needs_token_note():
    # Discussions are GraphQL-only; without a token they get a short "needs a token" note (no call).
    plugin = _make(
        token="",
        rest=lambda *a: pytest.fail("rest must not be called for a discussion"),
        graphql=lambda *a: pytest.fail("graphql must not be called"),
    )
    text = _render(plugin, "https://github.com/o/r/discussions/7") or ""
    assert _OPEN in text
    assert "GitHub context unavailable for o/r#7" in text
    assert "configure a GitHub token to enrich discussions" in text


# ---------------------------------------------------------------------------
# Wiring: the plugin is registered with or without a token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [{"STRANDLY_GITHUB_TOKEN": "ghp_x"}, {}])
async def test_plugin_wired_with_or_without_a_token(values, fake_model, tmp_path):
    # Token-optional: without a token it still enriches public issues/PRs via anonymous REST.
    from strandly.core.agent import build_agent
    from strandly.core.config import Config
    from strandly.core.context import RuntimeContext

    agent = await build_agent(Config(values=values), RuntimeContext(cwd=str(tmp_path)), model=fake_model)
    owners = {
        type(getattr(cb, "__self__", None)).__name__
        for cb in agent.hooks.get_callbacks_for(BeforeInvocationEvent(agent=agent))
    }
    assert "GitHubContextInjector" in owners
