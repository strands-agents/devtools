"""``strandly deploy``: deploy the agent runtime, and resolve the deployed runtime from anywhere.

Builds the code bundle (``infra/scripts/build_agent_bundle.py --from-lock``) and runs
``cdk deploy <App>-AgentRuntime-<env> --exclusively``. Its three backend ids come from ``--env``, the
environment, or the Backend and Sandbox stack outputs, in that order. It records the runtime ARN and
region in ``~/.strandly/runtime.json`` for ``strandly invoke`` and ``strandly poll``, and prints
``runtime_arn=``, ``runtime_id=`` and ``exec_role=`` for a deploy pipeline. See docs/deploy.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Runtime ARN/region resolution + the user-global runtime record live in ops (boto3/stdlib-only
# territory) because the deployed Lambdas resolve the same way; re-exported here for the CLI.
from strandly.ops.runtime_client import (  # noqa: F401  (re-export for CLI/tests)
    record_runtime,
    resolve_region,
    resolve_runtime_arn,
)

#: ``strandly deploy`` ``--env`` keys -> the AgentRuntime stack's CDK context. The runtime takes no
#: other env from the command line: the stack fixes the rest (region, observability, the runtime
#: marker) and the agent reads everything else from its config secret.
CDK_ENV_CONTEXT = {
    "STRANDLY_SECRETS_ARN": "secret_arn",
    "AGENTCORE_MEMORY_ID": "memory_id",
    "STRANDLY_SANDBOX_RUNTIME_ARN": "sandbox_runtime_arn",
    # The sandbox sessions' sliding TTL (the agent moves each session's delete schedule on use).
    "STRANDLY_SANDBOX_CAPACITY_PROVIDER_ID": "sandbox_capacity_provider_id",
    "STRANDLY_SANDBOX_TTL_SCHEDULE_GROUP": "sandbox_ttl_schedule_group",
    "STRANDLY_SANDBOX_TTL_ROLE_ARN": "sandbox_ttl_role_arn",
    # Read only by the retained Instances standby (-c agent_instances_standby=true).
    "STRANDLY_AGENT_SUBNET_IDS": "agent_subnet_ids",
    "STRANDLY_AGENT_SECURITY_GROUP_ID": "agent_security_group_id",
}
#: Where each key is found when neither ``--env`` nor the process env has it: (stack kind, output).
BACKEND_OUTPUTS = {
    "STRANDLY_SECRETS_ARN": ("Backend", "SecretArn"),
    "AGENTCORE_MEMORY_ID": ("Backend", "MemoryId"),
    "STRANDLY_SANDBOX_RUNTIME_ARN": ("Sandbox", "SandboxRuntimeArn"),
    "STRANDLY_SANDBOX_CAPACITY_PROVIDER_ID": ("Sandbox", "CapacityProviderId"),
    "STRANDLY_SANDBOX_TTL_SCHEDULE_GROUP": ("Sandbox", "SandboxTtlScheduleGroup"),
    "STRANDLY_SANDBOX_TTL_ROLE_ARN": ("Sandbox", "SandboxTtlSchedulerRoleArn"),
    "STRANDLY_AGENT_SUBNET_IDS": ("Sandbox", "AgentSubnetIds"),
    "STRANDLY_AGENT_SECURITY_GROUP_ID": ("Sandbox", "AgentSecurityGroupId"),
}


def stack_name(app_name: str, kind: str, stage: str) -> str:
    """``Strandly-AgentRuntime-prod`` — the app's stack-id scheme (``Naming.stack``)."""
    return f"{app_name.capitalize()}-{kind}-{stage}"


def resolve_backend_env(
    *,
    app_name: str,
    stage: str,
    region: str,
    env: dict[str, str],
    environ: dict[str, str] | None = None,
    lookup: Callable[[str, str, str], str | None] | None = None,
) -> dict[str, str]:
    """The backend ids: ``env`` (``--env``) > ``environ`` > the deployed stacks' outputs.

    Only keys still missing reach ``lookup`` (``provisioning.deployed_stack_outputs``), so a fully
    specified call (a deploy pipeline) makes no AWS call here. Raises:
        ValueError: On an ``--env`` key the stack does not take (a typo must not be dropped).
    """
    unknown = sorted(set(env) - set(CDK_ENV_CONTEXT))
    if unknown:
        raise ValueError(
            f"deploy takes no runtime env beyond {sorted(CDK_ENV_CONTEXT)} (got {unknown}); the "
            "agent reads everything else from its config secret"
        )
    environ = dict(os.environ) if environ is None else environ
    resolved: dict[str, str] = {}
    for key in CDK_ENV_CONTEXT:
        value = env.get(key) or environ.get(key) or ""
        if not value:
            if lookup is None:
                lookup = _cached_stack_output()
            kind, output = BACKEND_OUTPUTS[key]
            value = lookup(stack_name(app_name, kind, stage), output, region) or ""
        if value:
            resolved[key] = value
    return resolved


def _cached_stack_output() -> Callable[[str, str, str], str | None]:
    """A ``lookup`` for :func:`resolve_backend_env` that describes each stack at most once.

    Several keys come from one stack (the Sandbox stack holds four), and each ``DescribeStacks`` is a
    round trip that can be throttled; a missing stack is remembered as "no outputs".
    """
    from strandly.serve.provisioning import deployed_stack_outputs

    cache: dict[str, dict[str, str]] = {}

    def lookup(stack: str, output: str, region: str) -> str | None:
        if stack not in cache:
            cache[stack] = deployed_stack_outputs(stack, region) or {}
        return cache[stack].get(output) or None

    return lookup


def cdk_deploy_command(
    *,
    cdk: list[str],
    app_name: str,
    stage: str,
    region: str,
    env: dict[str, str],
    bundle_dir: Path,
    outputs_file: Path,
) -> list[str]:
    """The ``cdk deploy`` argv for the AgentRuntime stack (pure — unit-tested).

    Raises:
        ValueError: On a missing required key or a key the stack does not take.
    """
    unknown = sorted(set(env) - set(CDK_ENV_CONTEXT))
    if unknown:
        raise ValueError(f"unexpected runtime env {unknown}")
    missing = [key for key in CDK_ENV_CONTEXT if not env.get(key)]
    if missing:
        raise ValueError(
            f"no {'/'.join(missing)}: pass --env KEY=VALUE, export it, or deploy the "
            f"{stack_name(app_name, 'Backend', stage)} / {stack_name(app_name, 'Sandbox', stage)} "
            "stacks first (strandly provision)"
        )
    context = {
        "name": app_name,
        "env": stage,
        "region": region,
        "agent_code_asset": str(bundle_dir),
        **{CDK_ENV_CONTEXT[k]: v for k, v in env.items()},
    }
    cmd = [*cdk, "deploy", stack_name(app_name, "AgentRuntime", stage), "--exclusively",
           "--require-approval", "never", "--outputs-file", str(outputs_file)]  # fmt: skip
    for key, value in context.items():
        cmd += ["-c", f"{key}={value}"]
    return cmd


def deploy_cdk(
    *,
    app_name: str = "strandly",
    stage: str,
    region: str,
    env: dict[str, str],
    build_bundle: bool = True,
) -> int:
    """Deploy the agent runtime: build the bundle, ``cdk deploy`` the AgentRuntime stack.

    Builds the code bundle (``infra/scripts/build_agent_bundle.py --from-lock`` unless
    ``build_bundle=False`` and ``infra/build/agent`` already exists), runs ``cdk deploy`` for
    ``<App>-AgentRuntime-<stage>``, records the runtime for invoke/poll, and prints
    ``runtime_arn=`` / ``runtime_id=`` / ``exec_role=`` lines on **stdout** (a deploy pipeline appends them to
    ``$GITHUB_OUTPUT``; everything else goes to stderr). Returns a process exit code.
    """
    from strandly.serve.provisioning import _cdk_command, _find_infra_dir

    infra = _find_infra_dir()
    cdk = _cdk_command()
    if infra is None or cdk is None:
        print(
            "strandly deploy needs a materialized workspace or STRANDLY_PROFILE, and the CDK CLI (npx aws-cdk).",
            file=sys.stderr,
        )
        return 1
    bundle = infra / "build" / "agent"
    outputs_file = infra / "cdk.agent-runtime-outputs.json"
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        from strandly.deployment import validate_profile_coordinates
        validate_profile_coordinates(infra, name=app_name, environment=stage, region=region)
        backend = resolve_backend_env(app_name=app_name, stage=stage, region=region, env=env)
        cmd = cdk_deploy_command(
            cdk=cdk, app_name=app_name, stage=stage, region=region, env=backend,
            bundle_dir=bundle, outputs_file=outputs_file,
        )  # fmt: skip
    except ValueError as exc:
        print(f"strandly deploy: {exc}", file=sys.stderr)
        return 2
    except (ClientError, BotoCoreError) as exc:  # BotoCoreError covers NoCredentialsError
        print(
            f"strandly deploy: could not read the deployed {stack_name(app_name, 'Backend', stage)} / "
            f"{stack_name(app_name, 'Sandbox', stage)} stack outputs in {region} "
            f"({type(exc).__name__}: {exc}). Check your AWS credentials and region, or pass every "
            f"value with --env KEY=VALUE ({', '.join(CDK_ENV_CONTEXT)}).",
            file=sys.stderr,
        )
        return 1

    if build_bundle or not (bundle / "main.py").is_file():
        script = infra / "scripts" / "build_agent_bundle.py"
        build = [sys.executable, str(script), "--out", str(bundle),
                 "--repo-root", str(infra.parent), "--from-lock"]  # fmt: skip
        print(f"# strandly deploy: building the agent bundle -> {bundle}", file=sys.stderr)
        rc = subprocess.run(build, stdout=sys.stderr).returncode
        if rc != 0:
            print("strandly deploy: agent bundle build failed", file=sys.stderr)
            return rc

    # A previous deploy's outputs must never be reported as this one's (cdk may fail before writing).
    outputs_file.unlink(missing_ok=True)
    print(f"# strandly deploy: {' '.join(cmd[:4])}", file=sys.stderr, flush=True)
    rc = subprocess.run(cmd, cwd=infra, stdout=sys.stderr).returncode
    if rc != 0:
        print("strandly deploy: cdk deploy failed", file=sys.stderr)
        return rc
    return _report_cdk_outputs(outputs_file, stack_name(app_name, "AgentRuntime", stage), region)


def _report_cdk_outputs(outputs_file: Path, stack: str, region: str) -> int:
    try:
        outputs = json.loads(outputs_file.read_text()).get(stack, {})
    except (OSError, json.JSONDecodeError):
        outputs = {}
    arn = outputs.get("AgentRuntimeArn", "")
    role = outputs.get("AgentExecRoleName", "")
    if not arn or not role:
        print(f"strandly deploy: {outputs_file} has no AgentRuntimeArn/AgentExecRoleName for {stack}",
              file=sys.stderr)  # fmt: skip
        return 1
    record_runtime(arn, region)
    print(f"Recorded runtime for invoke/poll from any folder: {arn}", file=sys.stderr)
    print(f"runtime_arn={arn}")
    print(f"runtime_id={outputs.get('AgentRuntimeId') or arn.rsplit('/', 1)[-1]}")
    print(f"exec_role={role}")
    return 0


def invoke(arn: str, region: str, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke the deployed runtime directly (boto3), from any folder. Returns the parsed response."""
    from strandly.ops.runtime_client import _invoke

    return _invoke(arn, region, session_id, payload)
