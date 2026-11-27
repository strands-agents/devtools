from __future__ import annotations

import sys
import types

import pytest
from _home_isolation import _isolated_home  # noqa: F401  (autouse: tmp HOME)

from strandly_mcp import cli


def test_help_lists_the_three_commands(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "login" in out and "logout" in out and "serve" in out


def test_version(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert "strandly-mcp 0.1.0" in capsys.readouterr().out


def test_requires_a_command():
    with pytest.raises(SystemExit):
        cli.main([])


def test_login_routes_to_the_auth_command(monkeypatch):
    import strandly_mcp.auth as auth_mod

    captured = {}
    monkeypatch.setattr(auth_mod, "login_command", lambda **kw: captured.update(kw) or 0)
    assert cli.main(["login", "--portal-url", "https://portal.example.com"]) == 0
    assert captured["portal_url"] == "https://portal.example.com" and captured["api_url"] is None


def test_logout_routes_to_the_auth_command(monkeypatch):
    import strandly_mcp.auth as auth_mod

    monkeypatch.setattr(auth_mod, "logout_command", lambda **kw: 0)
    assert cli.main(["logout"]) == 0


def test_serve_requires_a_login(monkeypatch, capsys):
    import strandly_mcp.auth as auth_mod

    monkeypatch.setattr(auth_mod, "load_auth", lambda path=None: None)
    assert cli.main(["serve"]) == 1
    err = capsys.readouterr().err
    assert "strandly-mcp login" in err and "--agentcore" in err


def test_serve_starts_the_dashboard_proxy(monkeypatch):
    import strandly_mcp.auth as auth_mod
    import strandly_mcp.server as server_mod

    session = {"api_url": "https://api.example.com", "access_token": "at"}
    captured = {}
    monkeypatch.setattr(auth_mod, "load_auth", lambda path=None: session)
    monkeypatch.setattr(server_mod, "serve_mcp_dashboard_proxy", lambda auth: captured.update(auth=auth))
    assert cli.main(["serve"]) == 0
    assert captured["auth"] is session


def test_serve_agentcore_starts_the_sigv4_proxy(monkeypatch):
    import strandly_mcp.server as server_mod

    captured = {}
    monkeypatch.setitem(sys.modules, "boto3", types.ModuleType("boto3"))
    monkeypatch.setattr(server_mod, "serve_mcp_proxy", lambda arn, region: captured.update(arn=arn, region=region))
    assert cli.main(["serve", "--agentcore", "--runtime-arn", "arn:rt", "--region", "us-west-2"]) == 0
    assert captured == {"arn": "arn:rt", "region": "us-west-2"}


def test_serve_agentcore_requires_a_runtime_arn(monkeypatch, capsys):
    monkeypatch.delenv("STRANDLY_RUNTIME_ARN", raising=False)
    import strandly_mcp.agentcore as ac

    monkeypatch.setattr(ac, "_recorded", lambda: {})
    assert cli.main(["serve", "--agentcore", "--region", "us-west-2"]) == 1
    assert "no runtime ARN" in capsys.readouterr().err


def test_serve_agentcore_without_boto3_names_the_extra(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "boto3", None)  # makes `import boto3` raise ImportError
    assert cli.main(["serve", "--agentcore", "--runtime-arn", "arn:rt", "--region", "us-west-2"]) == 1
    assert "strandly-mcp[aws]" in capsys.readouterr().err
