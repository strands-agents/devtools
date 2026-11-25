"""The client commands, as a generated CLI and as ``strandly`` run them."""

from __future__ import annotations

import ast
import json
import sys
import time
from pathlib import Path

import pytest

import strandly_mcp.auth as mcp_auth
import strandly_mcp.dashboard as mcp_dashboard
import strandly_mcp.identity as mcp_identity
import strandly_mcp.server as mcp_server
from strandly.serve.cli.main import main as strandly_main
from strandly_cli import chat as cli_chat
from strandly_cli import commands
from strandly_cli.profile import ClientProfile

ROOT = Path(__file__).resolve().parents[2]
ARN = "arn:aws:bedrock-agentcore:us-west-2:111111111111:runtime/example_agent-AbC123"
PROFILE = ClientProfile.from_dict(
    {"command": "strandly-example", "display_name": "Example agent", "portal_url": "https://portal.example.com",
     "runtime_arn": ARN, "default_org": "example-org"}
)
LOGIN = {"api_url": "https://api.example.com", "cognito_domain": "https://d.example.com", "client_id": "abc",
         "access_token": "SECRET-ACCESS", "refresh_token": "SECRET-REFRESH", "expires_at": time.time() + 600}


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(commands.MCP_LABEL_ENV, raising=False)
    monkeypatch.delenv(commands.MCP_REPOSITORIES_ENV, raising=False)
    # Never read the developer's real `gh` token or call GitHub from a test.
    monkeypatch.setattr(mcp_identity, "github_token", lambda *a, **k: None)
    original_hint = mcp_auth.LOGIN_HINT
    yield tmp_path
    mcp_auth.LOGIN_HINT = original_hint


def generated(*argv):
    return commands.main(list(argv), profile=PROFILE)


def logged_in():
    mcp_auth.save_auth(dict(LOGIN), PROFILE.auth_file())


class FakeDashboard:
    def __init__(self, auth, *, auth_file=None, **_):
        self.auth, self.auth_file = auth, auth_file
        FakeDashboard.last = self

    def sessions(self):
        return [{"session_id": "cli-1", "description": "a  task\nwith lines", "last_status": "completed"}]

    def session(self, session_id):
        return {"session_id": session_id, "turns": []} if session_id == "cli-1" else None


def test_login_uses_only_the_baked_portal_and_the_cli_cache(monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp_auth, "login_command", lambda **kw: seen.update(kw) or 0)
    monkeypatch.setenv("STRANDLY_API_URL", "https://attacker.example.com")
    assert generated("login") == 0
    assert seen == {"portal_url": "https://portal.example.com", "path": PROFILE.auth_file(), "default_portal_url": None}


def test_login_with_baked_direct_coordinates_skips_discovery(monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp_auth, "login_command", lambda **kw: seen.update(kw) or 0)
    profile = ClientProfile.from_dict(
        {"command": "strandly-example", "display_name": "E", "api_url": "https://api.example.com",
         "cognito_domain": "https://d.example.com", "client_id": "abc"}
    )
    assert commands.main(["login"], profile=profile) == 0
    assert seen["api_url"] == "https://api.example.com" and "portal_url" not in seen


def test_login_takes_no_coordinate_flags():
    with pytest.raises(SystemExit):
        generated("login", "--portal-url", "https://other.example.com")


def test_logout_clears_only_this_cli(monkeypatch, _home):
    logged_in()
    shared = _home / ".strandly" / "auth.json"
    mcp_auth.save_auth(dict(LOGIN), shared)
    monkeypatch.setattr(mcp_auth, "_post_form", lambda *a, **k: (200, b"{}"))
    assert generated("logout") == 0
    assert not PROFILE.auth_file().exists() and shared.exists()


def test_whoami_reports_the_login_and_never_prints_tokens(capsys):
    assert generated("whoami") == 1
    assert "run `strandly-example login`" in capsys.readouterr().err
    logged_in()
    assert generated("whoami") == 0
    out = capsys.readouterr().out
    shown = dict(line.strip().split(": ", 1) for line in out.splitlines())
    assert shown["deployment"] == "Example agent" and shown["api"] == LOGIN["api_url"]
    assert "SECRET" not in out


def test_sessions_list_and_show(monkeypatch, capsys):
    logged_in()
    monkeypatch.setattr(mcp_dashboard, "DashboardClient", FakeDashboard)
    assert generated("sessions") == 0
    assert capsys.readouterr().out == "cli-1\tcompleted\t-\ta task with lines\n"
    assert FakeDashboard.last.auth_file == PROFILE.auth_file()
    assert generated("sessions", "--json") == 0
    assert json.loads(capsys.readouterr().out)[0]["session"] == "cli-1"
    assert generated("sessions", "cli-1") == 0
    assert json.loads(capsys.readouterr().out)["session"] == "cli-1"
    assert generated("sessions", "missing") == 1


def test_sessions_prefers_the_session_title(monkeypatch, capsys):
    logged_in()
    rows = [{"session_id": "cli-2", "title": "Release prep", "description": "long first prompt", "last_status": None}]
    monkeypatch.setattr(mcp_dashboard, "DashboardClient", FakeDashboard)
    monkeypatch.setattr(FakeDashboard, "sessions", lambda self: rows)
    assert generated("sessions") == 0
    assert capsys.readouterr().out == "cli-2\t-\t-\tRelease prep\n"


def test_mcp_serves_the_dashboard_proxy_with_this_cli_cache(monkeypatch):
    logged_in()
    served = {}
    monkeypatch.setattr(mcp_dashboard, "DashboardClient", FakeDashboard)
    monkeypatch.setattr(
        mcp_server, "build_dashboard_proxy_server",
        lambda client: type("S", (), {"run": lambda self: served.update(client=client)})(),
    )
    assert generated("mcp") == 0
    assert served["client"].auth_file == PROFILE.auth_file()
    import os

    assert os.environ[commands.MCP_LABEL_ENV] == "Example agent"
    assert os.environ[commands.MCP_REPOSITORIES_ENV] == "example-org/*"


def test_mcp_requires_a_login(capsys):
    assert generated("mcp") == 1
    assert "strandly-example login" in capsys.readouterr().err


def test_mcp_agentcore_uses_the_baked_runtime(monkeypatch):
    pytest.importorskip("boto3")
    served = {}
    monkeypatch.setattr(mcp_server, "serve_mcp_proxy", lambda arn, region: served.update(arn=arn, region=region))
    assert generated("mcp", "--agentcore") == 0
    assert served == {"arn": ARN, "region": "us-west-2"}


def test_acp_passes_this_cli_cache_and_login_command(monkeypatch):
    from strandly_cli import acp

    seen = {}
    monkeypatch.setattr(acp, "main", lambda auth_file, login: seen.update(auth_file=auth_file, login=login) or 0)
    assert generated("acp") == 0
    assert seen == {"auth_file": PROFILE.auth_file(), "login": "strandly-example login"}


def test_transport_errors_name_this_cli_login():
    generated("whoami")
    assert str(mcp_auth.NotLoggedInError()) == "not logged in — run `strandly-example login`"


def test_chat_one_turn(monkeypatch, capsys):
    logged_in()
    monkeypatch.setattr(mcp_dashboard, "DashboardClient", FakeDashboard)

    async def collect(client, sid, prompt, on_activity=None):
        await on_activity("→ shell")
        return f"answer to {prompt} on {sid}"

    monkeypatch.setattr(cli_chat, "run_dashboard_collect", collect)
    assert generated("chat", "-p", "hi", "-s", "cli-1") == 0
    captured = capsys.readouterr()
    assert captured.out == "answer to hi on cli-1\n"
    assert "→ shell" in captured.err


def test_chat_repl_ends_on_exit_words():
    lines = iter(["", "quit"])
    assert cli_chat.chat_remote(object(), read_line=lambda _: next(lines)) == 0  # type: ignore[arg-type]


# ---- strandly itself runs the same commands ------------------------------------------------


def test_strandly_mcp_is_serve_mcp(monkeypatch):
    import strandly_mcp.server as proxy

    mcp_auth.save_auth(dict(LOGIN))
    served = {}
    monkeypatch.setattr(proxy, "serve_mcp_dashboard_proxy", lambda auth: served.update(auth=auth))
    assert strandly_main(["mcp"]) == 0
    assert served["auth"]["api_url"] == LOGIN["api_url"]


def test_strandly_client_commands_use_the_shared_cache(monkeypatch, capsys):
    mcp_auth.save_auth(dict(LOGIN))
    monkeypatch.setattr(mcp_dashboard, "DashboardClient", FakeDashboard)
    assert strandly_main(["whoami"]) == 0
    assert ".strandly/auth.json" in capsys.readouterr().out
    assert strandly_main(["sessions", "--json"]) == 0
    assert FakeDashboard.last.auth_file is None


def test_strandly_chat_goes_through_the_dashboard(monkeypatch):
    seen = {}
    monkeypatch.setattr(commands, "run_chat", lambda ctx, args: seen.update(ctx=ctx, args=args) or 0)
    assert strandly_main(["chat"]) == 0
    assert seen["ctx"].profile is None and seen["args"].session_id is None
    assert strandly_main(["chat", "-s", "named", "-p", "hi"]) == 0
    assert seen["args"].session_id == "named" and seen["args"].prompt == "hi"


def _github_says(monkeypatch, login, seen=None):
    """A local GitHub token whose GET /user answers ``login`` (both faked at the HTTP seam)."""
    monkeypatch.setattr(mcp_identity, "github_token", lambda *a, **k: ("gho_SECRET", "gh auth token"))
    real = mcp_identity.github_login_for

    def request(method, url, *, headers=None, **kw):
        if seen is not None:
            seen.append((url, dict(headers or {})))
        return 200, json.dumps({"login": login}).encode()

    monkeypatch.setattr(mcp_identity, "github_login_for", lambda token, **kw: real(token, request=request))


def test_identity_show_set_by_hand_and_clear(capsys, _home):
    assert generated("identity") == 0
    out = capsys.readouterr()
    assert out.out == "you: not set\n" and "strandly-example identity --verify" in out.err
    assert generated("identity", "--github", "@Octo-Cat") == 0
    out = capsys.readouterr()
    assert out.out == "you: @Octo-Cat (set by hand)\n" and "unverified" in out.err
    cached = PROFILE.auth_file().with_name("identity.json")
    assert json.loads(cached.read_text())["verified"] == "manual"
    assert not (_home / ".strandly" / "identity.json").exists()  # this CLI's own cache only
    assert generated("identity", "--github", "not a login") == 1
    assert generated("identity", "--clear") == 0 and not cached.exists()


def test_identity_verify_uses_the_local_github_token(monkeypatch, capsys):
    seen = []
    _github_says(monkeypatch, "octocat", seen)
    assert generated("identity", "--verify") == 0
    assert capsys.readouterr().out.startswith("you: @octocat (verified via gh ")
    assert [url for url, _ in seen] == ["https://api.github.com/user"]
    assert "SECRET" not in PROFILE.auth_file().with_name("identity.json").read_text()


def test_identity_verify_without_a_token_fails_and_keeps_the_identity(capsys):
    generated("identity", "--github", "octocat")
    assert generated("identity", "--verify") == 1
    assert "no GitHub token" in capsys.readouterr().err
    assert generated("identity") == 0 and "@octocat (set by hand)" in capsys.readouterr().out


def test_login_verifies_the_identity_and_never_fails_on_it(monkeypatch, capsys):
    monkeypatch.setattr(mcp_auth, "login_command", lambda **kw: 0)
    assert generated("login") == 0
    assert "no GitHub token" in capsys.readouterr().err
    _github_says(monkeypatch, "octocat")
    assert generated("login") == 0
    assert "you are @octocat (verified via gh" in capsys.readouterr().err
    monkeypatch.setattr(mcp_auth, "login_command", lambda **kw: 1)
    monkeypatch.setattr(commands, "verify_identity_after_login", lambda ctx: pytest.fail("verified after a failed login"))
    assert generated("login") == 1


def test_whoami_shows_you_and_the_mapped_person(monkeypatch, capsys):
    logged_in()
    calls = []

    class Api(FakeDashboard):
        reply: object = None

        def whoami(self):
            calls.append(self.auth_file)
            return Api.reply

    monkeypatch.setattr(mcp_dashboard, "DashboardClient", Api)
    assert generated("whoami") == 0
    assert dict(line.strip().split(": ", 1) for line in capsys.readouterr().out.splitlines())["you"] == "not set"
    assert calls == []  # no identity, nothing to map, no network
    generated("identity", "--github", "octocat")
    capsys.readouterr()
    Api.reply = {"github": "octocat", "person": {"person_id": "octo", "name": "Octo Cat", "role": "maintainer"}}
    assert generated("whoami") == 0
    shown = dict(line.strip().split(": ", 1) for line in capsys.readouterr().out.splitlines())
    assert shown["you"] == "@octocat (set by hand)"
    assert shown["person"] == "Octo Cat, maintainer (octo)"
    Api.reply = {"github": "octocat", "person": None}
    generated("whoami")
    assert "person: not in the people directory" in capsys.readouterr().out
    Api.reply = None
    generated("whoami")
    assert "does not report it" in capsys.readouterr().out
    monkeypatch.setattr(Api, "whoami", lambda self: (_ for _ in ()).throw(OSError("down")))
    assert generated("whoami") == 0
    assert "unreachable" in capsys.readouterr().out


def test_strandly_identity_and_login_use_the_shared_cache(monkeypatch, capsys, _home):
    assert strandly_main(["identity", "--github", "octocat"]) == 0
    assert json.loads((_home / ".strandly" / "identity.json").read_text())["github_login"] == "octocat"
    monkeypatch.setattr(mcp_auth, "login_command", lambda **kw: 0)
    _github_says(monkeypatch, "OctoCat")
    assert strandly_main(["login"]) == 0
    assert json.loads((_home / ".strandly" / "identity.json").read_text())["verified"] == "gh"


def test_acp_sends_the_cached_person_from_this_cli(monkeypatch, _home):
    import asyncio

    import httpx

    from strandly_cli import acp

    logged_in()
    generated("identity", "--github", "octocat")
    sent = []

    def handler(request):
        sent.append(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    api = acp.NativeAPI(PROFILE.auth_file(), "strandly-example login")
    api.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(api.request("GET", {"capabilities": "acp"})) == {"ok": True}
    assert sent[0]["x-strandly-person"] == "github:octocat"


def test_acp_policy_names_the_login_command():
    import strandly_cli.acp

    assert strandly_cli.acp.POLICY.endswith("Run strandly login separately.")


ALLOWED_IMPORTS = {"strandly_cli", "strandly_mcp", "mcp", "httpx", "acp", "boto3"}


@pytest.mark.parametrize("path", sorted((ROOT / "cli" / "src" / "strandly_cli").glob("*.py")), ids=lambda p: p.name)
def test_client_package_imports_only_the_client_stack(path):
    tree = ast.parse(path.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules.add(node.module.split(".")[0])
    foreign = sorted(m for m in modules - ALLOWED_IMPORTS - {"__future__"} if m not in sys.stdlib_module_names)
    assert not foreign, f"{path.name} imports {foreign}"
