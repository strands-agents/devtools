"""Tests for ``resolve_self_login`` — the agent's own GitHub login for the prompt identity section.

Hermetic: the ``viewer { login }`` network seam (``_query_viewer_login``) is a fake here; the
suite-wide conftest fixture already stubs it to ``None`` and clears the per-process cache.
"""

from __future__ import annotations

from strandly.tools import github_client as gh


def test_login_comes_from_the_token(monkeypatch):
    monkeypatch.setattr(gh, "_query_viewer_login", lambda token: "example-agent")
    assert gh.resolve_self_login("t") == "example-agent"


def test_cached_per_token(monkeypatch):
    calls: list[str] = []

    def counting(token: str) -> str:
        calls.append(token)
        return f"bot-for-{token}"

    monkeypatch.setattr(gh, "_query_viewer_login", counting)
    assert gh.resolve_self_login("t") == "bot-for-t"
    assert gh.resolve_self_login("t") == "bot-for-t"
    assert calls == ["t"]  # second resolve served from the cache
    # A different token is a different identity — the cache is keyed on the token, not global.
    assert gh.resolve_self_login("other") == "bot-for-other"
    assert calls == ["t", "other"]


def test_failure_not_cached(monkeypatch):
    # A transient API failure must not be cached — the next agent build retries.
    monkeypatch.setattr(gh, "_query_viewer_login", lambda token: None)
    assert gh.resolve_self_login("t") is None
    monkeypatch.setattr(gh, "_query_viewer_login", lambda token: "bot")
    assert gh.resolve_self_login("t") == "bot"


def test_no_token_resolves_none(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(gh, "_query_viewer_login", lambda token: calls.append(token) or "bot")
    assert gh.resolve_self_login(None) is None
    assert gh.resolve_self_login("") is None
    assert calls == []  # no token → no API call


def test_repo_owner_env_shortcut_not_used(monkeypatch):
    # _viewer_login's GITHUB_REPOSITORY_OWNER shortcut is fine for the throttle, but WRONG for
    # identity: CI on example-maintainer/example-repo runs with owner "example-maintainer" while the token belongs
    # to the bot. resolve_self_login must ask the API, not trust the repo owner.
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "example-maintainer")
    monkeypatch.setattr(gh, "_query_viewer_login", lambda token: "example-agent")
    assert gh.resolve_self_login("t") == "example-agent"


def test_throttle_viewer_login_still_uses_env_shortcut(monkeypatch):
    # The throttle's resolver keeps its cheap env shortcut (unchanged behavior).
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "some-owner")
    assert gh._viewer_login("t") == "some-owner"
