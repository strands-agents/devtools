"""Tests for serving.deploy.deploy() — the observability env wiring (no toolkit, no AWS).

These pin that ``deploy()`` enables AgentCore observability by default (and lets the caller force it
off), without actually shelling out to the agentcore toolkit. We monkeypatch the toolkit-available
check and capture the env each subprocess run is invoked with via the deploy command list.
"""

from __future__ import annotations

import strandly_harness.ops.runtime_client as rc
import strandly_harness.serve.deploy as dep


def test_warn_heavy_source_artifacts_flags_cdk_out(tmp_path, capsys):
    # A big infra/cdk.out in the source tree → the toolkit would zip it into the runtime and blow
    # the 250 MB limit. The deploy step warns up front so the failure is diagnosable.
    big = tmp_path / "infra" / "cdk.out"
    big.mkdir(parents=True)
    (big / "blob").write_bytes(b"x" * (60 * 1024 * 1024))  # 60 MB > threshold
    dep._warn_heavy_source_artifacts(root=tmp_path)
    err = capsys.readouterr().err
    assert "WARNING" in err and "infra/cdk.out" in err and "rm -rf" in err


def test_warn_heavy_source_artifacts_silent_when_clean(tmp_path, capsys):
    (tmp_path / "infra").mkdir()  # no cdk.out / build
    dep._warn_heavy_source_artifacts(root=tmp_path)
    assert capsys.readouterr().err == ""


def _capture_deploy_env(monkeypatch) -> dict[str, str]:
    """Run deploy() with the toolkit + subprocess stubbed; return the env folded into --env flags."""
    monkeypatch.setattr(dep, "_toolkit_available", lambda: True)
    monkeypatch.setattr(rc, "_arn_from_local_yaml", lambda *a, **k: None)
    captured: dict[str, str] = {}

    class _Result:
        returncode = 0

    def fake_run(cmd, *a, **k):
        # The deploy step is the one carrying repeated --env KEY=VALUE pairs.
        if "deploy" in cmd:
            it = iter(cmd)
            for tok in it:
                if tok == "--env":
                    key, _, value = next(it).partition("=")
                    captured[key] = value
        return _Result()

    monkeypatch.setattr(dep.subprocess, "run", fake_run)
    return captured


def test_deploy_enables_observability_by_default(monkeypatch):
    captured = _capture_deploy_env(monkeypatch)
    rc = dep.deploy(name="strandly", region="us-west-2", env={"AWS_REGION": "us-west-2"})
    assert rc == 0
    assert captured["AGENT_OBSERVABILITY_ENABLED"] == "true"
    assert captured["AWS_REGION"] == "us-west-2"  # caller env preserved


def test_deploy_observability_off(monkeypatch):
    captured = _capture_deploy_env(monkeypatch)
    dep.deploy(name="strandly", region="us-west-2", env={}, observability=False)
    assert "AGENT_OBSERVABILITY_ENABLED" not in captured


def test_deploy_explicit_observability_env_wins(monkeypatch):
    # An explicit AGENT_OBSERVABILITY_ENABLED in env overrides the default-on injection.
    captured = _capture_deploy_env(monkeypatch)
    dep.deploy(
        name="strandly",
        region="us-west-2",
        env={"AGENT_OBSERVABILITY_ENABLED": "false"},
    )
    assert captured["AGENT_OBSERVABILITY_ENABLED"] == "false"
