from __future__ import annotations

import pytest

from strandly_harness.serve.cli.main import build_parser


def test_run_parsed():
    a = build_parser().parse_args(["run", "do a thing"])
    assert a.command == "run" and a.prompt == "do a thing" and a.hitl is False


def test_run_hitl_flag():
    a = build_parser().parse_args(["run", "x", "--hitl"])
    assert a.hitl is True


def test_poll_parsed():
    a = build_parser().parse_args(["poll", "12345", "--session-id", "sess-abc"])
    assert a.command == "poll" and a.task_id == "12345" and a.session_id == "sess-abc"


def test_poll_invokes_runtime_client(monkeypatch):
    import strandly_harness.ops.runtime_client as rc

    captured = {}
    monkeypatch.setattr(
        rc, "poll_run", lambda arn, region, sid, tid: captured.update(arn=arn, sid=sid, tid=tid) or {"status": "completed", "result": "done"}
    )
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    from strandly_harness.serve.cli.main import main

    rc_code = main(["poll", "t-1", "--session-id", "s-1", "--runtime-arn", "arn:rt"])
    assert rc_code == 0
    assert captured == {"arn": "arn:rt", "sid": "s-1", "tid": "t-1"}


def test_deploy_parsed():
    a = build_parser().parse_args(
        ["deploy", "--region", "us-west-2", "--env", "A=1", "--env", "B=2"]
    )
    assert a.command == "deploy" and a.region == "us-west-2" and a.env == ["A=1", "B=2"]
    assert a.no_observability is False  # observability on by default


def test_deploy_no_observability_flag():
    a = build_parser().parse_args(["deploy", "--region", "us-west-2", "--no-observability"])
    assert a.no_observability is True


def test_deploy_passes_observability_to_run_deploy(monkeypatch):
    import strandly_harness.serve.deploy as dep

    captured = {}
    monkeypatch.setattr(dep, "resolve_region", lambda x: x or "us-west-2")
    monkeypatch.setattr(dep, "deploy", lambda **kw: captured.update(kw) or 0)
    from strandly_harness.serve.cli.main import main

    assert main(["deploy", "--region", "us-west-2"]) == 0
    assert captured["observability"] is True  # default
    assert main(["deploy", "--region", "us-west-2", "--no-observability"]) == 0
    assert captured["observability"] is False


def test_invoke_parsed():
    a = build_parser().parse_args(["invoke", "do it", "--session-id", "s-1"])
    assert a.command == "invoke" and a.prompt == "do it" and a.session_id == "s-1"


def test_invoke_resolves_arn_and_calls_runtime(monkeypatch):
    # invoke now routes through launch_run (fire-and-forget, no GitHub context); the session id is
    # forwarded for both instance affinity and the Memory result channel.
    import strandly_harness.ops.runtime_client as rc
    import strandly_harness.serve.deploy as dep

    captured = {}
    monkeypatch.setattr(dep, "resolve_runtime_arn", lambda x: x or "arn:resolved")
    monkeypatch.setattr(dep, "resolve_region", lambda x: x or "us-west-2")
    monkeypatch.setattr(
        rc,
        "launch_run",
        lambda arn, region, sid, prompt: captured.update(arn=arn, sid=sid, prompt=prompt)
        or {"status": "accepted", "taskId": "t9"},
    )
    from strandly_harness.serve.cli.main import main

    assert main(["invoke", "hello", "--session-id", "s-1"]) == 0
    assert captured["arn"] == "arn:resolved" and captured["sid"] == "s-1"
    assert captured["prompt"] == "hello"


def test_chat_agentcore_parsed():
    a = build_parser().parse_args(["chat", "--agentcore", "--session-id", "sx"])
    assert a.command == "chat" and a.agentcore is True and a.session_id == "sx"


def test_chat_agentcore_needs_runtime(monkeypatch):
    # --agentcore requires a resolvable runtime ARN + region + AGENTCORE_MEMORY_ID; missing → exit 1.
    import strandly_harness.serve.deploy as dep

    monkeypatch.setattr(dep, "resolve_runtime_arn", lambda x: None)
    monkeypatch.setattr(dep, "resolve_region", lambda x: "us-west-2")
    from strandly_harness.serve.cli.main import main

    assert main(["chat", "--agentcore"]) == 1


def test_chat_agentcore_streams_when_resolvable(monkeypatch):
    import strandly_harness.serve.cli.repl as repl
    import strandly_harness.serve.deploy as dep
    from strandly_harness.core.config import Config

    monkeypatch.setattr(dep, "resolve_runtime_arn", lambda x: "arn:rt")
    monkeypatch.setattr(dep, "resolve_region", lambda x: "us-west-2")
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: Config(values={"AGENTCORE_MEMORY_ID": "mem-1"})))
    captured = {}
    monkeypatch.setattr(
        repl, "chat_agentcore", lambda arn, region, sid: captured.update(arn=arn, region=region, sid=sid)
    )
    from strandly_harness.serve.cli.main import main

    assert main(["chat", "--agentcore", "--session-id", "sx"]) == 0
    assert captured == {"arn": "arn:rt", "region": "us-west-2", "sid": "sx"}


def test_invoke_errors_without_arn(monkeypatch):
    import strandly_harness.serve.deploy as dep

    monkeypatch.setattr(dep, "resolve_runtime_arn", lambda x: None)
    monkeypatch.setattr(dep, "resolve_region", lambda x: "us-west-2")
    from strandly_harness.serve.cli.main import main

    assert main(["invoke", "hi", "--session-id", "s-1"]) == 1  # no ARN anywhere → error


def test_serve_mode():
    a = build_parser().parse_args(["serve", "mcp"])
    assert a.command == "serve" and a.mode == "mcp"


def test_serve_agentcore():
    a = build_parser().parse_args(["serve", "agentcore"])
    assert a.mode == "agentcore"


def test_provision_parsed():
    a = build_parser().parse_args(["provision", "--name", "strandly"])
    # KB is provisioned by default (no_kb defaults False).
    assert a.command == "provision" and a.name == "strandly" and a.no_kb is False


def test_provision_no_kb_flag():
    a = build_parser().parse_args(["provision", "--no-kb"])
    assert a.no_kb is True


def test_provision_env_defaults_to_dev():
    a = build_parser().parse_args(["provision"])
    assert a.env == "dev"


def test_provision_env_flag(monkeypatch):
    captured = {}
    import strandly_harness.serve.provisioning as prov

    monkeypatch.setattr(prov, "provision", lambda **kw: captured.update(kw))
    from strandly_harness.serve.cli.main import main

    assert main(["provision", "--env", "prod"]) == 0
    assert captured["env"] == "prod"
    assert captured["with_kb"] is True  # default: provision the KB


def test_provision_github_token_folded_into_secret(monkeypatch):
    captured = {}

    import strandly_harness.serve.provisioning as prov

    monkeypatch.setattr(prov, "provision", lambda **kw: captured.update(kw))
    from strandly_harness.serve.cli.main import main

    assert main(["provision", "--github-token", "ghp_secret"]) == 0
    assert captured["extra_secrets"] == {"STRANDLY_GITHUB_TOKEN": "ghp_secret"}


def test_provision_without_github_token_passes_none(monkeypatch):
    captured = {}

    import strandly_harness.serve.provisioning as prov

    monkeypatch.setattr(prov, "provision", lambda **kw: captured.update(kw))
    from strandly_harness.serve.cli.main import main

    assert main(["provision"]) == 0
    assert captured["extra_secrets"] is None  # no token flag → nothing folded in


def test_serve_rejects_bad_mode():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["serve", "telepathy"])


def test_requires_command():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_main_run_invokes_oneshot(monkeypatch):
    import strandly_harness.serve.cli.repl as repl

    captured = {}
    monkeypatch.setattr(
        repl,
        "run_oneshot",
        lambda settings, prompt, session_id, hitl: captured.update(
            prompt=prompt, session_id=session_id, hitl=hitl
        ),
    )
    from strandly_harness.serve.cli.main import main

    assert main(["run", "hello"]) == 0
    # autonomous by default; run gets its own default session
    assert captured == {"prompt": "hello", "session_id": "strandly-run", "hitl": False}


def test_brief_flags_parsed():
    a = build_parser().parse_args(
        ["brief", "--since", "3d", "--out", "/tmp/today.md", "--session-id", "s-x"]
    )
    assert a.since == "3d" and a.out == "/tmp/today.md" and a.session_id == "s-x"


def test_brief_invoke(monkeypatch):
    import strandly_harness.serve.cli.repl as repl
    from strandly_harness.core.config import Config

    monkeypatch.setattr(Config, "load", classmethod(lambda cls: Config(values={})))
    captured = {}
    monkeypatch.setattr(
        repl,
        "run_oneshot",
        lambda settings, prompt, session_id: captured.update(prompt=prompt, session_id=session_id),
    )
    from strandly_harness.serve.cli.main import main

    assert main(["brief"]) == 0
    assert 'skill(action="activate", name="brief")' in captured["prompt"]
    assert "last 24h" in captured["prompt"] and "./briefs" in captured["prompt"]
    assert captured["session_id"].startswith("strandly-brief-")
    assert captured["session_id"][len("strandly-brief-"):].isdigit()
