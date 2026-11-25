"""Tests for provision() — the CDK-wrapper behavior, without invoking cdk or AWS.

We stub the infra-dir + cdk-command resolution and subprocess.run, so these exercise the pure
wiring: which stacks/context get built, how the sandbox runtime ARN is handed onward, and how a
token reaches the config secret without passing through CDK context.
"""

from __future__ import annotations

import json

import pytest

import strandly.serve.provisioning as prov


def _stub_cdk(monkeypatch, tmp_path, capture: dict, outputs: dict | None = None):
    """Make provision() runnable offline: fake infra dir, fake cdk, capture the subprocess cmd.

    ``outputs`` is written to the ``--outputs-file`` path the way a real ``cdk deploy`` would, so the
    stack-output parsing (the sandbox ARN hand-off) is exercised rather than stubbed out.
    """
    monkeypatch.setattr(prov, "_find_infra_dir", lambda: tmp_path)
    monkeypatch.setattr(prov, "_cdk_command", lambda: ["cdk"])

    class _Result:
        returncode = 0

    def fake_run(cmd, *a, **k):
        capture["cmd"] = cmd
        if outputs is not None:
            (tmp_path / "cdk.provision-outputs.json").write_text(json.dumps(outputs))
        return _Result()

    monkeypatch.setattr(prov.subprocess, "run", fake_run)


_SANDBOX_ARN = "arn:aws:bedrock-agentcore:us-west-2:1234:runtime/strandly_dev_sandbox_ec2-abc"
_TTL_OUTPUTS = {
    "CapacityProviderId": "strandly_dev_sandbox_ec2-cp1",
    "SandboxTtlScheduleGroup": "strandly-dev-sandbox-ttl",
    "SandboxTtlSchedulerRoleArn": "arn:aws:iam::1234:role/strandly-dev-sandbox-ttl-scheduler",
}
_TTL_EXPORTS = {
    "STRANDLY_SANDBOX_CAPACITY_PROVIDER_ID": "strandly_dev_sandbox_ec2-cp1",
    "STRANDLY_SANDBOX_TTL_SCHEDULE_GROUP": "strandly-dev-sandbox-ttl",
    "STRANDLY_SANDBOX_TTL_ROLE_ARN": "arn:aws:iam::1234:role/strandly-dev-sandbox-ttl-scheduler",
}
_NET_OUTPUTS = {"AgentSubnetIds": "subnet-a,subnet-b", "AgentSecurityGroupId": "sg-agent"}
_NET_EXPORTS = {
    "STRANDLY_AGENT_SUBNET_IDS": "subnet-a,subnet-b",
    "STRANDLY_AGENT_SECURITY_GROUP_ID": "sg-agent",
}


def test_the_sandbox_stack_is_deployed_alongside_the_backends(monkeypatch, tmp_path):
    """The sandbox is not optional: a deployed agent without one refuses to run.

    Provisioning "the backends" includes it, so a deployment cannot end up agent-without-sandbox.
    """
    capture: dict = {}
    _stub_cdk(monkeypatch, tmp_path, capture)
    prov.provision(env="dev", region="us-west-2")
    assert "Strandly-Sandbox-dev" in capture["cmd"]


def test_provision_deploys_exclusively_so_a_conditional_stack_cannot_drop_an_export(
    monkeypatch, tmp_path
):
    """`--exclusively` is load-bearing, not tidiness — it broke the first Actions deploy without it.

    CDK treats a cross-stack export as unused when the consuming stack is absent from the synthesized
    app, and ``MonitoringStack`` is only synthesized once the poller Lambda asset has been built
    (``infra/app.py``). On a runner that hasn't built it, deploying Data would therefore try to DELETE
    the run-ledger export that the *live* ``Strandly-Monitoring-<env>`` still imports, and
    CloudFormation aborts the update:

        Delete canceled. Cannot delete export …RunLedger…Arn… as it is in use by
        Strandly-Monitoring-prod

    which is a rollback, not a failure to start — the deploy gets half way and reverses. It passed
    locally only because the asset happened to exist there, so this is asserted rather than left to
    whoever next runs it on a clean machine.
    """
    capture: dict = {}
    _stub_cdk(monkeypatch, tmp_path, capture)
    prov.provision(env="dev", region="us-west-2")
    assert "--exclusively" in capture["cmd"]


def test_the_sandbox_runtime_arn_is_returned_and_exported(monkeypatch, tmp_path, capsys):
    """The ARN must come back as an eval-able export — this is what replaced the repo variable.

    a deploy pipeline does `eval "$(strandly provision …)"` and then passes the ARN to `strandly deploy`,
    so if this export stops being printed the agent silently deploys with no sandbox.
    """
    capture: dict = {}
    _stub_cdk(
        monkeypatch,
        tmp_path,
        capture,
        outputs={
            "Strandly-Backend-dev": {"SecretArn": "arn:aws:secretsmanager:::secret:x"},
            "Strandly-Sandbox-dev": {"SandboxRuntimeArn": _SANDBOX_ARN},
        },
    )
    result = prov.provision(env="dev", region="us-west-2")
    assert result.sandbox_runtime_arn == _SANDBOX_ARN
    out = capsys.readouterr().out
    assert f"export STRANDLY_SANDBOX_RUNTIME_ARN={_SANDBOX_ARN}" in out


def test_a_missing_sandbox_output_warns_loudly_and_returns_none(monkeypatch, tmp_path, capsys):
    """Silence here would ship an agent that fails every invocation.

    The caller (a deploy pipeline) turns this into a hard failure; provision's job is to make the absence
    unmistakable rather than print a normal-looking success.
    """
    capture: dict = {}
    _stub_cdk(
        monkeypatch,
        tmp_path,
        capture,
        outputs={"Strandly-Backend-dev": {"SecretArn": "arn:aws:secretsmanager:::secret:x"}},
    )
    result = prov.provision(env="dev", region="us-west-2")
    assert result.sandbox_runtime_arn is None
    captured = capsys.readouterr()
    assert "STRANDLY_SANDBOX_RUNTIME_ARN" not in captured.out, (
        "an empty export would eval to nothing useful and hide the problem"
    )
    assert "WARNING" in captured.err and "REFUSES to run" in captured.err


_SECRET_ARN = "arn:aws:secretsmanager:us-west-2:111111111111:secret:strandly/dev/config-AbCdEf"


def test_github_token_goes_to_the_secret_never_into_cdk_context(monkeypatch, tmp_path):
    capture: dict = {}
    stored: list = []
    _stub_cdk(
        monkeypatch, tmp_path, capture,
        outputs={"Strandly-Backend-dev": {"SecretArn": _SECRET_ARN}},
    )
    monkeypatch.setattr(prov, "store_secret_values", lambda *a, **k: stored.append(a))
    prov.provision(
        env="dev", region="us-west-2", extra_secrets={"STRANDLY_GITHUB_TOKEN": "ghp_x"}
    )
    # A context value is serialized into the template and cdk.out, and is visible in `ps`.
    assert not any("ghp_x" in tok or tok.startswith("github_token=") for tok in capture["cmd"])
    assert stored == [(_SECRET_ARN, "us-west-2", {"STRANDLY_GITHUB_TOKEN": "ghp_x"})]


def test_extra_secret_without_a_secret_output_fails_loudly(monkeypatch, tmp_path, capsys):
    capture: dict = {}
    _stub_cdk(monkeypatch, tmp_path, capture, outputs={})
    monkeypatch.setattr(prov, "store_secret_values", lambda *a, **k: pytest.fail("no ARN"))
    with pytest.raises(SystemExit):
        prov.provision(
            env="dev", region="us-west-2", extra_secrets={"STRANDLY_GITHUB_TOKEN": "ghp_x"}
        )
    assert "NOT stored" in capsys.readouterr().err
    assert not any("ghp_x" in tok for tok in capture["cmd"])


def test_store_secret_values_merges_and_keeps_other_keys(capsys):
    class _Client:
        written: dict = {}

        def get_secret_value(self, SecretId):
            assert SecretId == _SECRET_ARN
            return {"SecretString": json.dumps({"AGENTCORE_MEMORY_ID": "m", "STRANDLY_GITHUB_TOKEN": "old"})}

        def put_secret_value(self, SecretId, SecretString):
            self.written = {"id": SecretId, "value": json.loads(SecretString)}

    client = _Client()
    prov.store_secret_values(
        _SECRET_ARN, "us-west-2", {"STRANDLY_GITHUB_TOKEN": "ghp_new"}, client=client
    )
    assert client.written == {
        "id": _SECRET_ARN,
        "value": {"AGENTCORE_MEMORY_ID": "m", "STRANDLY_GITHUB_TOKEN": "ghp_new"},
    }
    # The value never reaches a log.
    err = capsys.readouterr().err
    assert "stored" in err and "ghp_new" not in err


def test_the_sandbox_ttl_outputs_are_returned_and_exported(monkeypatch, tmp_path, capsys):
    """The agent's harness schedules each sandbox session's deletion with these three; without
    them every session's EBS volume is kept forever, so provision hands them on like the ARN."""
    capture: dict = {}
    _stub_cdk(
        monkeypatch,
        tmp_path,
        capture,
        outputs={
            "Strandly-Backend-dev": {"SecretArn": "arn:aws:secretsmanager:::secret:x"},
            "Strandly-Sandbox-dev": {"SandboxRuntimeArn": _SANDBOX_ARN, **_TTL_OUTPUTS},
        },
    )
    result = prov.provision(env="dev", region="us-west-2")
    assert result.sandbox_ttl == _TTL_EXPORTS
    captured = capsys.readouterr()
    for key, value in _TTL_EXPORTS.items():
        assert f"export {key}={value}" in captured.out
    assert "never expire" not in captured.err


def test_missing_sandbox_ttl_outputs_warn(monkeypatch, tmp_path, capsys):
    capture: dict = {}
    _stub_cdk(
        monkeypatch,
        tmp_path,
        capture,
        outputs={"Strandly-Sandbox-dev": {"SandboxRuntimeArn": _SANDBOX_ARN}},
    )
    result = prov.provision(env="dev", region="us-west-2")
    assert result.sandbox_ttl == dict.fromkeys(_TTL_EXPORTS)
    captured = capsys.readouterr()
    assert "STRANDLY_SANDBOX_TTL" not in captured.out
    assert "WARNING" in captured.err and "never expire" in captured.err


def test_skip_sandbox_leaves_the_sandbox_stack_out_and_reads_its_outputs(
    monkeypatch, tmp_path, capsys
):
    """``--skip-sandbox`` leaves the deployed Sandbox stack alone, but the agent still needs its
    ARN and its session-TTL wiring, so both are read from the deployed stack."""
    capture: dict = {}
    _stub_cdk(
        monkeypatch,
        tmp_path,
        capture,
        outputs={"Strandly-Backend-dev": {"SecretArn": "arn:aws:secretsmanager:::secret:x"}},
    )
    looked_up: list[tuple] = []
    deployed = {"SandboxRuntimeArn": _SANDBOX_ARN, **_TTL_OUTPUTS, **_NET_OUTPUTS, "Unrelated": "x"}

    def fake_outputs(stack, region, **_kwargs):
        looked_up.append((stack, region))
        return deployed

    monkeypatch.setattr(prov, "deployed_stack_outputs", fake_outputs)
    result = prov.provision(env="dev", region="us-west-2", skip_sandbox=True)
    assert "Strandly-Sandbox-dev" not in capture["cmd"]
    assert {"Strandly-Data-dev", "Strandly-Backend-dev"} <= set(capture["cmd"])
    assert looked_up == [("Strandly-Sandbox-dev", "us-west-2")], "one DescribeStacks, not one per key"
    assert result.sandbox_runtime_arn == _SANDBOX_ARN
    assert result.sandbox_ttl == _TTL_EXPORTS
    assert result.agent_network == _NET_EXPORTS
    assert f"export STRANDLY_SANDBOX_RUNTIME_ARN={_SANDBOX_ARN}" in capsys.readouterr().out


def test_cli_provision_skip_sandbox_flag(monkeypatch):
    from strandly.serve.cli.main import main

    captured: dict = {}
    monkeypatch.setattr(prov, "provision", lambda **kw: captured.update(kw))
    assert main(["provision", "--env", "prod", "--skip-sandbox"]) == 0
    assert captured["skip_sandbox"] is True


# Review R3 S6: the skip path used to swallow EVERY describe_stacks error and return None, so an
# AccessDenied or throttle surfaced later as "no SandboxRuntimeArn output". Real botocore client,
# in-memory responses (Stubber validates them against the CloudFormation service model).
def _cfn_stub():
    import boto3
    from botocore.stub import Stubber

    client = boto3.Session(region_name="us-west-2").client(
        "cloudformation", aws_access_key_id="x", aws_secret_access_key="x"
    )
    return client, Stubber(client)


def test_deployed_stack_outputs_reads_the_output():
    client, stub = _cfn_stub()
    stub.add_response(
        "describe_stacks",
        {
            "Stacks": [
                {
                    "StackName": "S",
                    "CreationTime": "2026-01-01T00:00:00Z",
                    "StackStatus": "UPDATE_COMPLETE",
                    "Outputs": [{"OutputKey": "SandboxRuntimeArn", "OutputValue": _SANDBOX_ARN}],
                }
            ]
        },
        {"StackName": "S"},
    )
    with stub:
        assert prov.deployed_stack_outputs("S", None, client=client)["SandboxRuntimeArn"] == _SANDBOX_ARN


def test_deployed_stack_outputs_is_none_only_for_a_missing_stack():
    client, stub = _cfn_stub()
    stub.add_client_error(
        "describe_stacks", "ValidationError", "Stack with id S does not exist", 400
    )
    with stub:
        assert prov.deployed_stack_outputs("S", None, client=client) is None


def test_deployed_stack_outputs_propagates_every_other_error():
    from botocore.exceptions import ClientError

    client, stub = _cfn_stub()
    stub.add_client_error("describe_stacks", "AccessDenied", "not authorized", 403)
    with stub, pytest.raises(ClientError):
        prov.deployed_stack_outputs("S", None, client=client)


def test_export_lines_are_shell_quoted(monkeypatch, tmp_path, capsys):
    """Callers ``eval "$(strandly provision …)"``: a value with shell metacharacters must not run."""
    import shlex

    capture: dict = {}
    nasty = "arn:aws:secretsmanager:::secret:x;touch pwned $(id)"
    _stub_cdk(
        monkeypatch,
        tmp_path,
        capture,
        outputs={
            "Strandly-Backend-dev": {"SecretArn": nasty},
            "Strandly-Sandbox-dev": {"SandboxRuntimeArn": _SANDBOX_ARN, **_TTL_OUTPUTS},
        },
    )
    prov.provision(env="dev", region="us-west-2")
    out = capsys.readouterr().out
    assert f"export STRANDLY_SECRETS_ARN={shlex.quote(nasty)}" in out
    for line in out.splitlines():
        if line.startswith("export "):
            key, _, value = line.removeprefix("export ").partition("=")
            assert len(shlex.split(value)) == 1, line


def test_a_stale_outputs_file_is_removed_before_cdk_runs(monkeypatch, tmp_path):
    capture: dict = {}
    stale = tmp_path / "cdk.provision-outputs.json"
    stale.write_text(json.dumps({"Strandly-Backend-dev": {"SecretArn": "arn:OLD"}}))
    _stub_cdk(monkeypatch, tmp_path, capture, outputs=None)  # cdk "succeeds" but writes nothing
    result = prov.provision(env="dev", region="us-west-2")
    assert result.secret_arn is None, "a previous run's outputs are never reported as this run's"


def test_the_agent_network_outputs_are_returned_and_exported(monkeypatch, tmp_path, capsys):
    """The Sandbox stack owns the env's one VPC; its agent subnets + SG reach `strandly deploy`."""
    capture: dict = {}
    _stub_cdk(
        monkeypatch,
        tmp_path,
        capture,
        outputs={
            "Strandly-Backend-dev": {"SecretArn": "arn:aws:secretsmanager:::secret:x"},
            "Strandly-Sandbox-dev": {"SandboxRuntimeArn": _SANDBOX_ARN, **_NET_OUTPUTS},
        },
    )
    result = prov.provision(env="dev", region="us-west-2")
    assert result.agent_network == _NET_EXPORTS
    out = capsys.readouterr().out
    for env_var, value in _NET_EXPORTS.items():
        assert f"export {env_var}={value}" in out


def test_missing_agent_network_outputs_warn(monkeypatch, tmp_path, capsys):
    capture: dict = {}
    _stub_cdk(
        monkeypatch,
        tmp_path,
        capture,
        outputs={
            "Strandly-Backend-dev": {"SecretArn": "arn:aws:secretsmanager:::secret:x"},
            "Strandly-Sandbox-dev": {"SandboxRuntimeArn": _SANDBOX_ARN},
        },
    )
    result = prov.provision(env="dev", region="us-west-2")
    assert result.agent_network == dict.fromkeys(_NET_EXPORTS)
    captured = capsys.readouterr()
    assert "STRANDLY_AGENT_" not in captured.out
    assert "STRANDLY_AGENT_SECURITY_GROUP_ID, STRANDLY_AGENT_SUBNET_IDS" in captured.err
