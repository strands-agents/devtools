"""Public profile and authorization contract; no deployment or network calls."""
from __future__ import annotations

import json

import pytest

from strandly.core.config import Config
from strandly.extensions import load_extension, skills_source_dir
from strandly.profiles import Deployment, Profile
from strandly.tools.github_guardrails import validate_rest_path


def profile_data(name="example-user", account="111111111111"):
    return {
        "identity": {
            "display_name": "Example agent", "github_login": name, "mention_handle": name,
            "managed_repositories": [f"{name}/example"], "allowed_authors": ["example-maintainer"],
        },
        "deployment": {
            "name": "example-runtime", "environment": "preview", "account": account,
            "region": "us-west-2", "secret_references": {"config": "example/preview/config"},
        },
    }


def test_generic_install_is_not_authorized():
    config = Config({})
    assert config.github.allowed_owners == ()
    assert config.mention_poller.allowed_orgs == ()
    assert not config.mention_poller.is_authorized("unknown")
    error, _ = validate_rest_path("/repos/example/repo/issues", "POST", allowed_owners=set())
    assert error


def test_identity_is_not_an_aws_account_or_org(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile_data()))
    profile = Profile.load(path)
    assert profile.deployment.account == "111111111111"
    assert profile.deployment.name == "example-runtime"
    assert profile.identity.allowed_orgs == ()
    config = Config({"STRANDLY_PROFILE": str(path)})
    assert config.github.allowed_owners == ("example-user/example",)
    assert config.mention_poller.handle == "example-user"


def test_explicit_empty_revokes_profile_authority(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile_data()))
    config = Config({"STRANDLY_PROFILE": str(path), "STRANDLY_ALLOWED_OWNERS": ""})
    assert config.github.allowed_owners == ()


def test_deployment_does_not_infer_missing_coordinates():
    with pytest.raises(ValueError, match="Deployment requires explicit"):
        Profile().deployment_context()
    with pytest.raises(ValueError, match="account"):
        Deployment("example", "dev", "example-user", "us-west-2", {"config": "example"})
    with pytest.raises(ValueError, match="secret_references.config"):
        Deployment("example", "dev", "111111111111", "us-west-2", {})


def test_configured_missing_provider_is_fatal(tmp_path):
    with pytest.raises(RuntimeError, match="install its provider"):
        load_extension("example_uninstalled_addon:skills")
    with pytest.raises(RuntimeError, match="install its provider"):
        skills_source_dir(tmp_path, ("example_uninstalled_addon:skills",))


def test_profile_rejects_combined_aws_names_that_exceed_service_limits():
    with pytest.raises(ValueError, match="32-character stem"):
        Deployment("a" * 24, "b" * 24, "111111111111", "us-west-2", {"config": "example/config"})
    assert Deployment("a" * 24, "b" * 7, "111111111111", "us-west-2", {"config": "example/config"})

def test_runtime_profile_loads_explicit_secret_region_without_broadening(tmp_path, monkeypatch):
    path = tmp_path / "profile.json"
    data = profile_data()
    path.write_text(json.dumps(data))
    calls = []

    def load_secret(reference, region):
        calls.append((reference, region))
        return {"STRANDLY_ALLOWED_OWNERS": "restricted/example"}

    monkeypatch.setattr("strandly.core.config._load_secret", load_secret)
    config = Config.load({"STRANDLY_PROFILE": str(path)})
    assert calls == [("example/preview/config", "us-west-2")]
    assert config.aws_region == "us-west-2"
    assert config.github.allowed_owners == ("restricted/example",)
    revoked = Config.load({"STRANDLY_PROFILE": str(path), "STRANDLY_ALLOWED_OWNERS": ""})
    assert not revoked.github.allowed_owners


def test_materialized_profile_rejects_legacy_cli_coordinates(tmp_path):
    from strandly.deployment import validate_profile_coordinates
    coordinates = {"name": "example-runtime", "env": "preview", "account": "111111111111", "region": "us-west-2"}
    (tmp_path / "cdk.json").write_text(json.dumps({"context": {"profile_coordinates": coordinates}}))
    validate_profile_coordinates(tmp_path, name="example-runtime", environment="preview", region="us-west-2")
    with pytest.raises(ValueError, match="conflict with profile coordinates"):
        validate_profile_coordinates(tmp_path, name="strandly", environment="prod", region="us-west-2")


def test_runtime_skill_scope_follows_deployment_not_github_identity(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile_data()))
    config = Config({"STRANDLY_PROFILE": str(path)})
    assert "ManagedBy=example-runtime-preview" in config.aws_test_scope
    assert "example-runtime-preview-managed" in config.aws_test_scope
    assert "example-user" not in config.aws_test_scope
    assert Config({}).aws_test_scope == ""


def test_combined_coordinate_limit_is_checked_before_infrastructure():
    with pytest.raises(ValueError, match="32-character stem"):
        Deployment("a" * 24, "b" * 24, "111111111111", "us-west-2", {"config": "example/config"})
    assert Deployment("a" * 24, "b" * 7, "111111111111", "us-west-2", {"config": "example/config"})


def _deployment(name: str, environment: str) -> Deployment:
    return Deployment(name, environment, "111111111111", "us-west-2", {"config": "example/config"})


def test_environment_cannot_contain_the_coordinate_delimiter():
    # Both pairs would otherwise derive `example-agent-preview` state and authorization names.
    assert _deployment("example-agent", "preview")
    with pytest.raises(ValueError, match="deployment.environment must be lowercase letters and digits"):
        _deployment("example", "agent-preview")


@pytest.mark.parametrize("environment", ["pre-view", "preview-", "a-b-c"])
def test_environment_rejects_hyphens_anywhere(environment):
    with pytest.raises(ValueError, match="deployment.environment must be lowercase letters and digits"):
        _deployment("example", environment)


@pytest.mark.parametrize(("name", "environment", "field"), [
    ("example", "", "environment"), ("example", "-preview", "environment"), ("example", "pre_view", "environment"),
    ("example", "Preview", "environment"), ("example", "1preview", "environment"),
    ("", "preview", "name"), ("-example", "preview", "name"), ("Example", "preview", "name"),
    ("example_agent", "preview", "name"), ("1example", "preview", "name"),
])
def test_coordinates_stay_lowercase_and_start_with_a_letter(name, environment, field):
    with pytest.raises(ValueError, match=f"deployment.{field} must be"):
        _deployment(name, environment)


def test_accepted_coordinates_never_share_runtime_authorization_scopes():
    names = ("a", "b", "ab", "a-b", "a-b-c", "b-c", "a-bc", "a--b", "a-", "example", "example-agent")
    environments = ("a", "b", "c", "bc", "b1", "preview", "agentpreview")
    scopes = {
        key: {Profile(deployment=_deployment(name, environment)).runtime_values()[key]
              for name in names for environment in environments}
        for key in ("STRANDLY_TEST_MANAGED_BY_VALUE", "STRANDLY_TEST_NAME_PREFIX", "STRANDLY_PROTECTED_MANAGED_BY_VALUE")
    }
    assert {key: len(values) for key, values in scopes.items()} == dict.fromkeys(scopes, len(names) * len(environments))


@pytest.mark.parametrize(("name", "environment"), [
    ("x-prod", "infra"), ("x-prod", "managed"), ("x-prod", "sandbox"),
    ("x-prod-managed", "y"), ("x-infra", "prod"), ("strandly-sandbox-a", "prod"),
])
def test_reserved_words_cannot_follow_a_hyphen(name, environment):
    with pytest.raises(ValueError, match="cannot be deployment.environment or a deployment.name segment"):
        _deployment(name, environment)


def test_accepted_coordinates_never_alias_scopes_across_functions():
    words = ("x", "prod", "y", "infra", "managed", "sandbox")
    names = {"-".join(parts) for parts in (*((a,) for a in words), *((a, b) for a in words for b in words),
                                         *((a, b, c) for a in words for b in words for c in words))}
    accepted = []
    for name in sorted(names):
        for environment in words:
            try:
                accepted.append(Profile(deployment=_deployment(name, environment)).runtime_values())
            except ValueError:
                pass
    tags = [v[key] for v in accepted for key in ("STRANDLY_TEST_MANAGED_BY_VALUE", "STRANDLY_PROTECTED_MANAGED_BY_VALUE")]
    assert len(tags) == len(set(tags))
    prefixes = [v["STRANDLY_TEST_NAME_PREFIX"] for v in accepted]
    assert not [(a, b) for a in prefixes for b in prefixes if a != b and b.startswith(a + "-")]
    assert len(accepted) > 100
    assert _deployment("managed", "prod") and _deployment("sandbox-x", "prod")  # a first segment is fine
