"""Portable synth and independent deployment policy; no credentials or private addon."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra"))
from stacks.backend_stack import BackendStack  # noqa: E402
from stacks.common import Naming  # noqa: E402


def test_kebab_names_and_access_scopes_are_independent(tmp_path):
    left = Naming(name="example-user", env="preview")
    right = Naming(name="example-second-agent", env="preview")
    assert left.under == "example_user_preview"
    assert "-" not in left.memory
    assert left.memory != right.memory
    assert left.agent_tag != right.agent_tag
    assert left.managed_prefix != right.managed_prefix
    assert left.infra_tag != right.infra_tag
    for naming, account, other in ((left, "111111111111", right), (right, "222222222222", left)):
        from strandly.profiles import Deployment, Profile
        profile = Profile(deployment=Deployment(naming.name, naming.env, account, "us-west-2", {"config": "example/config"}))
        values = profile.runtime_values()
        assert values["STRANDLY_TEST_MANAGED_BY_VALUE"] == naming.agent_tag
        assert values["STRANDLY_TEST_NAME_PREFIX"] == naming.managed_prefix
        assert values["STRANDLY_PROTECTED_MANAGED_BY_VALUE"] == naming.infra_tag
        app = cdk.App(outdir=str(tmp_path / naming.under))
        # ci_bedrock_role=True: the Code Interpreter role carries the per-deployment ABAC scope.
        stack = BackendStack(app, naming.stack("Backend"), naming=naming, ci_bedrock_role=True,
                             env=cdk.Environment(account=account, region="us-west-2"))
        template = json.dumps(Template.from_stack(stack).to_json())
        assert naming.agent_tag in template
        assert naming.managed_prefix in template
        assert other.agent_tag not in template
        assert other.managed_prefix not in template


def test_distinct_coordinates_cannot_alias_state_or_authorization_names():
    from strandly.profiles import Deployment

    # Both pairs would otherwise derive `example-agent-preview` / `example_agent_preview`.
    accepted = Naming("example-agent", "preview")
    assert accepted.hyphen == "example-agent-preview" and accepted.under == "example_agent_preview"
    for constructor in (Naming, lambda name, env: Deployment(name, env, "111111111111", "us-west-2", {"config": "example/config"})):
        with pytest.raises(ValueError, match="deployment.environment must be lowercase letters and digits"):
            constructor("example", "agent-preview")

    names = ("a", "b", "ab", "a-b", "a-b-c", "b-c", "a-bc", "a--b", "a-", "example", "example-agent")
    environments = ("a", "b", "c", "bc", "b1", "preview", "agentpreview")
    namings = [Naming(name, env) for name in names for env in environments]
    derived = {
        "hyphen": lambda n: n.hyphen, "under": lambda n: n.under, "memory": lambda n: n.memory,
        "run_ledger_table": lambda n: n.run_ledger_table, "people_table": lambda n: n.people_table,
        "session_bucket": lambda n: n.session_bucket("111111111111"),
        "agent_tag": lambda n: n.agent_tag, "infra_tag": lambda n: n.infra_tag,
        "managed_prefix": lambda n: n.managed_prefix,
        "instance_role": lambda n: n.instance_role("sandbox"), "stack": lambda n: n.stack("Data"),
    }
    assert {key: len({value(n) for n in namings}) for key, value in derived.items()} == dict.fromkeys(derived, len(namings))


@pytest.mark.parametrize(("name", "env"), [("strandly", "pre-view"), ("strandly", "Prod"), ("strandly", ""), ("Strandly", "prod"), ("strandly_x", "prod"), ("", "prod")])
def test_naming_applies_the_profile_coordinate_contract(name, env):
    with pytest.raises(ValueError, match="deployment.(name|environment) must be"):
        Naming(name, env)


@pytest.mark.skipif(sys.version_info < (3, 11), reason="the CDK app reads uv.lock with tomllib")
def test_generic_app_synthesizes_without_credentials_or_addons(tmp_path):
    app = Path(__file__).resolve().parents[2] / "infra" / "app.py"
    context = {
        "name": "example-agent", "env": "preview", "account": "111111111111",
        "region": "us-west-2", "with_kb": "false", "sandbox_bedrock_grants": "false",
        "secret_arn": "arn:aws:secretsmanager:us-west-2:111111111111:secret:example/config-AbCdEf",
    }
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "STRANDLY_"))}
    environment.update({"AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
                        "AWS_EC2_METADATA_DISABLED": "true", "CDK_OUTDIR": str(tmp_path),
                        "CDK_CONTEXT_JSON": json.dumps(context),
                        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1"})
    subprocess.run([sys.executable, str(app)], cwd=app.parent, env=environment, check=True)
    assert list(tmp_path.glob("*-Data-preview.template.json"))
    assert not list(tmp_path.glob("*-Oidc-preview.template.json"))
    assert not list(tmp_path.glob("*-RuntimeAccess-preview.template.json"))
    for template in tmp_path.glob("*.template.json"):
        body = template.read_text()
        assert "PeopleAdminFn" not in body


def test_largest_accepted_stem_synthesizes_bounded_memory_and_iam_names(tmp_path):
    from stacks.sandbox_stack import SandboxStack

    from strandly.profiles import Deployment
    deployment = Deployment("a" * 24, "b" * 7, "111111111111", "us-west-2", {"config": "example/config"})
    naming = Naming(deployment.name, deployment.environment)
    app = cdk.App(outdir=str(tmp_path / "assembly"))
    environment = cdk.Environment(account=deployment.account, region=deployment.region)
    backend = BackendStack(app, naming.stack("Backend"), naming=naming, env=environment)
    sandbox = SandboxStack(app, naming.stack("Sandbox"), naming=naming,
                           asset_directory=str(Path(__file__).resolve().parents[2] / "sandbox"),
                           sagemaker_test_grants=True, env=environment)
    assert len(naming.memory) <= 48
    for stack in (backend, sandbox):
        for resource in Template.from_stack(stack).to_json()["Resources"].values():
            properties = resource.get("Properties", {})
            for key in ("RoleName", "FunctionName"):
                if isinstance(properties.get(key), str):
                    assert len(properties[key]) <= 64
    app.synth()
