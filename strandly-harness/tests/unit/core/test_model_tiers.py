"""Model tier registry: the fixed Claude-family subset selectable via spawn."""

from __future__ import annotations

import pytest

from strandly_harness.core.config import Config
from strandly_harness.core.constants import MODEL_TIER_DEFAULT, MODEL_TIERS
from strandly_harness.core.model import build_model


def test_registry_shape():
    # Exactly the configured subset — adding a tier is a deliberate constants change.
    assert set(MODEL_TIERS) == {"default", "fast", "advanced"}
    assert MODEL_TIER_DEFAULT in MODEL_TIERS
    for name, spec in MODEL_TIERS.items():
        assert spec["model_id"], name
        assert spec["max_tokens"] > 0, name
        assert spec["context_window"] > 0, name
        assert "thinking_config" in spec, name


def test_tiers_are_claude_family():
    for name, spec in MODEL_TIERS.items():
        assert "anthropic" in spec["model_id"], f"{name} must stay in the Claude family"


def test_fast_tier_uses_haiku_supported_config():
    """Regression: Haiku 4.5 rejects adaptive thinking AND output_config.effort (verified against
    live Bedrock — both raise ValidationException), and is only registered under the *versioned*
    inference-profile id (the bare alias is 'invalid model identifier'). Both bit the fast tier."""
    fast = MODEL_TIERS["fast"]
    # Versioned id, not the bare alias.
    assert fast["model_id"] == "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    thinking = fast["thinking_config"]
    # Neither of the two fields Haiku rejects may be present.
    assert thinking.get("thinking", {}).get("type") != "adaptive"
    assert "output_config" not in thinking


def test_build_model_rejects_unknown_tier():
    with pytest.raises(ValueError, match="unknown model tier"):
        build_model(Config(values={}), tier="nope")


def test_build_model_configures_deep_botocore_retries(monkeypatch):
    """The model-layer retry gap: the Bedrock client must carry adaptive botocore retries, not the
    legacy default (~4 narrow attempts) that fails a whole fire-and-forget run on a throttling
    window. (Mid-stream drops are covered one level up by serving.agentcore._run's retry loop.)"""
    import strands.models

    from strandly_harness.core.constants import MODEL_BOTO_MAX_ATTEMPTS

    captured: dict = {}

    class FakeBedrockModel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(strands.models, "BedrockModel", FakeBedrockModel)
    build_model(Config(values={}))
    boto_cfg = captured["boto_client_config"]
    assert boto_cfg.retries == {"max_attempts": MODEL_BOTO_MAX_ATTEMPTS, "mode": "adaptive"}
    assert boto_cfg.read_timeout == 300
