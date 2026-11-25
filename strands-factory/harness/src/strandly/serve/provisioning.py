"""``strandly provision``: deploy the Backend, Data and Sandbox stacks of the CDK app.

A thin wrapper over ``cdk deploy`` for the stacks that hold Memory, the config secret, the tables
and the sandbox runtime. It prints the secret ARN, sandbox runtime ARN, TTL ids and agent network
ids as shell exports and writes every stack output to ``infra/cdk.provision-outputs.json``. A
``--github-token`` is written to the config secret after the deploy. See docs/deploy.md.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ProvisionResult:
    secret_arn: str | None
    region: str | None
    #: The sandbox runtime's ARN, from the Sandbox stack's ``SandboxRuntimeArn`` output. The agent
    #: refuses to run on a deployed runtime without it (``sandbox/select.py``), so callers should
    #: treat a ``None`` here as fatal rather than deploy an agent that cannot execute anything.
    sandbox_runtime_arn: str | None = None
    outputs: dict[str, dict[str, str]] = field(default_factory=dict)
    #: The sandbox sessions' sliding-TTL wiring (Sandbox stack outputs), exported for the AGENT
    #: runtime: env var name -> value (``None`` when the output is missing).
    sandbox_ttl: dict[str, str | None] = field(default_factory=dict)
    #: The agent runtime's network (Sandbox stack outputs): env var name -> value (or ``None``).
    agent_network: dict[str, str | None] = field(default_factory=dict)


#: Sandbox stack output -> the agent-runtime env var it becomes (the harness's ``sandbox/ttl.py``
#: reads these; ``core/config.py`` names them). Exported next to ``STRANDLY_SANDBOX_RUNTIME_ARN``.
SANDBOX_TTL_OUTPUTS = {
    "CapacityProviderId": "STRANDLY_SANDBOX_CAPACITY_PROVIDER_ID",
    "SandboxTtlScheduleGroup": "STRANDLY_SANDBOX_TTL_SCHEDULE_GROUP",
    "SandboxTtlSchedulerRoleArn": "STRANDLY_SANDBOX_TTL_ROLE_ARN",
}
#: Sandbox stack output -> env var for the AGENT runtime's network: the Sandbox stack owns the
#: environment's one VPC, and ``strandly deploy`` passes these to the AgentRuntime stack as context.
AGENT_NETWORK_OUTPUTS = {
    "AgentSubnetIds": "STRANDLY_AGENT_SUBNET_IDS",
    "AgentSecurityGroupId": "STRANDLY_AGENT_SECURITY_GROUP_ID",
}


def _find_infra_dir() -> Path | None:
    """Walk up from cwd (then this file) looking for the ``infra/app.py`` CDK app."""
    explicit = os.environ.get("STRANDLY_DEPLOYMENT_DIR")
    if explicit:
        candidate = Path(explicit).resolve() / "infra" / "app.py"
        if not candidate.is_file():
            raise FileNotFoundError(f"Configured deployment workspace is missing: {candidate}")
        return candidate.parent
    for start in (Path.cwd(), Path(__file__).resolve()):
        for parent in (start, *start.parents):
            candidate = parent / "infra" / "app.py"
            if candidate.is_file():
                return candidate.parent
    # Installed wheel: materialize resources, never assume a checkout or write site-packages.
    import tempfile

    from strandly.deployment import materialize
    from strandly.profiles import Profile
    profile_path = os.environ.get("STRANDLY_PROFILE")
    if not profile_path:
        return None
    destination = Path(tempfile.mkdtemp(prefix="strandly-deploy-")) / "workspace"
    return materialize(destination, Profile.load(profile_path)) / "infra"


def _cdk_command() -> list[str] | None:
    """Resolve how to invoke the CDK CLI: a ``cdk`` on PATH, else ``npx aws-cdk``."""
    if shutil.which("cdk"):
        return ["cdk"]
    if shutil.which("npx"):
        return ["npx", "--yes", "aws-cdk@2"]
    return None


def deployed_stack_outputs(
    stack: str, region: str | None, *, client: Any = None
) -> dict[str, str] | None:
    """Every output of an already-deployed CloudFormation stack (``None`` if the stack is absent).

    One ``DescribeStacks`` call, so a caller needing several outputs of one stack reads it once.
    Only "stack does not exist" maps to None. Every other error (throttling, AccessDenied, expired
    credentials) propagates, so the deploy stops on the real cause instead of failing later on an
    empty sandbox ARN with a message that blames a missing output.
    """
    from botocore.exceptions import ClientError

    if client is None:
        import boto3

        client = boto3.Session(region_name=region).client("cloudformation")
    try:
        described = client.describe_stacks(StackName=stack)["Stacks"]
    except ClientError as exc:
        if "does not exist" not in str(exc):
            raise
        logger.warning("stack %s does not exist; no outputs", stack)
        return None
    return {
        str(out["OutputKey"]): str(out.get("OutputValue") or "")
        for out in ((described[0].get("Outputs") or []) if described else [])
        if out.get("OutputKey")
    }


def store_secret_values(
    secret_arn: str, region: str | None, values: dict[str, str], *, client: Any = None
) -> None:
    """Merge ``values`` into the live config secret (read, overlay, write).

    Secrets go straight to Secrets Manager, never through CDK context: a context value is written
    in plaintext into the template and ``cdk.out``. Other keys in the secret are kept.
    """
    if client is None:
        import boto3

        client = boto3.Session(region_name=region).client("secretsmanager")
    current = json.loads(client.get_secret_value(SecretId=secret_arn).get("SecretString") or "{}")
    client.put_secret_value(SecretId=secret_arn, SecretString=json.dumps({**current, **values}))
    # Fixed text: nothing derived from ``values`` reaches a log.
    print("# strandly provision: stored the extra secret value(s) in the config secret", file=sys.stderr)


def provision(
    *,
    name: str = "strandly",
    env: str = "dev",
    region: str | None = None,
    account: str | None = None,
    extra_secrets: dict[str, str] | None = None,
    skip_sandbox: bool = False,
) -> ProvisionResult:
    """Deploy the Backend + Data stacks via ``cdk deploy`` and return the resolved secret ARN.

    ``env`` is the environment suffix (``dev`` / ``prod`` / …) that isolates one deployment's
    resources from another.
    """
    infra = _find_infra_dir()
    if infra is None:
        print(
            "strandly provision: could not find the infra/ CDK app. Materialize a workspace with strandly-package, or set STRANDLY_PROFILE "
            "to select explicit deployment inputs.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    from strandly.deployment import validate_profile_coordinates
    try:
        validate_profile_coordinates(infra, name=name, environment=env, region=region, account=account)
    except ValueError as exc:
        print(f"strandly provision: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    cdk = _cdk_command()
    if cdk is None:
        print(
            "strandly provision needs the CDK toolkit: install Node and either `npm i -g aws-cdk` "
            "or have `npx` available. Then re-run.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    context = {
        "name": name,
        "env": env,
    }
    if region:
        context["region"] = region
    if account:
        context["account"] = account
    cap = name.capitalize()
    # Sandbox last in the list purely for readability; CloudFormation/CDK order these independently
    # (they share no references). It is NOT optional: a deployed agent with no sandbox refuses to
    # run, so provisioning "the backends" without it would only ever produce a broken deployment.
    sandbox_stack = f"{cap}-Sandbox-{env}"
    stacks = [f"{cap}-Data-{env}", f"{cap}-Backend-{env}"]
    if not skip_sandbox:
        stacks.append(sandbox_stack)
    outputs_file = infra / "cdk.provision-outputs.json"
    # A previous run's file must never be read as this run's outputs (e.g. a stack renamed since).
    outputs_file.unlink(missing_ok=True)

    cmd = [
        *cdk,
        "deploy",
        *stacks,
        # --exclusively: deploy EXACTLY these three and nothing they depend on. Without it, CDK
        # decides an export is unused whenever the consuming stack is absent from the synthesized
        # app — and MonitoringStack is *conditionally* synthesized (it needs the poller Lambda asset
        # built, see infra/app.py). So on a runner that hasn't built that asset, Data's run-ledger
        # export gets dropped while the live Strandly-Monitoring-<env> still imports it, and
        # CloudFormation refuses mid-deploy: "Cannot delete export … as it is in use by
        # Strandly-Monitoring-<env>" → UPDATE_ROLLBACK_COMPLETE. That is exactly how the first
        # GitHub-Actions deploy failed; it worked locally only because the asset happened to exist.
        "--exclusively",
        "--require-approval",
        "never",
        "--outputs-file",
        str(outputs_file),
    ]
    for key, value in context.items():
        cmd += ["-c", f"{key}={value}"]

    print(f"# strandly provision: cdk deploy {' '.join(stacks)}", file=sys.stderr, flush=True)
    # stdout is reserved for the `export ...` lines below (callers redirect it to a file and source
    # that, as a deploy pipeline does; every value in them is shlex-quoted),
    # so cdk's own progress output — which can contain shell metacharacters, e.g. the bootstrap
    # notice's "(cli): Bootstrap stack outdated" — must be redirected to stderr, not inherited.
    rc = subprocess.run(cmd, cwd=infra, stdout=sys.stderr).returncode
    if rc != 0:
        print("strandly provision: cdk deploy failed", file=sys.stderr)
        raise SystemExit(rc)

    outputs: dict[str, dict[str, str]] = {}
    if outputs_file.is_file():
        try:
            outputs = json.loads(outputs_file.read_text())
        except json.JSONDecodeError:
            pass

    backend = outputs.get(f"{cap}-Backend-{env}", {})
    secret_arn = backend.get("SecretArn")
    sandbox_outputs = outputs.get(sandbox_stack, {})
    if skip_sandbox:
        # The sandbox stack was deliberately left alone (`--skip-sandbox`). Its outputs still have
        # to reach the agent, so read them from the stack that is already deployed.
        print(f"# strandly provision: {sandbox_stack} skipped (left as deployed)", file=sys.stderr)
        deployed = deployed_stack_outputs(sandbox_stack, region) or {}  # one DescribeStacks
        sandbox_outputs = {
            key: deployed[key] for key in ("SandboxRuntimeArn", *SANDBOX_TTL_OUTPUTS, *AGENT_NETWORK_OUTPUTS) if deployed.get(key)
        }
    sandbox_runtime_arn = sandbox_outputs.get("SandboxRuntimeArn")
    sandbox_ttl = {env_var: sandbox_outputs.get(key) for key, env_var in SANDBOX_TTL_OUTPUTS.items()}
    agent_network = {
        env_var: sandbox_outputs.get(key) for key, env_var in AGENT_NETWORK_OUTPUTS.items()
    }

    print("# strandly provisioned via CDK:", ", ".join(stacks), file=sys.stderr)
    if extra_secrets:
        if not secret_arn:
            print(
                "strandly provision: the Backend stack produced no SecretArn output, so the "
                "extra secret value(s) (e.g. --github-token) were NOT stored. Add them to the "
                "config secret by hand.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        store_secret_values(secret_arn, region, extra_secrets)
    if secret_arn:
        print(f"export STRANDLY_SECRETS_ARN={shlex.quote(secret_arn)}")
    # Exported so `eval "$(strandly provision …)"` wires the agent to its sandbox with no copy step.
    if sandbox_runtime_arn:
        print(f"export STRANDLY_SANDBOX_RUNTIME_ARN={shlex.quote(sandbox_runtime_arn)}")
    else:
        print(
            "# strandly provision: WARNING — the Sandbox stack produced no SandboxRuntimeArn "
            "output. A deployed agent without STRANDLY_SANDBOX_RUNTIME_ARN REFUSES to run.",
            file=sys.stderr,
        )
    # The sandbox sessions' sliding TTL (sandbox/ttl.py in the agent): without these the agent
    # schedules no session deletion, and every session's EBS volume is kept (and billed) forever.
    for env_var, ttl_value in sandbox_ttl.items():
        if ttl_value:
            print(f"export {env_var}={shlex.quote(ttl_value)}")
    missing_ttl = sorted(k for k, v in sandbox_ttl.items() if not v)
    if missing_ttl:
        print(
            "# strandly provision: WARNING — the Sandbox stack produced no output for "
            f"{', '.join(missing_ttl)}. Without them sandbox sessions never expire.",
            file=sys.stderr,
        )
    # The agent runtime's subnets + security group (the Sandbox stack owns the one VPC).
    for env_var, net_value in agent_network.items():
        if net_value:
            print(f"export {env_var}={shlex.quote(net_value)}")
    missing_net = sorted(k for k, v in agent_network.items() if not v)
    if missing_net:
        print(
            "# strandly provision: WARNING — the Sandbox stack produced no output for "
            f"{', '.join(missing_net)}. The agent runtime cannot be deployed without them.",
            file=sys.stderr,
        )
    if region:
        print(f"export AWS_REGION={shlex.quote(region)}")
    return ProvisionResult(
        secret_arn=secret_arn,
        region=region,
        sandbox_runtime_arn=sandbox_runtime_arn,
        outputs=outputs,
        sandbox_ttl=sandbox_ttl,
        agent_network=agent_network,
    )
