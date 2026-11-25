"""The model strings resolve to the models and efforts production runs.

``create_harness`` builds every model string; a ``bedrock/`` Claude string becomes
``AnthropicModel`` on Anthropic's Bedrock client. These tests resolve strings the way it does
(``resolve_model`` with the default caching), so they pin what the deployed agent runs.
"""

from __future__ import annotations

import anthropic
import pytest
from strands.models import BedrockModel
from strands.models.anthropic import AnthropicModel
from strands_harness.defaults import DEFAULT_MODEL
from strands_harness.models import resolve_model, resolve_web_fetch_model

from strandly.core.config import Config
from strandly.core.constants import EFFORT_DEFAULT, MODEL_TIER_DEFAULT, MODEL_TIERS, WEB_FETCH_MODEL

_ENDPOINT_VARS = (
    "ANTHROPIC_BEDROCK_BASE_URL",
    "AWS_ENDPOINT_URL_BEDROCK_RUNTIME",
    "AWS_ENDPOINT_URL",
)


@pytest.fixture(autouse=True)
def _region(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    for name in _ENDPOINT_VARS:
        monkeypatch.delenv(name, raising=False)


def _claude(model: str, effort: str) -> AnthropicModel:
    built = resolve_model(model, DEFAULT_MODEL, effort=effort, caching=True)  # type: ignore[arg-type]
    assert isinstance(built, AnthropicModel)
    assert isinstance(built.client, anthropic.AsyncAnthropicBedrock)
    return built


def test_default_model_is_opus_5_5_at_medium_effort():
    config = Config(values={})
    assert config.model == DEFAULT_MODEL == "bedrock/global.anthropic.claude-opus-5-5"
    assert config.effort == EFFORT_DEFAULT == "medium"
    model = _claude(config.model, config.effort)
    settings = model.get_config()
    assert settings["model_id"] == "global.anthropic.claude-opus-5-5"
    assert settings["max_tokens"] == 128_000
    assert settings["params"] == {
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": "medium"},
    }
    # Tool definitions plus a cache point on the last user message; the system prompt is cached
    # automatically. Nothing else in the request varies per call, so the prefix stays stable.
    assert settings["cache_config"].strategy == "auto"
    assert settings["cache_config"].tools_ttl is True
    assert "anthropic_tools" not in settings
    assert model.client.aws_region == "us-west-2"
    assert (
        str(model.client.base_url).rstrip("/") == "https://bedrock-runtime.us-west-2.amazonaws.com"
    )
    # The SDK's model info: injected context as turn-scoped system messages, server-side summary.
    assert model.model_info.capabilities.temporary_messages is True
    assert model.model_info.capabilities.server_side_summary is True


@pytest.mark.parametrize(
    "model_id,effort",
    [
        ("global.anthropic.claude-opus-5-5", "medium"),
        ("global.anthropic.claude-fable-5-2", "max"),
        ("global.anthropic.claude-fable-5-2", "off"),
        ("global.anthropic.claude-haiku-5-5", "off"),
    ],
)
def test_bedrock_claude_matches_the_anthropic_path(monkeypatch, model_id, effort):
    """Bedrock Claude gets the request the Anthropic API would, on the Bedrock client."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    direct = resolve_model(f"anthropic/{model_id}", DEFAULT_MODEL, effort=effort, caching=True)  # type: ignore[arg-type]
    assert _claude(f"bedrock/{model_id}", effort).get_config() == direct.get_config()


def test_auto_effort_is_the_vendor_default():
    assert _claude("bedrock/global.anthropic.claude-fable-5-2", "auto").get_config()["params"][
        "output_config"
    ] == {"effort": "high"}
    assert _claude(DEFAULT_MODEL, "auto").get_config()["params"]["output_config"] == {
        "effort": "medium"
    }


def test_unsupported_effort_raises():
    with pytest.raises(ValueError, match="not supported"):
        _claude(DEFAULT_MODEL, "minimal")
    with pytest.raises(ValueError, match="Unknown effort"):
        _claude(DEFAULT_MODEL, "huge")


def test_strandly_model_and_effort_override_the_default():
    assert Config(values={"STRANDLY_MODEL": "  "}).model == DEFAULT_MODEL
    assert Config(values={"STRANDLY_MODEL": " bedrock/x "}).model == "bedrock/x"
    assert Config(values={"STRANDLY_EFFORT": " "}).effort == EFFORT_DEFAULT
    assert Config(values={"STRANDLY_EFFORT": " high "}).effort == "high"


def test_a_bare_bedrock_claude_id_takes_the_anthropic_client():
    assert (
        _claude("anthropic.claude-fable-5-2", "auto").get_config()["model_id"]
        == "anthropic.claude-fable-5-2"
    )


def test_other_bedrock_models_stay_on_converse():
    built = resolve_model("bedrock/us.amazon.nova-2-lite-v1:0", DEFAULT_MODEL, effort="medium")
    assert isinstance(built, BedrockModel)


def test_region_resolves_like_bedrock_model(monkeypatch):
    import boto3

    monkeypatch.setattr(boto3.Session, "region_name", property(lambda self: "eu-west-1"))
    assert _claude(DEFAULT_MODEL, "auto").client.aws_region == "eu-west-1"
    monkeypatch.setattr(boto3.Session, "region_name", property(lambda self: None))
    assert _claude(DEFAULT_MODEL, "auto").client.aws_region == "us-west-2"
    monkeypatch.delenv("AWS_REGION")
    assert _claude(DEFAULT_MODEL, "auto").client.aws_region == "us-west-2"  # BedrockModel's default


def test_aws_endpoint_overrides_pass_through(monkeypatch):
    def base_url() -> str:
        return str(_claude(DEFAULT_MODEL, "auto").client.base_url).rstrip("/")

    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://generic.example")
    assert base_url() == "https://generic.example"
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", "https://runtime.example")
    assert base_url() == "https://runtime.example"
    monkeypatch.setenv("ANTHROPIC_BEDROCK_BASE_URL", "https://anthropic.example")
    assert base_url() == "https://anthropic.example"


def test_tiers():
    assert set(MODEL_TIERS) == {"default", "fast", "advanced"}
    assert MODEL_TIERS[MODEL_TIER_DEFAULT] == (None, None)

    fast = _claude(*MODEL_TIERS["fast"]).get_config()  # type: ignore[arg-type]
    assert fast["model_id"] == "global.anthropic.claude-haiku-5-5"
    assert "params" not in fast

    advanced = _claude(*MODEL_TIERS["advanced"]).get_config()  # type: ignore[arg-type]
    assert advanced["model_id"] == "global.anthropic.claude-fable-5-2"
    assert advanced["params"]["output_config"] == {"effort": "max"}


def test_web_fetch_summarizer_is_haiku_5_5_without_thinking():
    summarizer = resolve_web_fetch_model(DEFAULT_MODEL, WEB_FETCH_MODEL)
    assert isinstance(summarizer, AnthropicModel)
    config = summarizer.get_config()
    assert config["model_id"] == "global.anthropic.claude-haiku-5-5"
    assert "params" not in config
