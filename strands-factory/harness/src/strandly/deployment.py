"""Portable deployment workspace from an installed wheel; no checkout or AWS discovery.

The wheel carries an explicit build manifest, generic CDK app, Lambda/runtime source,
container inputs and web sources. Materialization copies immutable resources to an
operator-owned directory: synth/build never writes into site-packages. Deployment still
requires reviewed credentials and a separate, explicit deploy command.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from strandly.profiles import Profile


def materialize(destination: Path, profile: Profile | None = None) -> Path:
    assets = Path(__file__).parent / "_deployment"
    if not (assets / "infra" / "app.py").is_file():
        raise RuntimeError("Deployment assets missing: install a built strandly wheel or sdist")
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite deployment workspace: {destination}")
    shutil.copytree(assets, destination)
    if profile is not None:
        cdk_path = destination / "infra" / "cdk.json"
        cdk = json.loads(cdk_path.read_text())
        cdk["context"].update(profile.deployment_context())
        cdk["context"]["profile_defaults"] = json.dumps(profile.runtime_values(), sort_keys=True)
        cdk["context"]["deployment_extensions"] = ",".join(profile.deployment_extensions)
        cdk_path.write_text(json.dumps(cdk, indent=2) + "\n")
    return destination


def validate_profile_coordinates(
    infra: Path, *, name: str, environment: str, region: str | None = None, account: str | None = None,
) -> None:
    """Reject legacy CLI defaults that conflict with explicit profile coordinates, before AWS I/O."""
    coordinates = None
    profile_path = os.environ.get("STRANDLY_PROFILE")
    if profile_path:
        coordinates = Profile.load(profile_path).deployment_context()["profile_coordinates"]
    elif (infra / "cdk.json").is_file():
        coordinates = json.loads((infra / "cdk.json").read_text()).get("context", {}).get("profile_coordinates")
    if not coordinates:
        return  # Existing protected private workflows retain their original argument contract.
    supplied = {"name": name, "env": environment, "region": region, "account": account}
    wrong = [key for key, value in supplied.items() if value is not None and value != coordinates[key]]
    if wrong:
        raise ValueError(f"CLI arguments conflict with profile coordinates ({', '.join(wrong)}); pass matching --name, --stage/--env and --region values")


def synth(workspace: Path, profile: Profile) -> int:
    """Offline composition only. Explicit account/region mean no AWS environment lookup."""
    context = profile.deployment_context()
    context["profile_defaults"] = json.dumps(profile.runtime_values(), sort_keys=True)
    context["deployment_extensions"] = ",".join(profile.deployment_extensions)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("AWS_")}
    environment.update({
        "AWS_EC2_METADATA_DISABLED": "true", "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "CDK_CONTEXT_JSON": json.dumps(context), "CDK_OUTDIR": str(workspace / "cdk.out"),
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
    })
    return subprocess.run(
        [sys.executable, "app.py"], cwd=workspace / "infra", env=environment, check=False,
    ).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("materialize", "synth"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args(argv)
    profile = Profile.load(args.profile)
    profile.deployment_context()  # Validate before writing anything.
    workspace = materialize(args.directory, profile)
    if args.command == "synth":
        return synth(workspace, profile)
    print(workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
