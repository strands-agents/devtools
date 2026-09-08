"""Shared test fixtures. All tests run with no AWS and no network."""

from __future__ import annotations

from collections.abc import AsyncIterator  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402

# Importing the package runs sanitize_otel_env() before any strands/opentelemetry import, so the
# suite doesn't need OTEL_PROPAGATORS set on the command line.
import strandly_harness  # noqa: F401


@pytest.fixture(autouse=True)
def _hermetic_github_context(monkeypatch):
    """Drop any ambient ``GITHUB_CONTEXT`` so the suite is hermetic regardless of the environment.

    Several seams (e.g. ``serving.agentcore._github_context`` and the mention poller) consult
    ``GITHUB_CONTEXT`` as a fallback; an ambient value (set by GitHub Actions) would otherwise leak
    into tests. Removing it for every test keeps results identical with ``GITHUB_CONTEXT`` set or
    unset.
    """
    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)


def text_response_events(text: str, stop_reason: str = "end_turn") -> list[dict[str, Any]]:
    """Raw model wire-format stream for a plain text response (what the event loop consumes)."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {}}},
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": stop_reason}},
    ]


class FakeModel:
    """Minimal stand-in for a Strands model — replays canned wire-format events, no provider call."""

    stateful = False
    context_window_limit = 200_000

    def __init__(self, events: list[dict[str, Any]] | None = None):
        self.events = events or []
        self.config: dict[str, Any] = {}

    def get_config(self) -> dict[str, Any]:
        return self.config

    def update_config(self, **kwargs: Any) -> None:
        self.config.update(kwargs)

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        for ev in self.events:
            yield ev

    async def structured_output(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def count_tokens(self, *args: Any, **kwargs: Any) -> int:  # pragma: no cover
        return 0


@pytest.fixture
def fake_model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def text_model():
    def _make(text: str) -> FakeModel:
        return FakeModel(events=text_response_events(text))

    return _make
