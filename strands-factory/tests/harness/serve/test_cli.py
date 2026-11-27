from __future__ import annotations

import pytest

from strandly.serve.cli.main import build_parser


def test_poll_parsed():
    a = build_parser().parse_args(["poll", "12345", "--session-id", "sess-abc"])
    assert a.command == "poll" and a.task_id == "12345" and a.session_id == "sess-abc"


def test_poll_invokes_runtime_client(monkeypatch):
    import strandly.ops.runtime_client as rc

    captured = {}
    monkeypatch.setattr(
        rc, "poll_run", lambda arn, region, sid, tid: captured.update(arn=arn, sid=sid, tid=tid) or {"status": "completed", "result": "done"}
    )
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    from strandly.serve.cli.main import main

    rc_code = main(["poll", "t-1", "--session-id", "s-1", "--runtime-arn", "arn:rt"])
    assert rc_code == 0
    assert captured == {"arn": "arn:rt", "sid": "s-1", "tid": "t-1"}


def test_deploy_parsed():
    a = build_parser().parse_args(
        ["deploy", "--stage", "dev", "--region", "us-west-2", "--env", "A=1", "--env", "B=2"]
    )
    assert a.command == "deploy" and a.region == "us-west-2" and a.env == ["A=1", "B=2"]
    assert a.stage == "dev" and a.app_name == "strandly"


def test_invoke_parsed():
    a = build_parser().parse_args(["invoke", "do it", "--session-id", "s-1"])
    assert a.command == "invoke" and a.prompt == "do it" and a.session_id == "s-1"


def test_invoke_resolves_arn_and_calls_runtime(monkeypatch):
    # invoke now routes through launch_run (fire-and-forget, no GitHub context); the session id is
    # forwarded for both instance affinity and the durable result channel.
    import strandly.ops.runtime_client as rc
    import strandly.serve.deploy as dep

    captured = {}
    monkeypatch.setattr(dep, "resolve_runtime_arn", lambda x: x or "arn:resolved")
    monkeypatch.setattr(dep, "resolve_region", lambda x: x or "us-west-2")
    monkeypatch.setattr(
        rc,
        "launch_run",
        lambda arn, region, sid, prompt, mode=None, trigger_source=None: captured.update(
            arn=arn, sid=sid, prompt=prompt, mode=mode, trigger_source=trigger_source
        )
        or {"status": "accepted", "taskId": "t9"},
    )
    from strandly.serve.cli.main import main

    assert main(["invoke", "hello", "--session-id", "s-1"]) == 0
    assert captured["arn"] == "arn:resolved" and captured["sid"] == "s-1"
    assert captured["prompt"] == "hello"
    assert captured["mode"] is None and captured["trigger_source"] is None


def test_invoke_forwards_mode_and_trigger_source(monkeypatch):
    """--mode / --trigger-source reach launch_run so the event Action stamps the ledger."""
    import strandly.ops.runtime_client as rc
    import strandly.serve.deploy as dep

    captured = {}
    monkeypatch.setattr(dep, "resolve_runtime_arn", lambda x: x or "arn:resolved")
    monkeypatch.setattr(dep, "resolve_region", lambda x: x or "us-west-2")
    monkeypatch.setattr(
        rc,
        "launch_run",
        lambda arn, region, sid, prompt, mode=None, trigger_source=None: captured.update(
            mode=mode, trigger_source=trigger_source
        )
        or {"status": "accepted", "taskId": "t9"},
    )
    from strandly.serve.cli.main import main

    assert main(["invoke", "review it", "--session-id", "s-1", "--mode", "draft",
                 "--trigger-source", "event:pull_request.opened"]) == 0
    assert captured["mode"] == "draft"
    assert captured["trigger_source"] == "event:pull_request.opened"


def test_chat_agentcore_parsed():
    a = build_parser().parse_args(["chat", "--agentcore", "--session-id", "sx"])
    assert a.command == "chat" and a.agentcore is True and a.session_id == "sx"


def test_chat_agentcore_needs_runtime(monkeypatch):
    # --agentcore requires a resolvable runtime ARN + region; missing → exit 1.
    import strandly.serve.deploy as dep

    monkeypatch.setattr(dep, "resolve_runtime_arn", lambda x: None)
    monkeypatch.setattr(dep, "resolve_region", lambda x: "us-west-2")
    from strandly.serve.cli.main import main

    assert main(["chat", "--agentcore"]) == 1


def test_chat_agentcore_streams_when_resolvable(monkeypatch):
    import strandly.serve.cli.repl as repl
    import strandly.serve.deploy as dep
    from strandly.core.config import Config

    monkeypatch.setattr(dep, "resolve_runtime_arn", lambda x: "arn:rt")
    monkeypatch.setattr(dep, "resolve_region", lambda x: "us-west-2")
    # No AGENTCORE_MEMORY_ID needed any more — the runtime owns its session store.
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: Config(values={})))
    captured = {}
    monkeypatch.setattr(
        repl, "chat_agentcore", lambda arn, region, sid: captured.update(arn=arn, region=region, sid=sid)
    )
    from strandly.serve.cli.main import main

    assert main(["chat", "--agentcore", "--session-id", "sx"]) == 0
    assert captured == {"arn": "arn:rt", "region": "us-west-2", "sid": "sx"}


def test_invoke_errors_without_arn(monkeypatch):
    import strandly.serve.deploy as dep

    monkeypatch.setattr(dep, "resolve_runtime_arn", lambda x: None)
    monkeypatch.setattr(dep, "resolve_region", lambda x: "us-west-2")
    from strandly.serve.cli.main import main

    assert main(["invoke", "hi", "--session-id", "s-1"]) == 1  # no ARN anywhere → error


def test_serve_mode():
    a = build_parser().parse_args(["serve", "mcp"])
    assert a.command == "serve" and a.mode == "mcp"


def test_serve_agentcore():
    a = build_parser().parse_args(["serve", "agentcore"])
    assert a.mode == "agentcore"


@pytest.mark.parametrize("argv", [["run", "x"], ["brief"], ["port", "--issue", "u"], ["serve", "mcp", "--local"]])
def test_local_agent_commands_are_gone(argv):
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


def test_serve_agentcore_starts_the_deployed_entrypoint(monkeypatch):
    import strandly.serve.agentcore_app as app
    from strandly.core.config import Config
    from strandly.serve.cli.main import main

    seen = []
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: Config(values={})))
    monkeypatch.setattr(app, "serve_agentcore", lambda config: seen.append(config))
    assert main(["serve", "agentcore"]) == 0
    assert len(seen) == 1


def test_provision_parsed():
    a = build_parser().parse_args(["provision", "--name", "strandly"])
    assert a.command == "provision" and a.name == "strandly"


def test_provision_has_no_knowledge_base_switch():
    # Long-term memory is AgentCore Memory records now; there is no KB to opt out of.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["provision", "--no-kb"])


def test_provision_env_defaults_to_dev():
    a = build_parser().parse_args(["provision"])
    assert a.env == "dev"


def test_provision_env_flag(monkeypatch):
    captured = {}
    import strandly.serve.provisioning as prov

    monkeypatch.setattr(prov, "provision", lambda **kw: captured.update(kw))
    from strandly.serve.cli.main import main

    assert main(["provision", "--env", "prod"]) == 0
    assert captured["env"] == "prod"
    assert "with_kb" not in captured


def test_provision_github_token_folded_into_secret(monkeypatch):
    captured = {}

    import strandly.serve.provisioning as prov

    monkeypatch.setattr(prov, "provision", lambda **kw: captured.update(kw))
    from strandly.serve.cli.main import main

    assert main(["provision", "--github-token", "ghp_secret"]) == 0
    assert captured["extra_secrets"] == {"STRANDLY_GITHUB_TOKEN": "ghp_secret"}


def test_provision_without_github_token_passes_none(monkeypatch):
    captured = {}

    import strandly.serve.provisioning as prov

    monkeypatch.setattr(prov, "provision", lambda **kw: captured.update(kw))
    from strandly.serve.cli.main import main

    assert main(["provision"]) == 0
    assert captured["extra_secrets"] is None  # no token flag → nothing folded in


def test_serve_rejects_bad_mode():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["serve", "telepathy"])


def test_requires_command():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_serve_mcp_agentcore_parsed():
    a = build_parser().parse_args(["serve", "mcp", "--agentcore", "--runtime-arn", "arn:rt"])
    assert a.command == "serve" and a.mode == "mcp" and a.agentcore is True
    assert a.runtime_arn == "arn:rt"


def test_serve_mcp_agentcore_starts_the_proxy(monkeypatch):
    import strandly.serve.deploy as dep
    import strandly_mcp.server as proxy

    captured = {}
    monkeypatch.setattr(dep, "resolve_runtime_arn", lambda x: x or "arn:resolved")
    monkeypatch.setattr(dep, "resolve_region", lambda x: x or "us-west-2")
    monkeypatch.setattr(
        proxy, "serve_mcp_proxy", lambda arn, region: captured.update(arn=arn, region=region)
    )
    from strandly.serve.cli.main import main

    assert main(["serve", "mcp", "--agentcore"]) == 0
    assert captured == {"arn": "arn:resolved", "region": "us-west-2"}


def test_serve_mcp_agentcore_requires_a_runtime_arn(monkeypatch, capsys):
    import strandly.serve.deploy as dep

    monkeypatch.setattr(dep, "resolve_runtime_arn", lambda x: None)
    monkeypatch.setattr(dep, "resolve_region", lambda x: "us-west-2")
    from strandly.serve.cli.main import main

    assert main(["serve", "mcp", "--agentcore"]) == 1
    assert "no runtime ARN" in capsys.readouterr().err


def test_serve_agentcore_rejects_the_agentcore_flag(capsys):
    # `serve agentcore` IS the deployed entrypoint; the proxy flag only makes sense for mcp.
    from strandly.serve.cli.main import main

    assert main(["serve", "agentcore", "--agentcore"]) == 1
    assert "applies to `serve mcp` only" in capsys.readouterr().err


# ---- serve mcp default (dashboard-API proxy) + login/logout routing ------------------------------


def test_serve_mcp_default_requires_a_login(monkeypatch, capsys):
    import strandly_mcp.auth as auth_mod

    monkeypatch.setattr(auth_mod, "load_auth", lambda path=None: None)
    from strandly.serve.cli.main import main

    assert main(["serve", "mcp"]) == 1
    err = capsys.readouterr().err
    assert "strands-factory login" in err  # the actionable fix, not a stack trace
    assert "--agentcore" in err  # the explicit alternative


def test_serve_mcp_default_starts_the_dashboard_proxy(monkeypatch):
    import strandly_mcp.auth as auth_mod
    import strandly_mcp.server as proxy

    session = {"api_url": "https://api.example.com", "access_token": "at"}
    captured = {}
    monkeypatch.setattr(auth_mod, "load_auth", lambda path=None: session)
    monkeypatch.setattr(proxy, "serve_mcp_dashboard_proxy", lambda auth: captured.update(auth=auth))
    from strandly.serve.cli.main import main

    assert main(["serve", "mcp"]) == 0
    assert captured["auth"] is session


def test_login_flags_parsed():
    a = build_parser().parse_args(
        ["login", "--api-url", "https://api", "--cognito-domain", "https://pool", "--client-id", "c-1"]
    )
    assert a.command == "login" and a.api_url == "https://api"
    assert a.cognito_domain == "https://pool" and a.client_id == "c-1" and a.portal_url is None


def test_login_routes_to_the_auth_command(monkeypatch):
    import strandly_mcp.auth as auth_mod

    captured = {}
    monkeypatch.setattr(
        auth_mod, "login_command", lambda **kw: captured.update(kw) or 0
    )
    from strandly.serve.cli.main import main

    assert main(["login", "--portal-url", "https://portal.example.com"]) == 0
    assert captured["portal_url"] == "https://portal.example.com"
    assert captured["api_url"] is None  # discovery/env/cache resolve the rest


def test_logout_routes_to_the_auth_command(monkeypatch):
    import strandly_mcp.auth as auth_mod

    monkeypatch.setattr(auth_mod, "logout_command", lambda **kw: 0)
    from strandly.serve.cli.main import main

    assert main(["logout"]) == 0


# ---- people (the operator path into the people directory) -------------------------------
def test_people_add_parsed():
    a = build_parser().parse_args(
        ["people", "--table", "t", "add", "example-maintainer", "--name", "Example Maintainer", "--role", "maintainer",
         "--github", "example-maintainer", "--slack", "U0EXAMPLEPERSON"]
    )
    assert a.command == "people" and a.people_command == "add" and a.person_id == "example-maintainer"
    assert a.github == "example-maintainer" and a.slack == "U0EXAMPLEPERSON" and a.disabled is False


def test_people_requires_a_subcommand():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["people"])


class _FakePeopleDdb:
    def __init__(self):
        self.rows = {}

    def put_item(self, TableName, Item):
        self.rows[Item["person_id"]["S"]] = Item

    def delete_item(self, TableName, Key):
        self.rows.pop(Key["person_id"]["S"], None)

    def get_item(self, TableName, Key, ConsistentRead=False):
        row = self.rows.get(Key["person_id"]["S"])
        return {"Item": row} if row else {}

    def scan(self, **kw):
        return {"Items": list(self.rows.values())}

    def query(self, **kw):
        attr = kw["ExpressionAttributeNames"]["#k"]
        value = kw["ExpressionAttributeValues"][":v"]["S"]
        return {"Items": [r for r in self.rows.values() if r.get(attr, {}).get("S") == value]}


@pytest.fixture
def people_ddb(monkeypatch):
    import sys
    import types

    ddb = _FakePeopleDdb()
    made = []
    monkeypatch.setitem(
        sys.modules, "boto3", types.SimpleNamespace(client=lambda svc, region_name=None: made.append((svc, region_name)) or ddb)
    )
    ddb.made = made
    return ddb


def test_people_add_list_remove_round_trip(people_ddb, capsys, monkeypatch):
    import json

    from strandly.serve.cli.main import main

    monkeypatch.setenv("STRANDLY_PEOPLE_TABLE", "strandly-dev-people")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert main(["people", "add", "example-maintainer", "--name", "Example Maintainer", "--github", "example-maintainer", "--slack", "U0EXAMPLEPERSON"]) == 0
    assert people_ddb.made == [("dynamodb", "us-west-2")]
    stored = json.loads(capsys.readouterr().out)
    assert stored["github_login"] == "example-maintainer" and stored["enabled"] is True  # normalized on write
    assert "github_login" in people_ddb.rows["example-maintainer"]

    assert main(["people", "list"]) == 0
    assert [p["person_id"] for p in json.loads(capsys.readouterr().out)] == ["example-maintainer"]

    assert main(["people", "remove", "example-maintainer"]) == 0
    assert people_ddb.rows == {}


def test_people_add_rejects_a_person_without_identities(people_ddb, capsys, monkeypatch):
    from strandly.serve.cli.main import main

    assert main(["people", "--table", "t", "add", "ghost", "--name", "Nobody"]) == 1
    assert "at least one identity" in capsys.readouterr().err
    assert people_ddb.rows == {}


_SUB = "0f8fad5b-d9cb-469f-a165-70867728950e"


class _FakeCognito:
    """AdminGetUser over one user; unknown names raise the botocore-shaped UserNotFoundException."""

    def __init__(self):
        self.calls = []

    def admin_get_user(self, *, UserPoolId, Username):
        self.calls.append((UserPoolId, Username))
        if Username not in ("alice", "alice@example.com"):
            error = Exception("User does not exist.")
            error.response = {"Error": {"Code": "UserNotFoundException"}}
            raise error
        return {"Username": "alice", "UserAttributes": [{"Name": "sub", "Value": _SUB}]}


@pytest.fixture
def people_and_cognito(people_ddb, monkeypatch):
    import sys
    import types

    cognito = _FakeCognito()
    clients = {"dynamodb": people_ddb, "cognito-idp": cognito}
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda svc, region_name=None: clients[svc]))
    monkeypatch.setenv("STRANDLY_PEOPLE_TABLE", "strandly-dev-people")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.delenv("STRANDLY_COGNITO_USER_POOL_ID", raising=False)
    return people_ddb, cognito


def test_people_add_cognito_binds_the_users_sub(people_and_cognito, capsys):
    import json

    from strandly.serve.cli.main import main

    ddb, cognito = people_and_cognito
    assert main(["people", "--user-pool-id", "us-west-2_pool", "add", "alice", "--github", "alice",
                 "--cognito", "alice@example.com"]) == 0
    assert cognito.calls == [("us-west-2_pool", "alice@example.com")]
    assert json.loads(capsys.readouterr().out)["cognito_sub"] == _SUB
    assert ddb.rows["alice"]["cognito_sub"] == {"S": _SUB}
    assert main(["people", "list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["cognito_sub"] == _SUB  # list shows the binding


def test_people_add_cognito_reads_the_pool_from_the_dashboard_stack(people_and_cognito, capsys, monkeypatch):
    from strandly.serve import provisioning
    from strandly.serve.cli.main import main

    _, cognito = people_and_cognito
    asked = []
    monkeypatch.setattr(provisioning, "deployed_stack_outputs",
                        lambda stack, region: asked.append((stack, region)) or {"CognitoUserPoolId": "us-west-2_stack"})
    assert main(["people", "--dashboard-stack", "Example-Dashboard-dev", "add", "alice", "--github", "alice",
                 "--cognito", "alice"]) == 0
    assert asked == [("Example-Dashboard-dev", "us-west-2")] and cognito.calls == [("us-west-2_stack", "alice")]


def test_people_add_cognito_sub_binds_directly_and_rejects_a_duplicate(people_and_cognito, capsys):
    from strandly.serve.cli.main import main

    ddb, cognito = people_and_cognito
    assert main(["people", "add", "alice", "--github", "alice", "--cognito-sub", _SUB]) == 0
    assert cognito.calls == [] and ddb.rows["alice"]["cognito_sub"] == {"S": _SUB}
    capsys.readouterr()
    assert main(["people", "add", "bob", "--github", "bob", "--cognito-sub", _SUB]) == 1
    assert "already bound to 'alice'" in capsys.readouterr().err and "bob" not in ddb.rows


def test_people_add_cognito_errors(people_and_cognito, capsys):
    from strandly.serve.cli.main import main

    ddb, _ = people_and_cognito
    assert main(["people", "add", "alice", "--github", "alice", "--cognito", "alice"]) == 1
    assert "--user-pool-id" in capsys.readouterr().err
    assert main(["people", "--user-pool-id", "p", "add", "alice", "--github", "alice", "--cognito", "mallory"]) == 1
    assert "no Cognito user 'mallory'" in capsys.readouterr().err
    assert main(["people", "add", "alice", "--github", "alice", "--cognito-sub", "alice@example.com"]) == 1
    assert "not a Cognito user sub" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["people", "add", "alice", "--github", "alice", "--cognito", "a", "--cognito-sub", _SUB])
    assert ddb.rows == {}


def test_people_add_keeps_a_binding_unless_told_otherwise(people_and_cognito, capsys):
    from strandly.serve.cli.main import main

    ddb, cognito = people_and_cognito
    assert main(["people", "add", "alice", "--github", "alice", "--cognito-sub", _SUB]) == 0
    # A replace without a --cognito* flag (a role edit) keeps the binding and asks Cognito nothing.
    assert main(["people", "add", "alice", "--github", "alice", "--role", "maintainer"]) == 0
    assert ddb.rows["alice"]["cognito_sub"] == {"S": _SUB} and cognito.calls == []
    assert main(["people", "add", "alice", "--github", "alice", "--unbind-cognito"]) == 0
    assert "cognito_sub" not in ddb.rows["alice"]
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main(["people", "add", "alice", "--github", "alice", "--unbind-cognito", "--cognito-sub", _SUB])


def test_people_errors_without_a_table(people_ddb, capsys, monkeypatch):
    from strandly.serve.cli.main import main

    monkeypatch.delenv("STRANDLY_PEOPLE_TABLE", raising=False)
    assert main(["people", "list"]) == 1
    assert "STRANDLY_PEOPLE_TABLE" in capsys.readouterr().err


@pytest.mark.parametrize("prompt", ["", "   ", "\n\t"])
def test_invoke_refuses_an_empty_prompt(monkeypatch, capsys, prompt):
    import strandly.ops.runtime_client as rc

    def no_launch(*_a, **_k):  # pragma: no cover — reaching it is the failure
        raise AssertionError("an empty prompt must not be sent")

    monkeypatch.setattr(rc, "launch_run", no_launch)
    from strandly.serve.cli.main import main

    assert main(["invoke", prompt, "--session-id", "s-1", "--runtime-arn", "arn:x"]) == 2
    assert "prompt is empty" in capsys.readouterr().err
