"""``strandly deploy`` — the agent runtime through the CDK AgentRuntime stack (no AWS/cdk run)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import strandly.serve.deploy as dep

_ENV = {
    "STRANDLY_SECRETS_ARN": "arn:secret",
    "AGENTCORE_MEMORY_ID": "mem-1",
    "STRANDLY_SANDBOX_RUNTIME_ARN": "arn:sandbox",
    "STRANDLY_SANDBOX_CAPACITY_PROVIDER_ID": "cp-1",
    "STRANDLY_SANDBOX_TTL_SCHEDULE_GROUP": "ttl-group",
    "STRANDLY_SANDBOX_TTL_ROLE_ARN": "arn:ttl-role",
    "STRANDLY_AGENT_SUBNET_IDS": "subnet-a,subnet-b",
    "STRANDLY_AGENT_SECURITY_GROUP_ID": "sg-agent",
}
# Everything the Sandbox stack hands the agent beyond the runtime ARN: TTL ids + the VPC's subnets/SG.
_TTL_ENV = {
    k: v for k, v in _ENV.items() if "CAPACITY_PROVIDER" in k or "_TTL_" in k or "STRANDLY_AGENT_" in k
}


def _cmd(**overrides) -> list[str]:
    kwargs = dict(
        cdk=["cdk"],
        app_name="strandly",
        stage="dev",
        region="us-west-2",
        env=dict(_ENV),
        bundle_dir=Path("/b"),
        outputs_file=Path("/o.json"),
    )
    kwargs.update(overrides)
    return dep.cdk_deploy_command(**kwargs)


def _context(cmd: list[str]) -> dict[str, str]:
    pairs = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-c"]
    return dict(p.split("=", 1) for p in pairs)


def test_cdk_command_deploys_only_the_agent_runtime_stack():
    cmd = _cmd()
    assert cmd[:3] == ["cdk", "deploy", "Strandly-AgentRuntime-dev"]
    assert "--exclusively" in cmd and cmd[cmd.index("--outputs-file") + 1] == "/o.json"
    assert _context(cmd) == {
        "name": "strandly",
        "env": "dev",
        "region": "us-west-2",
        "agent_code_asset": "/b",
        "secret_arn": "arn:secret",
        "memory_id": "mem-1",
        "sandbox_runtime_arn": "arn:sandbox",
        "sandbox_capacity_provider_id": "cp-1",
        "sandbox_ttl_schedule_group": "ttl-group",
        "sandbox_ttl_role_arn": "arn:ttl-role",
        "agent_subnet_ids": "subnet-a,subnet-b",
        "agent_security_group_id": "sg-agent",
    }


def test_cdk_command_passes_no_retired_flag():
    ctx = _context(_cmd())
    assert not {"agent_runtime_cdk", "agent_platform_version"} & set(ctx)


def test_cdk_command_requires_secret_memory_and_sandbox():
    env = dict(_ENV)
    del env["STRANDLY_SANDBOX_RUNTIME_ARN"]
    with pytest.raises(ValueError, match="STRANDLY_SANDBOX_RUNTIME_ARN"):
        _cmd(env=env)


def _resolve(env: dict[str, str], environ: dict[str, str], outputs: dict[tuple[str, str], str]):
    calls: list[tuple[str, str, str]] = []

    def lookup(stack: str, key: str, region: str) -> str | None:
        calls.append((stack, key, region))
        return outputs.get((stack, key))

    resolved = dep.resolve_backend_env(
        app_name="strandly", stage="dev", region="us-west-2", env=env, environ=environ, lookup=lookup
    )
    return resolved, calls


def test_backend_env_prefers_flags_then_process_env_and_never_calls_aws_when_complete():
    resolved, calls = _resolve(
        {"STRANDLY_SECRETS_ARN": "flag"},
        {"STRANDLY_SECRETS_ARN": "env", "AGENTCORE_MEMORY_ID": "m", "STRANDLY_SANDBOX_RUNTIME_ARN": "s", **_TTL_ENV},
        {},
    )
    assert resolved == {
        "STRANDLY_SECRETS_ARN": "flag",
        "AGENTCORE_MEMORY_ID": "m",
        "STRANDLY_SANDBOX_RUNTIME_ARN": "s",
        **_TTL_ENV,
    }
    assert calls == []


def test_backend_env_falls_back_to_the_deployed_stack_outputs():
    resolved, calls = _resolve(
        {},
        {"STRANDLY_SECRETS_ARN": "env"},
        {
            ("Strandly-Backend-dev", "MemoryId"): "mem-x",
            ("Strandly-Sandbox-dev", "SandboxRuntimeArn"): "arn:sb",
            ("Strandly-Sandbox-dev", "CapacityProviderId"): "cp-x",
            ("Strandly-Sandbox-dev", "SandboxTtlScheduleGroup"): "grp-x",
            ("Strandly-Sandbox-dev", "SandboxTtlSchedulerRoleArn"): "arn:ttl-x",
            ("Strandly-Sandbox-dev", "AgentSubnetIds"): "subnet-x,subnet-y",
            ("Strandly-Sandbox-dev", "AgentSecurityGroupId"): "sg-x",
        },
    )
    assert resolved == {
        "STRANDLY_SECRETS_ARN": "env",
        "AGENTCORE_MEMORY_ID": "mem-x",
        "STRANDLY_SANDBOX_RUNTIME_ARN": "arn:sb",
        "STRANDLY_SANDBOX_CAPACITY_PROVIDER_ID": "cp-x",
        "STRANDLY_SANDBOX_TTL_SCHEDULE_GROUP": "grp-x",
        "STRANDLY_SANDBOX_TTL_ROLE_ARN": "arn:ttl-x",
        "STRANDLY_AGENT_SUBNET_IDS": "subnet-x,subnet-y",
        "STRANDLY_AGENT_SECURITY_GROUP_ID": "sg-x",
    }
    assert [c[:2] for c in calls] == [
        ("Strandly-Backend-dev", "MemoryId"),
        ("Strandly-Sandbox-dev", "SandboxRuntimeArn"),
        ("Strandly-Sandbox-dev", "CapacityProviderId"),
        ("Strandly-Sandbox-dev", "SandboxTtlScheduleGroup"),
        ("Strandly-Sandbox-dev", "SandboxTtlSchedulerRoleArn"),
        ("Strandly-Sandbox-dev", "AgentSubnetIds"),
        ("Strandly-Sandbox-dev", "AgentSecurityGroupId"),
    ]


def test_backend_env_refuses_unknown_and_toolkit_era_keys():
    for key in ("FOO", "STRANDLY_KB_ID", "AGENT_OBSERVABILITY_ENABLED", "AWS_REGION"):
        with pytest.raises(ValueError, match=key):
            _resolve({key: "x"}, {}, {})


def test_report_cdk_outputs_prints_workflow_lines_and_records(tmp_path, capsys, monkeypatch):
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(dep, "record_runtime", lambda arn, region: recorded.append((arn, region)))
    arn = "arn:aws:bedrock-agentcore:us-west-2:ACCOUNT:runtime/strandly_dev_agent-abc"
    out = tmp_path / "o.json"
    out.write_text(
        json.dumps(
            {
                "Strandly-AgentRuntime-dev": {
                    "AgentRuntimeArn": arn,
                    "AgentRuntimeId": "strandly_dev_agent-abc",
                    "AgentExecRoleName": "strandly-dev-agent-runtime-exec",
                }
            }
        )
    )
    assert dep._report_cdk_outputs(out, "Strandly-AgentRuntime-dev", "us-west-2") == 0
    stdout = capsys.readouterr().out.splitlines()
    assert stdout == [
        f"runtime_arn={arn}",
        "runtime_id=strandly_dev_agent-abc",
        "exec_role=strandly-dev-agent-runtime-exec",
    ]
    assert recorded == [(arn, "us-west-2")]


def test_report_cdk_outputs_fails_without_outputs(tmp_path):
    assert dep._report_cdk_outputs(tmp_path / "missing.json", "S", "us-west-2") == 1


def test_cli_deploy_dispatches_to_cdk(monkeypatch):
    from strandly.serve.cli.main import main

    captured: dict = {}
    monkeypatch.setattr(dep, "deploy_cdk", lambda **kw: captured.update(kw) or 0)
    rc = main(
        ["deploy", "--stage", "prod", "--region", "us-west-2", "--env", "STRANDLY_SECRETS_ARN=s"]
    )
    assert rc == 0
    assert captured == {
        "app_name": "strandly",
        "stage": "prod",
        "region": "us-west-2",
        "env": {"STRANDLY_SECRETS_ARN": "s"},
    }


def test_cli_deploy_requires_stage(monkeypatch):
    from strandly.serve.cli.main import main

    monkeypatch.setattr(dep, "deploy_cdk", lambda **kw: pytest.fail("should not run"))
    with pytest.raises(SystemExit) as exc:
        main(["deploy", "--region", "us-west-2"])
    assert exc.value.code == 2


@pytest.mark.parametrize("flag", ["--cdk", "--platform-version=V2", "--no-observability"])
def test_cli_deploy_rejects_the_retired_flags(monkeypatch, flag):
    from strandly.serve.cli.main import main

    monkeypatch.setattr(dep, "deploy_cdk", lambda **kw: pytest.fail("should not run"))
    with pytest.raises(SystemExit):
        main(["deploy", "--stage", "dev", "--region", "us-west-2", flag])


# ---- deploy_cdk: the whole command, with cdk / the bundle build / AWS faked -------------------

#: Every key the AgentRuntime stack takes (built from the map, so a new key is covered for free).
_FULL_ENV = {key: f"value-{key.lower()}" for key in dep.CDK_ENV_CONTEXT}


class _Run:
    """A fake ``subprocess.run``: the bundle build and ``cdk deploy``, by argv."""

    def __init__(self, tmp_path: Path, *, build_rc: int = 0, cdk_rc: int = 0, outputs=None):
        self.tmp_path = tmp_path
        self.build_rc = build_rc
        self.cdk_rc = cdk_rc
        self.outputs = outputs
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        is_cdk = "deploy" in cmd and "--outputs-file" in cmd
        rc = self.cdk_rc if is_cdk else self.build_rc
        if is_cdk and rc == 0 and self.outputs is not None:
            out = Path(cmd[cmd.index("--outputs-file") + 1])
            out.write_text(json.dumps(self.outputs))
        return type("Done", (), {"returncode": rc})()


@pytest.fixture
def deploy_env(monkeypatch, tmp_path):
    """infra/ at tmp_path, a `cdk` on the path, no process env, and no AWS lookups."""
    import strandly.serve.provisioning as prov

    monkeypatch.setattr(prov, "_find_infra_dir", lambda: tmp_path)
    monkeypatch.setattr(prov, "_cdk_command", lambda: ["cdk"])
    for key in dep.CDK_ENV_CONTEXT:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        prov, "deployed_stack_outputs", lambda *a, **k: pytest.fail("no AWS call expected")
    )
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(dep, "record_runtime", lambda arn, region: recorded.append((arn, region)))
    return tmp_path, recorded


def _deploy(**kw) -> int:
    kw.setdefault("env", dict(_FULL_ENV))
    return dep.deploy_cdk(stage="dev", region="us-west-2", **kw)


_ARN = "arn:aws:bedrock-agentcore:us-west-2:1:runtime/strandly_dev_agent-abc"


def test_deploy_cdk_success_builds_deploys_and_reports(deploy_env, monkeypatch, capsys):
    infra, recorded = deploy_env
    run = _Run(
        infra,
        outputs={
            "Strandly-AgentRuntime-dev": {
                "AgentRuntimeArn": _ARN,
                "AgentExecRoleName": "strandly-dev-agent-exec",
                "AgentRuntimeId": "strandly_dev_agent-abc",
            }
        },
    )
    monkeypatch.setattr(dep.subprocess, "run", run)
    assert _deploy() == 0
    build, cdk = run.calls
    assert "build_agent_bundle.py" in build[1] and "--from-lock" in build
    assert cdk[:3] == ["cdk", "deploy", "Strandly-AgentRuntime-dev"]
    assert recorded == [(_ARN, "us-west-2")]
    out = capsys.readouterr().out.splitlines()
    assert out == [
        f"runtime_arn={_ARN}",
        "runtime_id=strandly_dev_agent-abc",
        "exec_role=strandly-dev-agent-exec",
    ]


def test_deploy_cdk_bundle_build_failure_stops_before_cdk(deploy_env, monkeypatch, capsys):
    infra, recorded = deploy_env
    run = _Run(infra, build_rc=3)
    monkeypatch.setattr(dep.subprocess, "run", run)
    assert _deploy() == 3
    assert len(run.calls) == 1, "cdk deploy never ran"
    assert "bundle build failed" in capsys.readouterr().err
    assert recorded == []


def test_deploy_cdk_failure_returns_its_code_and_reports_nothing(deploy_env, monkeypatch, capsys):
    infra, recorded = deploy_env
    # A previous deploy's outputs file must not be reported as this deploy's.
    stale = infra / "cdk.agent-runtime-outputs.json"
    stale.write_text(json.dumps({"Strandly-AgentRuntime-dev": {"AgentRuntimeArn": "arn:OLD"}}))
    run = _Run(infra, cdk_rc=1)
    monkeypatch.setattr(dep.subprocess, "run", run)
    assert _deploy() == 1
    captured = capsys.readouterr()
    assert "cdk deploy failed" in captured.err and "runtime_arn=" not in captured.out
    assert recorded == []
    assert not stale.exists(), "the stale outputs file was removed before cdk ran"


def test_deploy_cdk_unlinks_a_stale_outputs_file_cdk_did_not_rewrite(deploy_env, monkeypatch):
    infra, recorded = deploy_env
    stale = infra / "cdk.agent-runtime-outputs.json"
    stale.write_text(
        json.dumps(
            {"Strandly-AgentRuntime-dev": {"AgentRuntimeArn": "arn:OLD", "AgentExecRoleName": "r"}}
        )
    )
    monkeypatch.setattr(dep.subprocess, "run", _Run(infra, outputs=None))  # rc 0, writes nothing
    assert _deploy() == 1, "no outputs from THIS deploy"
    assert recorded == []


def test_deploy_cdk_missing_env_returns_2_before_building(deploy_env, monkeypatch, capsys):
    import strandly.serve.provisioning as prov

    infra, _ = deploy_env
    monkeypatch.setattr(prov, "deployed_stack_outputs", lambda *a, **k: None)  # stacks absent
    run = _Run(infra)
    monkeypatch.setattr(dep.subprocess, "run", run)
    env = dict(_FULL_ENV)
    del env["STRANDLY_SANDBOX_RUNTIME_ARN"]
    assert _deploy(env=env) == 2
    assert run.calls == []
    assert "STRANDLY_SANDBOX_RUNTIME_ARN" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["no-credentials", "access-denied"])
def test_deploy_cdk_aws_errors_reading_outputs_are_a_clear_message(
    deploy_env, monkeypatch, capsys, kind
):
    from botocore.exceptions import ClientError, NoCredentialsError

    import strandly.serve.provisioning as prov

    infra, _ = deploy_env
    error = (
        NoCredentialsError()
        if kind == "no-credentials"
        else ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "DescribeStacks")
    )

    def boom(*_a, **_k):
        raise error

    monkeypatch.setattr(prov, "deployed_stack_outputs", boom)
    run = _Run(infra)
    monkeypatch.setattr(dep.subprocess, "run", run)
    assert _deploy(env={}) == 1
    err = capsys.readouterr().err
    assert "could not read the deployed" in err and "--env KEY=VALUE" in err
    assert type(error).__name__ in err
    assert run.calls == []


def test_backend_lookup_describes_each_stack_once(monkeypatch):
    import strandly.serve.provisioning as prov

    described: list[str] = []

    def outputs(stack, region, **_k):
        described.append(stack)
        return {
            "Strandly-Backend-dev": {"SecretArn": "arn:s", "MemoryId": "m"},
            "Strandly-Sandbox-dev": {
                "SandboxRuntimeArn": "arn:sb",
                "CapacityProviderId": "cp",
                "SandboxTtlScheduleGroup": "g",
                "SandboxTtlSchedulerRoleArn": "arn:r",
            },
        }.get(stack)

    monkeypatch.setattr(prov, "deployed_stack_outputs", outputs)
    resolved = dep.resolve_backend_env(
        app_name="strandly", stage="dev", region="us-west-2", env={}, environ={}
    )
    assert resolved["STRANDLY_SANDBOX_TTL_ROLE_ARN"] == "arn:r"
    assert sorted(described) == ["Strandly-Backend-dev", "Strandly-Sandbox-dev"]
