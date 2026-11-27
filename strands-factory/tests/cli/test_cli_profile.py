"""Client profiles: which coordinates a generated CLI may bake in."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strandly_cli.profile import ClientProfile, ProfileError, load_client_profile

ROOT = Path(__file__).resolve().parents[2]
PORTAL = {"command": "strandly-example", "display_name": "Example", "portal_url": "https://portal.example.com/"}
TRIPLE = {
    "command": "strandly-example",
    "display_name": "Example",
    "api_url": "https://api.example.com",
    "cognito_domain": "https://example.auth.us-west-2.amazoncognito.com",
    "client_id": "abc123",
}


def test_portal_only_profile_is_enough_and_urls_are_normalized():
    profile = ClientProfile.from_dict(PORTAL)
    assert profile.portal_url == "https://portal.example.com"
    assert profile.to_dict() == {**PORTAL, "portal_url": "https://portal.example.com"}


def test_direct_login_coordinates_go_together():
    assert ClientProfile.from_dict(TRIPLE).client_id == "abc123"
    with pytest.raises(ProfileError, match="go together"):
        ClientProfile.from_dict({k: v for k, v in TRIPLE.items() if k != "client_id"})
    with pytest.raises(ProfileError, match="needs portal_url"):
        ClientProfile.from_dict({"command": "strandly-example", "display_name": "Example"})


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("portal_url", "http://portal.example.com", "https"),
        ("portal_url", "https://user:pw@portal.example.com", "https"),
        ("portal_url", "https://portal.example.com/?token=x", "https"),
        ("portal_url", "REQUIRED_PORTAL_URL", "placeholder"),
        ("command", "Strandly", "lowercase"),
        ("command", "strandly-", "lowercase"),
        ("command", "x", "lowercase"),
        ("region", "west", "region"),
        ("runtime_arn", "arn:aws:lambda:us-west-2:111111111111:function:x", "runtime ARN"),
        ("default_org", "not an org", "organization"),
        ("display_name", "", "nonempty"),
    ],
)
def test_invalid_values_name_the_field(field, value, message):
    with pytest.raises(ProfileError, match=message):
        ClientProfile.from_dict({**PORTAL, field: value})


def test_numeric_loopback_http_is_accepted_for_local_stubs():
    assert ClientProfile.from_dict({**PORTAL, "portal_url": "http://127.0.0.1:8080"}).portal_url


def test_unknown_client_fields_are_rejected():
    with pytest.raises(ProfileError, match="Unknown client fields"):
        ClientProfile.from_dict({**PORTAL, "secret": "x"})


def test_runtime_arn_supplies_and_must_match_the_region():
    arn = "arn:aws:bedrock-agentcore:us-west-2:111111111111:runtime/example_agent-AbC123"
    assert ClientProfile.from_dict({**PORTAL, "runtime_arn": arn}).region == "us-west-2"
    with pytest.raises(ProfileError, match="does not match"):
        ClientProfile.from_dict({**PORTAL, "runtime_arn": arn, "region": "us-east-1"})


def test_each_cli_has_its_own_login_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    a = ClientProfile.from_dict(PORTAL)
    b = ClientProfile.from_dict({**PORTAL, "command": "strandly-other"})
    assert a.auth_file() == tmp_path / ".strandly" / "clients" / "strandly-example" / "auth.json"
    assert a.auth_file() != b.auth_file() != tmp_path / ".strandly" / "auth.json"


def test_profile_file_reads_only_the_client_section_and_display_name(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(
            {
                "identity": {"display_name": "Agent of Example"},
                # Deployment placeholders do not matter to a client build.
                "deployment": {"account": "REQUIRED_AWS_ACCOUNT"},
                "context": {"secret": "never baked"},
                "client": {"command": "strandly-example", "portal_url": "https://portal.example.com"},
            }
        )
    )
    profile = load_client_profile(path)
    assert profile.display_name == "Agent of Example"
    assert "secret" not in json.dumps(profile.to_dict())
    assert load_client_profile(path, command="strandly-renamed").command == "strandly-renamed"


def test_profile_without_client_section_is_an_error(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"identity": {}}))
    with pytest.raises(ProfileError, match="no client section"):
        load_client_profile(path)


def test_the_public_example_profile_has_a_buildable_client_section():
    profile = load_client_profile(ROOT / "infra" / "example-profile.json")
    assert profile.command == "strandly-example"
    assert profile.portal_url == "https://portal.example.com"


def test_the_harness_profile_loader_accepts_the_client_section():
    from strandly.profiles import Profile

    assert Profile.load(ROOT / "infra" / "example-profile.json").deployment is not None
