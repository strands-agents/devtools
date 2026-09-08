from __future__ import annotations

from strandly_harness.core import config as config_mod
from strandly_harness.core.config import Config, _region_from_secret_arn


def _cfg(**values) -> Config:
    return Config(values=values)


def test_region_from_secret_arn():
    arn = "arn:aws:secretsmanager:us-west-2:111122223333:secret:strandly/prod/config-abc123"
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
    assert c.use_agentcore_sandbox is False
    assert c.use_agentcore_session is False
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


def test_agentcore_sandbox_and_session_gated_on_ids():
    c = _cfg(AGENTCORE_CODE_INTERPRETER_ID="ci-1", AGENTCORE_MEMORY_ID="mem-1")
    assert c.use_agentcore_sandbox is True
    assert c.use_agentcore_session is True
    assert c.memory_id == "mem-1"


def test_sandbox_without_session():
    c = _cfg(AGENTCORE_CODE_INTERPRETER_ID="ci-1")
    assert c.use_agentcore_sandbox is True
    assert c.use_agentcore_session is False  # no memory id → file session fallback


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
        STRANDLY_MENTION_HANDLE="@agent-of-mkmeral",
        STRANDLY_MENTION_ALLOWED_AUTHORS="mkmeral, alice ,",  # spaces + trailing comma tolerated
        STRANDLY_MENTION_SKIP_REPO="o/r",
        STRANDLY_DEDUP_TABLE="tbl",
        STRANDLY_RUNTIME_ARN="arn:...:runtime/x",
        AWS_REGION="us-west-2",
    )
    s = c.mention_poller
    assert s.handle == "agent-of-mkmeral"  # leading @ stripped
    assert s.allowed_authors == ("mkmeral", "alice")
    assert s.skip_repo == "o/r" and s.dedup_table == "tbl"
    assert s.runtime_arn == "arn:...:runtime/x" and s.region == "us-west-2"


def test_mention_poller_allowed_orgs_defaults_to_strands_pair():
    # The org-membership invoke gate defaults to the STRANDS_ORGS pair when the env key is unset.
    from strandly_harness.core.constants import STRANDS_ORGS

    assert _cfg().mention_poller.allowed_orgs == STRANDS_ORGS
    assert STRANDS_ORGS == ("strands-agents", "strands-labs")


def test_mention_poller_allowed_orgs_overridable_via_env():
    c = _cfg(STRANDLY_MENTION_ALLOWED_ORGS="my-org, other-org ,")  # spaces + trailing comma tolerated
    assert c.mention_poller.allowed_orgs == ("my-org", "other-org")


def test_mention_poller_allowed_orgs_empty_env_falls_back_to_default():
    # An empty value is treated as unset → the default pair (NOT "no gating").
    from strandly_harness.core.constants import STRANDS_ORGS

    assert _cfg(STRANDLY_MENTION_ALLOWED_ORGS="").mention_poller.allowed_orgs == STRANDS_ORGS
    assert _cfg(STRANDLY_MENTION_ALLOWED_ORGS="  , ,").mention_poller.allowed_orgs == STRANDS_ORGS


def test_actor_id_defaults_to_constant():
    # Brittle os.environ["USER"] is gone; the default is a stable constant so reader/writer agree.
    from strandly_harness.core.constants import DEFAULT_ACTOR_ID

    assert _cfg().actor_id == DEFAULT_ACTOR_ID


def test_actor_id_overridable():
    assert _cfg(STRANDLY_ACTOR_ID="my-actor").actor_id == "my-actor"


# ---- github owner write allow-list (use_github guardrail) ---------------------------

def test_github_allow_list_defaults_to_strands_orgs():
    # The owner write allow-list is ON by default and hardcoded to the Strands orgs.
    from strandly_harness.core.constants import STRANDS_ORG_OWNERS

    gh = _cfg().github
    assert gh.allowed_owners == ("strands-agents", "strands-labs")
    assert gh.allowed_owners == STRANDS_ORG_OWNERS
    assert gh.internal_owners == gh.allowed_owners  # internal mirrors the resolved allow-list
    assert gh.strict_mutations is True  # unverifiable mutation targets stay blocked


def test_github_allow_list_overridable_via_env():
    gh = _cfg(STRANDLY_ALLOWED_OWNERS="foo,bar").github
    assert gh.allowed_owners == ("foo", "bar")
    assert gh.internal_owners == ("foo", "bar")


def test_github_allow_list_override_tolerates_spaces_and_blanks():
    gh = _cfg(STRANDLY_ALLOWED_OWNERS=" foo , , bar ,").github
    assert gh.allowed_owners == ("foo", "bar")


def test_github_allow_list_empty_env_falls_back_to_default():
    # An empty / whitespace-only override must NOT disable the guardrail — it falls back.
    from strandly_harness.core.constants import STRANDS_ORG_OWNERS

    assert _cfg(STRANDLY_ALLOWED_OWNERS="").github.allowed_owners == STRANDS_ORG_OWNERS
    assert _cfg(STRANDLY_ALLOWED_OWNERS="  ,  ").github.allowed_owners == STRANDS_ORG_OWNERS


def test_allowed_owners_accepts_bare_owners_and_repo_globs():
    # STRANDLY_ALLOWED_OWNERS overrides the default and accepts both bare owners and owner/repo
    # globs verbatim — the tool's matcher interprets them. This is how a deployment grants specific
    # external repos (the AgentCore packages) without opening a whole org.
    c = _cfg(STRANDLY_ALLOWED_OWNERS="strands-agents, aws/bedrock-agentcore-*, aws/agentcore-cli")
    assert c.github.allowed_owners == ("strands-agents", "aws/bedrock-agentcore-*", "aws/agentcore-cli")


def test_allowed_owners_defaults_to_strands_orgs():
    from strandly_harness.core.constants import STRANDS_ORG_OWNERS
    assert _cfg().github.allowed_owners == STRANDS_ORG_OWNERS
