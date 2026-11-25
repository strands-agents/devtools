from __future__ import annotations

import pytest

from strandly.core import config as config_mod
from strandly.core.config import Config, _region_from_secret_arn


def _cfg(**values) -> Config:
    return Config(values=values)


def test_region_from_secret_arn():
    arn = "arn:aws:secretsmanager:us-west-2:111111111111:secret:strandly/prod/config-hqhx10"
    assert _region_from_secret_arn(arn) == "us-west-2"
    # A non-ARN / malformed value yields None rather than raising.
    assert _region_from_secret_arn("not-an-arn") is None


def test_load_secret_falls_back_to_arn_region_when_no_env_region(monkeypatch):
    # Regression (prod): the AgentCore runtime container has no AWS_REGION, so a secret load that
    # relies on the passed-in region raised NoRegionError and silently left the config empty (no
    # GitHub token). The region baked into the ARN must be used as the fallback.
    arn = "arn:aws:secretsmanager:eu-central-1:111122223333:secret:strandly/prod/config-abc123"
    seen = {}

    class _FakeClient:
        def get_secret_value(self, SecretId):  # noqa: N803 — boto3 kwarg name
            return {"SecretString": '{"STRANDLY_GITHUB_TOKEN": "ghp_x"}'}

    class _FakeSession:
        def __init__(self, region_name=None):
            seen["region"] = region_name

        def client(self, name):
            return _FakeClient()

    import boto3

    monkeypatch.setattr(boto3, "Session", _FakeSession)
    out = config_mod._load_secret(arn, None)  # None region → must derive from the ARN
    assert seen["region"] == "eu-central-1"
    assert out["STRANDLY_GITHUB_TOKEN"] == "ghp_x"


def test_defaults_all_capabilities_off():
    c = _cfg()
    assert c.github_enabled is False
    assert c.search_mcp_url is None
    assert c.sandbox_runtime_arn is None
    assert c.session_bucket is None  # local session store
    assert c.boto_session() is None  # ambient fallback


def test_github_gated_on_token():
    assert _cfg(STRANDLY_GITHUB_TOKEN="ghp_x").github_enabled is True


def test_github_token_resolution_order():
    # Same precedence as the use_github tool (GitHubSettings.token_env), but resolved through
    # Config.get so a Secrets-Manager-only deployment (values dict, no process env) also finds it.
    assert _cfg().github_token is None
    assert _cfg(PAT_TOKEN="pat").github_token == "pat"
    assert _cfg(GITHUB_TOKEN="gh", PAT_TOKEN="pat").github_token == "gh"
    assert _cfg(STRANDLY_GITHUB_TOKEN="strandly", GITHUB_TOKEN="gh").github_token == "strandly"
    # Empty strings are "unset", matching Config.get's falsy-to-None coercion.
    assert _cfg(STRANDLY_GITHUB_TOKEN="", GITHUB_TOKEN="gh").github_token == "gh"


def test_search_mcp_gated_on_url():
    c = _cfg(STRANDLY_SEARCH_MCP_URL="https://mcp.example/search", STRANDLY_SEARCH_MCP_TOKEN="t")
    assert c.search_mcp_url == "https://mcp.example/search"
    assert c.search_mcp_token == "t"


def test_search_mcp_sigv4_defaults_off():
    c = _cfg(STRANDLY_SEARCH_MCP_URL="https://mcp.example/search")
    assert c.search_mcp_sigv4 is False


def test_search_mcp_sigv4_on_when_configured():
    c = _cfg(STRANDLY_SEARCH_MCP_AUTH="sigv4")
    assert c.search_mcp_sigv4 is True
    # Case-insensitive; anything else (including a typo) stays off rather than silently signing.
    assert _cfg(STRANDLY_SEARCH_MCP_AUTH="SigV4").search_mcp_sigv4 is True
    assert _cfg(STRANDLY_SEARCH_MCP_AUTH="bearer").search_mcp_sigv4 is False


def test_search_mcp_region_falls_back_to_aws_region():
    assert _cfg(STRANDLY_SEARCH_MCP_REGION="us-east-1").search_mcp_region == "us-east-1"
    assert _cfg(AWS_REGION="us-west-2").search_mcp_region == "us-west-2"
    # An explicit region wins over the deployment's ambient region — the Gateway may live in a
    # different region (the web-search connector is us-east-1-only) than the rest of the stack.
    assert (
        _cfg(STRANDLY_SEARCH_MCP_REGION="us-east-1", AWS_REGION="us-west-2").search_mcp_region
        == "us-east-1"
    )


def test_agentcore_sandbox_and_session_gated_on_ids():
    c = _cfg(
        STRANDLY_SANDBOX_RUNTIME_ARN="arn:aws:bedrock-agentcore:us-west-2:1:runtime/sbx",
        AGENTCORE_MEMORY_ID="mem-1",
    )
    assert c.sandbox_runtime_arn == "arn:aws:bedrock-agentcore:us-west-2:1:runtime/sbx"
    # The Memory id does NOT select a session backend; the bucket does.
    assert c.memory_id == "mem-1"
    assert c.session_bucket is None


def test_session_bucket_selects_the_s3_store():
    assert _cfg(STRANDLY_SESSION_BUCKET="strandly-dev-sessions-1").session_bucket == (
        "strandly-dev-sessions-1"
    )


def test_sandbox_without_session():
    c = _cfg(STRANDLY_SANDBOX_RUNTIME_ARN="arn:aws:bedrock-agentcore:us-west-2:1:runtime/sbx")
    assert c.sandbox_runtime_arn == "arn:aws:bedrock-agentcore:us-west-2:1:runtime/sbx"
    assert c.session_bucket is None  # no bucket → local session store


def test_sandbox_runtime_qualifier_is_optional():
    """Unset ⇒ None, so the service resolves its own default endpoint (see Config docstring)."""
    arn = "arn:aws:bedrock-agentcore:us-west-2:1:runtime/sbx"
    assert _cfg(STRANDLY_SANDBOX_RUNTIME_ARN=arn).sandbox_runtime_qualifier is None
    c = _cfg(STRANDLY_SANDBOX_RUNTIME_ARN=arn, STRANDLY_SANDBOX_RUNTIME_QUALIFIER="DEFAULT")
    assert c.sandbox_runtime_qualifier == "DEFAULT"


def test_load_from_env_dict():
    c = Config.load(env={"STRANDLY_GITHUB_TOKEN": "x", "AWS_REGION": "us-west-2"})
    assert c.github_enabled and c.aws_region == "us-west-2"


def test_dotenv_loaded(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("STRANDLY_GITHUB_TOKEN=fromdotenv\n# comment\n")
    monkeypatch.chdir(tmp_path)
    c = Config.load(env={})  # no STRANDLY_SECRETS_ARN → reads ./.env
    assert c.values.get("STRANDLY_GITHUB_TOKEN") == "fromdotenv"


def test_env_wins_over_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("AWS_REGION=eu-west-1\n")
    monkeypatch.chdir(tmp_path)
    c = Config.load(env={"AWS_REGION": "us-east-1"})
    assert c.aws_region == "us-east-1"


# ---- mention poller (ingress) gating + settings -------------------------------------

def test_poller_disabled_without_token_or_arn():
    assert _cfg().poller_enabled is False
    assert _cfg(STRANDLY_NOTIFICATIONS_TOKEN="ghp_x").poller_enabled is False  # no runtime arn
    assert _cfg(STRANDLY_RUNTIME_ARN="arn:...:runtime/x").poller_enabled is False  # no token


def test_poller_enabled_with_token_and_arn():
    c = _cfg(STRANDLY_NOTIFICATIONS_TOKEN="ghp_x", STRANDLY_RUNTIME_ARN="arn:...:runtime/x")
    assert c.poller_enabled is True


def test_notifications_token_falls_back_to_github_token():
    assert _cfg(STRANDLY_GITHUB_TOKEN="ghp_gh").notifications_token == "ghp_gh"
    assert _cfg(STRANDLY_NOTIFICATIONS_TOKEN="ghp_n", STRANDLY_GITHUB_TOKEN="ghp_gh").notifications_token == "ghp_n"


def test_mention_poller_settings_parsed():
    c = _cfg(
        STRANDLY_MENTION_HANDLE="@example-user",
        STRANDLY_MENTION_ALLOWED_AUTHORS="example-maintainer, alice ,",  # spaces + trailing comma tolerated
        STRANDLY_MENTION_SKIP_REPO="o/r",
        STRANDLY_DEDUP_TABLE="tbl",
        STRANDLY_PEOPLE_TABLE="people-tbl",
        STRANDLY_RUNTIME_ARN="arn:...:runtime/x",
        AWS_REGION="us-west-2",
    )
    s = c.mention_poller
    assert s.handle == "example-user"  # leading @ stripped
    assert s.allowed_authors == ("example-maintainer", "alice")
    assert s.skip_repo == "o/r" and s.dedup_table == "tbl"
    assert s.people_table == "people-tbl"
    assert s.runtime_arn == "arn:...:runtime/x" and s.region == "us-west-2"


def test_mention_poller_people_table_defaults_to_none():
    # No table ⇒ the people directory is disabled and the ingresses use the static lists alone.
    assert _cfg().mention_poller.people_table is None


def test_mention_poller_allowed_orgs_defaults_to_strands_pair():
    # The org-membership invoke gate defaults to the STRANDS_ORGS pair when the env key is unset.
    from strandly.core.constants import STRANDS_ORGS

    assert _cfg().mention_poller.allowed_orgs == STRANDS_ORGS
    assert STRANDS_ORGS == ()


def test_mention_poller_allowed_orgs_overridable_via_env():
    c = _cfg(STRANDLY_MENTION_ALLOWED_ORGS="my-org, other-org ,")  # spaces + trailing comma tolerated
    assert c.mention_poller.allowed_orgs == ("my-org", "other-org")


def test_mention_poller_allowed_orgs_empty_env_falls_back_to_default():
    # An empty value is treated as unset → the default pair (NOT "no gating").
    from strandly.core.constants import STRANDS_ORGS

    assert _cfg(STRANDLY_MENTION_ALLOWED_ORGS="").mention_poller.allowed_orgs == STRANDS_ORGS
    assert _cfg(STRANDLY_MENTION_ALLOWED_ORGS="  , ,").mention_poller.allowed_orgs == STRANDS_ORGS


# ---- github owner write allow-list (use_github guardrail) ---------------------------

def test_github_allow_list_defaults_to_strands_orgs():
    # The owner write allow-list is ON by default and hardcoded to the Strands orgs.
    from strandly.core.constants import STRANDS_ORG_OWNERS

    gh = _cfg().github
    assert gh.allowed_owners == ()
    assert gh.allowed_owners == STRANDS_ORG_OWNERS
    assert gh.internal_owners == gh.allowed_owners  # internal mirrors the resolved allow-list
    assert gh.strict_mutations is True  # unverifiable mutation targets stay blocked


def test_github_allow_list_overridable_via_env():
    gh = _cfg(STRANDLY_ALLOWED_OWNERS="foo,bar").github
    assert gh.allowed_owners == ("foo", "bar")
    assert gh.internal_owners == ("foo", "bar")


def test_github_throttle_on_by_default_with_limit_50():
    gh = _cfg().github
    assert gh.throttle_enabled is True
    assert gh.throttle_limit == 50


@pytest.mark.parametrize(
    ("raw", "enabled"),
    [("false", False), (" FALSE ", False), ("true", True), ("off", True), ("", True)],
)
def test_github_throttle_disabled_only_by_explicit_false(raw, enabled):
    assert _cfg(STRANDLY_GITHUB_THROTTLE_ENABLED=raw).github.throttle_enabled is enabled


@pytest.mark.parametrize(("raw", "limit"), [("10", 10), ("0", 50), ("-3", 50), ("ten", 50)])
def test_github_throttle_limit_from_config(raw, limit):
    assert _cfg(STRANDLY_GITHUB_THROTTLE_LIMIT=raw).github.throttle_limit == limit


def test_github_allow_list_override_tolerates_spaces_and_blanks():
    gh = _cfg(STRANDLY_ALLOWED_OWNERS=" foo , , bar ,").github
    assert gh.allowed_owners == ("foo", "bar")


def test_github_allow_list_empty_env_falls_back_to_default():
    # An empty / whitespace-only override must NOT disable the guardrail — it falls back.
    from strandly.core.constants import STRANDS_ORG_OWNERS

    assert _cfg(STRANDLY_ALLOWED_OWNERS="").github.allowed_owners == STRANDS_ORG_OWNERS
    assert _cfg(STRANDLY_ALLOWED_OWNERS="  ,  ").github.allowed_owners == STRANDS_ORG_OWNERS


def test_allowed_owners_accepts_bare_owners_and_repo_globs():
    # STRANDLY_ALLOWED_OWNERS overrides the default and accepts both bare owners and owner/repo
    # globs verbatim — the tool's matcher interprets them. This is how a deployment grants specific
    # external repos (the AgentCore packages) without opening a whole org.
    c = _cfg(STRANDLY_ALLOWED_OWNERS="strands-agents, aws/bedrock-agentcore-*, aws/agentcore-cli")
    assert c.github.allowed_owners == ("strands-agents", "aws/bedrock-agentcore-*", "aws/agentcore-cli")


def test_allowed_owners_defaults_to_strands_orgs():
    from strandly.core.constants import STRANDS_ORG_OWNERS
    assert _cfg().github.allowed_owners == STRANDS_ORG_OWNERS


# ---- sandbox git token (separate, weaker credential than use_github's) --------------

def test_sandbox_git_token_absent_by_default():
    assert _cfg().sandbox_git_token is None


def test_sandbox_git_token_read_from_its_own_key():
    assert _cfg(STRANDLY_SANDBOX_GIT_TOKEN="github_pat_x").sandbox_git_token == "github_pat_x"


def test_sandbox_git_token_never_falls_back_to_github_token():
    # THE point of the key: the credential inside the sandbox must be weaker than the one the
    # use_github tool holds. Any code the agent runs can read the sandbox credential (`git
    # credential fill` was observed in prod), so a fallback here would silently reinstate a
    # full-privilege, impersonation-capable token in the blast radius.
    c = _cfg(STRANDLY_GITHUB_TOKEN="ghp_full_privilege", GITHUB_TOKEN="gh", PAT_TOKEN="pat")
    assert c.github_token == "ghp_full_privilege"  # the tool still resolves its own token
    assert c.sandbox_git_token is None  # ...but the sandbox gets nothing


def test_sandbox_git_token_empty_is_unset():
    # Config.get coerces falsy to None, so an empty value must not become an empty-string credential
    # (which would write a broken ~/.git-credentials line rather than skipping the bootstrap).
    assert _cfg(STRANDLY_SANDBOX_GIT_TOKEN="").sandbox_git_token is None


def test_slack_gated_on_bot_token():
    assert not Config(values={}).slack_enabled
    assert Config(values={"STRANDLY_SLACK_BOT_TOKEN": "xoxb-1"}).slack_enabled


def test_slack_settings_parsed():
    cfg = Config(
        values={
            "STRANDLY_SLACK_BOT_TOKEN": "xoxb-1",
            "STRANDLY_SLACK_ALLOWED_USERS": " U111 , U222 ,, ",
        }
    )
    s = cfg.slack
    assert s.bot_token == "xoxb-1"
    assert s.allowed_users == ("U111", "U222")


def test_slack_signing_secret_never_a_config_property():
    # The signing secret is ingress-Lambda-only: Config must not surface it to the agent side.
    cfg = Config(values={"STRANDLY_SLACK_SIGNING_SECRET": "s3cr3t"})
    assert not cfg.slack_enabled
    assert cfg.slack.bot_token == ""
    assert not hasattr(cfg, "slack_signing_secret")


def test_slack_home_channel():
    token = {"STRANDLY_SLACK_BOT_TOKEN": "xoxb-1", "STRANDLY_SLACK_HOME_CHANNEL_ENABLED": "true"}
    assert Config(values=token).slack_home_channel is None
    cfg = Config(values={**token, "STRANDLY_SLACK_HOME_CHANNEL": " C0HOME "})
    assert cfg.slack_home_channel == "C0HOME" and cfg.slack.home_channel == "C0HOME"
    assert Config(values={**token, "STRANDLY_SLACK_HOME_CHANNEL": "G0PRIV"}).slack_home_channel
    # A DM id, a name or no bot token: no home channel.
    assert Config(values={**token, "STRANDLY_SLACK_HOME_CHANNEL": "D0DM"}).slack_home_channel is None
    assert Config(values={**token, "STRANDLY_SLACK_HOME_CHANNEL": "#strandly"}).slack_home_channel is None
    assert Config(values={"STRANDLY_SLACK_HOME_CHANNEL": "C0HOME"}).slack_home_channel is None


def test_user_text_offload_tokens_default_override_and_bad_values():
    from strandly.core.constants import CONTEXT_USER_TEXT_THRESHOLD_TOKENS as default

    assert Config(values={}).user_text_offload_tokens == default
    key = "STRANDLY_USER_TEXT_OFFLOAD_TOKENS"
    assert Config(values={key: "80000"}).user_text_offload_tokens == 80_000
    # Not a number, or at/below the 4k preview the SDK cuts to: the default.
    for bad in ("lots", "4000", "-1"):
        assert Config(values={key: bad}).user_text_offload_tokens == default
