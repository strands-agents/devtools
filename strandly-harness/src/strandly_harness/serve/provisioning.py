"""Provision the AWS backends a deployed Strandly agent uses — via the unified CDK app.

``strandly provision`` is a thin wrapper that drives ``cdk deploy`` for the **Backend** and **Data**
stacks of the repo's ``infra/`` CDK app (see ``infra/app.py``). Those stacks create — declaratively,
with CloudFormation handling ordering/idempotency/readiness — everything the old imperative
provisioner created by hand:

- an **AgentCore Memory** resource and an **AgentCore Code Interpreter** (the managed sandbox),
- an **S3-Vectors Knowledge Base** (+ CUSTOM data source) for long-term memory,
- a **Secrets Manager secret** holding the harness config, so a deployed runtime just sets
  ``STRANDLY_SECRETS_ARN`` and gets everything,
- the **run-ledger** and **dedup** DynamoDB tables (Data stack).

It then reads the stack outputs and prints the same copy-pasteable ``export`` lines the imperative
provisioner did, so ``eval "$(strandly provision …)"`` still works. This needs the CDK toolkit
(``npx aws-cdk`` or a ``cdk`` on PATH), AWS credentials, and to be run from a repo checkout (the
``infra/`` app is not part of the installed wheel).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProvisionResult:
    secret_arn: str | None
    region: str | None
    outputs: dict[str, dict[str, str]] = field(default_factory=dict)


def _find_infra_dir() -> Path | None:
    """Walk up from cwd (then this file) looking for the ``infra/app.py`` CDK app."""
    for start in (Path.cwd(), Path(__file__).resolve()):
        for parent in (start, *start.parents):
            candidate = parent / "infra" / "app.py"
            if candidate.is_file():
                return candidate.parent
    return None


def _cdk_command() -> list[str] | None:
    """Resolve how to invoke the CDK CLI: a ``cdk`` on PATH, else ``npx aws-cdk``."""
    if shutil.which("cdk"):
        return ["cdk"]
    if shutil.which("npx"):
        return ["npx", "--yes", "aws-cdk@2"]
    return None


def provision(
    *,
    name: str = "strandly",
    env: str = "dev",
    region: str | None = None,
    account: str | None = None,
    with_kb: bool = True,
    extra_secrets: dict[str, str] | None = None,
) -> ProvisionResult:
    """Deploy the Backend + Data stacks via ``cdk deploy`` and return the resolved secret ARN.

    ``env`` is the environment suffix (``dev`` / ``prod`` / …) that isolates one deployment's
    resources from another. ``with_kb=False`` skips the long-term-memory KB.
    """
    infra = _find_infra_dir()
    if infra is None:
        print(
            "strandly provision: could not find the infra/ CDK app. Run from a repo checkout "
            "(the CDK app is not part of the installed package).",
            file=sys.stderr,
        )
        raise SystemExit(1)

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
        "with_kb": "true" if with_kb else "false",
    }
    if region:
        context["region"] = region
    if account:
        context["account"] = account
    # The Backend stack currently accepts exactly one extra secret, via -c github_token=…. Map the
    # known key; loudly warn on any other so a caller's secret can't be silently dropped (add the
    # key here + a context knob in infra/app.py + the Backend stack to support more).
    for key, value in (extra_secrets or {}).items():
        if key == "STRANDLY_GITHUB_TOKEN":
            context["github_token"] = value
        else:
            print(
                f"# strandly provision: WARNING — extra secret {key!r} is not supported by the "
                "CDK Backend stack and was NOT stored. Only STRANDLY_GITHUB_TOKEN is forwarded "
                "today.",
                file=sys.stderr,
            )

    cap = name.capitalize()
    stacks = [f"{cap}-Data-{env}", f"{cap}-Backend-{env}"]
    outputs_file = infra / "cdk.provision-outputs.json"

    cmd = [
        *cdk,
        "deploy",
        *stacks,
        "--require-approval",
        "never",
        "--outputs-file",
        str(outputs_file),
    ]
    for key, value in context.items():
        cmd += ["-c", f"{key}={value}"]

    print(f"# strandly provision: cdk deploy {' '.join(stacks)}", file=sys.stderr, flush=True)
    rc = subprocess.run(cmd, cwd=infra).returncode
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

    print("# strandly provisioned via CDK:", ", ".join(stacks), file=sys.stderr)
    if secret_arn:
        print(f"export STRANDLY_SECRETS_ARN={secret_arn}")
    if region:
        print(f"export AWS_REGION={region}")
    return ProvisionResult(secret_arn=secret_arn, region=region, outputs=outputs)
