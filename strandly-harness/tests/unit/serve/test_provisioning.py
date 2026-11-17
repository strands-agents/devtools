"""Tests for provision() — the CDK-wrapper behavior, without invoking cdk or AWS.

We stub the infra-dir + cdk-command resolution and subprocess.run, so these exercise the pure
wiring: which stacks/context get built, and the loud warning when a caller passes an extra secret
the Backend stack doesn't support.
"""

from __future__ import annotations

import strandly_harness.serve.provisioning as prov


def _stub_cdk(monkeypatch, tmp_path, capture: dict):
    """Make provision() runnable offline: fake infra dir, fake cdk, capture the subprocess cmd."""
    monkeypatch.setattr(prov, "_find_infra_dir", lambda: tmp_path)
    monkeypatch.setattr(prov, "_cdk_command", lambda: ["cdk"])

    class _Result:
        returncode = 0

    def fake_run(cmd, *a, **k):
        capture["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(prov.subprocess, "run", fake_run)


def test_github_token_mapped_to_context(monkeypatch, tmp_path):
    capture: dict = {}
    _stub_cdk(monkeypatch, tmp_path, capture)
    prov.provision(
        env="dev", region="us-west-2", extra_secrets={"STRANDLY_GITHUB_TOKEN": "ghp_x"}
    )
    assert "github_token=ghp_x" in capture["cmd"]


def test_unsupported_extra_secret_warns_and_is_not_forwarded(monkeypatch, tmp_path, capsys):
    capture: dict = {}
    _stub_cdk(monkeypatch, tmp_path, capture)
    prov.provision(
        env="dev", region="us-west-2", extra_secrets={"STRANDLY_SOMETHING_ELSE": "v"}
    )
    err = capsys.readouterr().err
    assert "WARNING" in err and "STRANDLY_SOMETHING_ELSE" in err
    # The unsupported key must not be silently smuggled into the cdk context.
    assert not any("STRANDLY_SOMETHING_ELSE" in tok or tok == "v" for tok in capture["cmd"])
