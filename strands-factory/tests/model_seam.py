"""Test seam: answer the model strings the agent factory hands the harness with test models."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def use_model(monkeypatch: Any, factory: Callable[[str], Any]) -> None:
    """Resolve every model string the agent factory receives with ``factory(model_string)``.

    Patches the harness's ``resolve_model`` as ``create_harness`` sees it.
    """
    import strands_harness.agent as harness_agent

    real = harness_agent.resolve_model

    def resolve(model: Any, *args: Any, **kwargs: Any) -> Any:
        return factory(model) if isinstance(model, str) else real(model, *args, **kwargs)

    monkeypatch.setattr(harness_agent, "resolve_model", resolve)
