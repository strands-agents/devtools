"""The model — a fixed tier registry, not a knob. Claude on Bedrock with adaptive thinking.

The TOP agent always runs the default tier (Opus 4.8). Subagents may select one of the fixed
tiers in ``constants.MODEL_TIERS`` (``fast`` = Haiku 4.5, ``advanced`` = Fable 5) via the
``spawn`` tool's ``model`` argument — a deliberate Claude-family subset, never a free-form id.
"""

from __future__ import annotations

from typing import Any

from botocore.config import Config as BotocoreConfig
from strands.models.model import CacheConfig, CacheToolsConfig

from strandly_harness.core.config import Config
from strandly_harness.core.constants import (
    MODEL_BOTO_MAX_ATTEMPTS,
    MODEL_BOTO_RETRY_MODE,
    MODEL_CACHE_CONFIG,
    MODEL_CACHE_TOOLS,
    MODEL_READ_TIMEOUT_SECONDS,
    MODEL_TIER_DEFAULT,
    MODEL_TIERS,
)


def build_model(config: Config, tier: str = MODEL_TIER_DEFAULT) -> Any:
    """Build the Bedrock model for a tier, sharing the harness's boto session (ambient creds when unset).

    ``tier`` must be a key of ``MODEL_TIERS`` ("default" / "fast" / "advanced"). Callers that take
    user-ish input (the ``spawn`` tool) validate first and return a friendly error; a ``ValueError``
    here means a programming bug, not bad input.
    """
    from strands.models import BedrockModel

    spec = MODEL_TIERS.get(tier)
    if spec is None:
        valid = ", ".join(sorted(MODEL_TIERS))
        raise ValueError(f"unknown model tier {tier!r}; valid tiers: {valid}")

    kwargs: dict[str, Any] = {
        "model_id": spec["model_id"],
        "max_tokens": spec["max_tokens"],
        "context_window_limit": spec["context_window"],
        "additional_request_fields": spec["thinking_config"],
        "cache_config": CacheConfig(**MODEL_CACHE_CONFIG),
        "cache_tools": CacheToolsConfig(**MODEL_CACHE_TOOLS),
        "boto_client_config": BotocoreConfig(
            read_timeout=MODEL_READ_TIMEOUT_SECONDS,
            # Deep request-level retries (adaptive = backoff + client-side rate limiting). Without
            # this, botocore's legacy default (~4 narrow attempts) is the only protection under a
            # model call, and a throttling/5xx window fails the whole fire-and-forget run.
            retries={"max_attempts": MODEL_BOTO_MAX_ATTEMPTS, "mode": MODEL_BOTO_RETRY_MODE},
        ),
    }
    session = config.boto_session()
    if session is not None:
        kwargs["boto_session"] = session
    elif config.aws_region:
        kwargs["region_name"] = config.aws_region
    return BedrockModel(**kwargs)
