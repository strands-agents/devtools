"""Deploy Strandly to a hosted AgentCore Runtime, and resolve the deployed runtime from anywhere.

``strandly deploy`` drives the bedrock-agentcore starter toolkit (``agentcore configure`` +
``agentcore deploy``) so the whole lifecycle lives under one CLI. It then records the deployed
runtime ARN + region to a **user-global** file (``~/.strandly/runtime.json``) so ``strandly invoke``
and ``strandly poll`` work **from any directory** — unlike ``agentcore invoke``, which only reads
``.bedrock_agentcore.yaml`` from the current folder.

ARN resolution order (so it works whether or not you just deployed, and from any cwd):
1. an explicit ``--runtime-arn``
2. ``$STRANDLY_RUNTIME_ARN``
3. ``~/.strandly/runtime.json`` (written by ``strandly deploy``)
4. ``./.bedrock_agentcore.yaml`` (the toolkit's per-project file, if you're in the deploy dir)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

# Runtime ARN/region resolution + the user-global runtime record live in ops (boto3/stdlib-only
# territory) because the deployed Lambdas resolve the same way; re-exported here for the CLI.
from strandly_harness.ops import runtime_client as _runtime_client
from strandly_harness.ops.runtime_client import (  # noqa: F401  (re-export for CLI/tests)
    record_runtime,
    resolve_region,
    resolve_runtime_arn,
)

# Local build artifacts that the agentcore toolkit does NOT exclude from the runtime source zip but
# that the runtime never needs. `infra/cdk.out` (CDK synth output — staged Lambda assets) and
# `infra/build` (the prebuilt poller wheels, ~230 MB) can be 1+ GB combined and silently push the
# package past the 250 MB AgentCore limit. The toolkit's bundled dockerignore only segment-matches
# bare `build/`/`cdk/` (so it catches `infra/build` but NOT `infra/cdk.out`), and it ignores any
# user .dockerignore — so we can't inject excludes. We warn so the failure is diagnosable up front.
_HEAVY_SOURCE_ARTIFACTS = ("infra/cdk.out", "infra/build")
_HEAVY_ARTIFACT_THRESHOLD_MB = 50


def _dir_size_mb(path: Path) -> float:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)


def _warn_heavy_source_artifacts(root: Path | None = None) -> None:
    """Warn if heavy, runtime-irrelevant build artifacts sit in the deploy source tree.

    The toolkit zips the whole source dir (minus its own hardcoded ignores) into the runtime
    package. CDK synth output / built Lambda assets under ``infra/`` aren't excluded and can blow the
    250 MB limit. Clearing them (``rm -rf infra/cdk.out infra/build``) before deploy avoids it.
    """
    base = root or Path.cwd()
    offenders = []
    for rel in _HEAVY_SOURCE_ARTIFACTS:
        p = base / rel
        if p.is_dir():
            mb = _dir_size_mb(p)
            if mb >= _HEAVY_ARTIFACT_THRESHOLD_MB:
                offenders.append((rel, mb))
    if offenders:
        listing = ", ".join(f"{rel} (~{mb:.0f} MB)" for rel, mb in offenders)
        print(
            f"strandly deploy: WARNING — heavy build artifacts in the source tree will be packaged "
            f"into the runtime and may exceed the 250 MB limit: {listing}. The agentcore toolkit "
            f"does not exclude these. Clear them first:\n  rm -rf {' '.join(_HEAVY_SOURCE_ARTIFACTS)}",
            file=sys.stderr,
        )


def _toolkit_available() -> bool:
    from shutil import which

    return which("agentcore") is not None


def deploy(
    *,
    name: str = "strandly",
    region: str,
    env: dict[str, str],
    entrypoint: str = "main.py",
    requirements: str = "requirements.txt",
    observability: bool = True,
) -> int:
    """Run ``agentcore configure`` + ``agentcore deploy``, then record the runtime globally.

    ``env`` are the runtime env vars (backend ids / secret arn) passed to ``agentcore deploy --env``.
    Returns a process exit code.

    ``observability`` (default ``True``) sets ``AGENT_OBSERVABILITY_ENABLED=true`` on the runtime so
    it emits GenAI traces to CloudWatch / X-Ray (what the dashboard's trace deep-links read). This
    pairs with ``aws-opentelemetry-distro`` in ``requirements.txt`` — without that dependency the
    toolkit won't wrap the entrypoint in ``opentelemetry-instrument`` and the env var is a no-op. An
    explicit ``AGENT_OBSERVABILITY_ENABLED`` in ``env`` always wins (so callers can force it off).
    """
    if not _toolkit_available():
        print(
            "strandly deploy needs the agentcore toolkit: pip install "
            "bedrock-agentcore-starter-toolkit",
            file=sys.stderr,
        )
        return 1

    _warn_heavy_source_artifacts()

    # Enable observability by default (caller's explicit value wins). The runtime side needs both
    # this flag and the otel distro in requirements; we ship the distro in requirements.txt.
    if observability:
        env = {"AGENT_OBSERVABILITY_ENABLED": "true", **env}

    configure = [
        "agentcore", "configure", "--entrypoint", entrypoint, "--name", name,
        "--requirements-file", requirements, "--disable-memory",
        "--region", region, "--non-interactive",
    ]  # fmt: skip
    rc = subprocess.run(configure).returncode
    if rc != 0:
        print("agentcore configure failed", file=sys.stderr)
        return rc

    deploy_cmd = ["agentcore", "deploy", "--auto-update-on-conflict"]
    for k, v in env.items():
        deploy_cmd += ["--env", f"{k}={v}"]
    rc = subprocess.run(deploy_cmd).returncode
    if rc != 0:
        print("agentcore deploy failed", file=sys.stderr)
        return rc

    arn = _runtime_client._arn_from_local_yaml()
    if arn:
        record_runtime(arn, region)
        print(f"\nRecorded runtime for invoke/poll from any folder: {arn}", file=sys.stderr)
    else:
        print(
            "deploy succeeded but could not read the runtime ARN from .bedrock_agentcore.yaml; "
            "invoke/poll will need --runtime-arn",
            file=sys.stderr,
        )
    return 0


def invoke(arn: str, region: str, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke the deployed runtime directly (boto3), from any folder. Returns the parsed response."""
    from strandly_harness.ops.runtime_client import _invoke

    return _invoke(arn, region, session_id, payload)
