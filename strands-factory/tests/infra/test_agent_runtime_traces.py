"""The agent runtimes' trace pipeline: TRACES deliveries to X-Ray and a caller-independent sampler."""
from __future__ import annotations

import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra"))
from stacks.agent_runtime_stack import AgentRuntimeStack, agent_environment  # noqa: E402
from stacks.common import Naming  # noqa: E402

_ENV = cdk.Environment(account="111111111111", region="us-west-2")
_RUNTIME = "AWS::BedrockAgentCore::Runtime"


def _template(tmp_path: Path, **overrides: object) -> Template:
    bundle = tmp_path / "agent-bundle"
    bundle.mkdir(exist_ok=True)
    (bundle / "main.py").write_text("app = None\n")
    kwargs: dict[str, object] = dict(
        naming=Naming(name="example-agent", env="preview"),
        code_asset_dir=str(bundle),
        secret_arn="arn:aws:secretsmanager:us-west-2:111111111111:secret:example/config",
        memory_id="mem-1",
        sandbox_runtime_arn="arn:aws:bedrock-agentcore:us-west-2:111111111111:runtime/sb-1",
        sandbox_capacity_provider_id="example_agent_preview_sandbox_g1-abcdefghij",
        sandbox_ttl_schedule_group="example-agent-preview-sandbox-ttl",
        sandbox_ttl_role_arn="arn:aws:iam::111111111111:role/example-agent-preview-sandbox-ttl",
        env=_ENV,
    )
    kwargs.update(overrides)
    return Template.from_stack(AgentRuntimeStack(cdk.App(), "Agent", **kwargs))


def _runtime_ids(t: Template) -> dict[str, str]:
    return {r["Properties"]["AgentRuntimeName"]: lid for lid, r in t.find_resources(_RUNTIME).items()}


def _assert_delivered(t: Template, runtime_id: str, source_name: str) -> None:
    resources = t.to_json()["Resources"]
    (source_id,) = [
        lid for lid, r in t.find_resources("AWS::Logs::DeliverySource").items()
        if r["Properties"]["Name"] == source_name
    ]
    assert resources[source_id]["Properties"] == {
        "Name": source_name,
        "LogType": "TRACES",
        "ResourceArn": {"Fn::GetAtt": [runtime_id, "AgentRuntimeArn"]},
    }
    (destination_id,) = t.find_resources("AWS::Logs::DeliveryDestination")
    deliveries = [
        r["Properties"] for r in t.find_resources("AWS::Logs::Delivery").values()
        if r["Properties"]["DeliverySourceName"] == {"Ref": source_id}
    ]
    # The references order creation after the runtime and deletion before it.
    assert deliveries == [{
        "DeliverySourceName": {"Ref": source_id},
        "DeliveryDestinationArn": {"Fn::GetAtt": [destination_id, "Arn"]},
    }]


def test_agent_runtime_traces_go_to_xray(tmp_path):
    t = _template(tmp_path)
    t.resource_count_is("AWS::Logs::DeliverySource", 1)
    t.resource_count_is("AWS::Logs::Delivery", 1)
    t.has_resource_properties("AWS::Logs::DeliveryDestination", {
        "Name": "example_agent_preview_agent-traces-xray",
        "DeliveryDestinationType": "XRAY",
    })
    runtime_id = _runtime_ids(t)["example_agent_preview_agent"]
    _assert_delivered(t, runtime_id, "example_agent_preview_agent-traces")


def test_standby_runtime_traces_share_the_destination(tmp_path):
    t = _template(tmp_path, retain_instances_standby=True, agent_subnet_ids="subnet-a",
                  agent_security_group_id="sg-a")
    t.resource_count_is("AWS::Logs::DeliveryDestination", 1)
    t.resource_count_is("AWS::Logs::Delivery", 2)
    ids = _runtime_ids(t)
    _assert_delivered(t, ids["example_agent_preview_agent"], "example_agent_preview_agent-traces")
    _assert_delivered(t, ids["example_agent_preview_agent_g2"], "example_agent_preview_agent_g2-traces")


def test_trace_delivery_adds_no_iam(tmp_path):
    t = _template(tmp_path)
    policies = t.find_resources("AWS::IAM::Policy").values()
    actions = {
        action
        for policy in policies
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        for action in ([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"])
    }
    assert not {a for a in actions if a.startswith(("logs:PutDelivery", "logs:CreateDelivery", "xray:PutResource"))}
    t.resource_count_is("AWS::XRay::ResourcePolicy", 0)
    t.resource_count_is("AWS::Logs::ResourcePolicy", 0)


def test_sampler_ignores_the_callers_sampled_flag_by_default(tmp_path):
    t = _template(tmp_path)
    (runtime,) = t.find_resources(_RUNTIME).values()
    env = runtime["Properties"]["EnvironmentVariables"]
    assert env["OTEL_TRACES_SAMPLER"] == "always_on"
    assert "OTEL_TRACES_SAMPLER_ARG" not in env


def test_sampler_is_configurable(tmp_path):
    t = _template(tmp_path, traces_sampler="traceidratio", traces_sampler_arg="0.25")
    (runtime,) = t.find_resources(_RUNTIME).values()
    env = runtime["Properties"]["EnvironmentVariables"]
    assert (env["OTEL_TRACES_SAMPLER"], env["OTEL_TRACES_SAMPLER_ARG"]) == ("traceidratio", "0.25")


@pytest.mark.parametrize("sampler, arg", [("xray", ""), ("ALWAYS_ON", ""), ("traceidratio", "")])
def test_sampler_rejects_unknown_names_and_ratios_without_an_argument(sampler, arg):
    with pytest.raises(ValueError, match="traces_sampler"):
        agent_environment(
            region="us-west-2", secret_arn="s", memory_id="m", sandbox_runtime_arn="r",
            sandbox_capacity_provider_id="c", sandbox_ttl_schedule_group="g",
            sandbox_ttl_role_arn="t", traces_sampler=sampler, traces_sampler_arg=arg,
        )


def test_largest_accepted_stem_keeps_delivery_names_within_limits():
    naming = Naming("a" * 24, "b" * 7)
    for name in (naming.agent_traces_source(naming.agent_runtime_name(2)), naming.agent_traces_destination):
        assert len(name) <= 60
