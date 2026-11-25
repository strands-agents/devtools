"""The advisory person identity: cache, GitHub verification (faked) and the request header."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

import pytest
from _home_isolation import _isolated_home  # noqa: F401  (autouse: tmp HOME)

from strandly_mcp import dashboard, identity
from strandly_mcp.auth import auth_path

_AUTH = {
    "api_url": "https://api.example.com",
    "cognito_domain": "https://pool.example.com",
    "client_id": "c-1",
    "access_token": "at-1",
    "refresh_token": "rt-1",
    "expires_at": 9e12,
}
_NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


class _Http:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, method, url, *, headers=None, data=None, **kw):
        self.requests.append({"method": method, "url": url, "headers": headers or {}, "data": data})
        status, body = self.responses.pop(0)
        return status, json.dumps(body).encode()


def test_identity_lives_next_to_the_login_cache(tmp_path):
    assert identity.identity_path() == auth_path().with_name("identity.json")
    assert identity.identity_path(tmp_path / "clients/x/auth.json") == tmp_path / "clients/x/identity.json"


def test_save_load_describe_and_clear(tmp_path):
    path = tmp_path / "identity.json"
    assert identity.load_identity(path) is None and identity.describe(None) == "not set"
    record = identity.save_identity("Octo-Cat", identity.VERIFIED_GH, path, now=_NOW)
    assert record == {"github_login": "Octo-Cat", "verified": "gh", "at": "2026-11-15T12:00:00Z"}
    assert identity.load_identity(path) == record
    assert identity.describe(record) == "@Octo-Cat (verified via gh 2026-11-15)"
    manual = identity.save_identity("octocat", identity.VERIFIED_MANUAL, path)
    assert identity.describe(manual) == "@octocat (set by hand)"
    assert identity.person_header(manual) == "github:octocat"
    assert identity.clear_identity(path) and not identity.clear_identity(path)


@pytest.mark.parametrize("login", ["", "-x", "x-", "a--b", "a" * 40, "a b", "a\nb", "github:a"])
def test_save_rejects_what_github_would(tmp_path, login):
    with pytest.raises(identity.IdentityError):
        identity.save_identity(login, identity.VERIFIED_MANUAL, tmp_path / "identity.json")


def test_a_tampered_cache_sends_no_header(tmp_path):
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"github_login": "evil\r\nX-Other: 1", "verified": "gh"}))
    assert identity.load_identity(path) is None
    path.write_text("not json")
    assert identity.load_identity(path) is None


def test_github_token_prefers_gh_then_the_environment():
    def gh(token, code=0):
        return lambda *a, **k: subprocess.CompletedProcess(a, code, stdout=token, stderr="")

    assert identity.github_token(gh("gho_x\n"), {"GITHUB_TOKEN": "ghp_env"}) == ("gho_x", "gh auth token")
    assert identity.github_token(gh("", 1), {"GITHUB_TOKEN": "ghp_env"}) == ("ghp_env", "$GITHUB_TOKEN")

    def missing(*a, **k):
        raise FileNotFoundError("gh")

    assert identity.github_token(missing, {}) is None


def test_verify_asks_github_only_and_never_stores_the_token(tmp_path):
    path = tmp_path / "identity.json"
    http = _Http((200, {"login": "OctoCat", "id": 1}))
    record = identity.verify(path, token_source=lambda: ("gho_SECRET", "gh auth token"), request=http, now=_NOW)
    assert record["github_login"] == "OctoCat" and record["verified"] == "gh"
    (call,) = http.requests
    assert call["method"] == "GET" and call["url"] == "https://api.github.com/user"
    assert call["headers"]["Authorization"] == "Bearer gho_SECRET"
    assert "SECRET" not in path.read_text()


def test_verify_without_a_token_or_with_a_refused_one(tmp_path):
    with pytest.raises(identity.IdentityError, match="no GitHub token"):
        identity.verify(tmp_path / "i.json", token_source=lambda: None)
    with pytest.raises(identity.IdentityError, match="returned 401"):
        identity.verify(tmp_path / "i.json", token_source=lambda: ("t", "x"), request=_Http((401, {})))
    assert not (tmp_path / "i.json").exists()


def test_dashboard_requests_carry_the_cached_person(tmp_path):
    auth_file = tmp_path / "auth.json"
    identity.save_identity("octocat", identity.VERIFIED_MANUAL, identity.identity_path(auth_file))
    http = _Http((200, {"sessions": []}), (200, {"status": "accepted", "taskId": "t"}))
    client = dashboard.DashboardClient(_AUTH, request=http, auth_file=auth_file)
    client.sessions()
    client.launch("cli-1", "hi")
    assert [r["headers"]["X-Strandly-Person"] for r in http.requests] == ["github:octocat"] * 2
    assert all("Bearer at-1" == r["headers"]["Authorization"] for r in http.requests)


def test_no_identity_no_header_and_an_explicit_none_wins(tmp_path):
    http = _Http((200, {"sessions": []}), (200, {"sessions": []}))
    dashboard.DashboardClient(_AUTH, request=http, auth_file=tmp_path / "auth.json").sessions()
    identity.save_identity("octocat", identity.VERIFIED_MANUAL, tmp_path / "identity.json")
    dashboard.DashboardClient(_AUTH, request=http, auth_file=tmp_path / "auth.json", person=None).sessions()
    assert all(identity.PERSON_HEADER not in r["headers"] for r in http.requests)


def test_whoami_reads_the_probe_and_tolerates_an_older_api(tmp_path):
    seen = {"github": "octocat", "person": {"person_id": "octo", "name": "Octo"}}
    http = _Http((200, seen), (400, {"error": "poll requires 'task_id' and 'session_id'"}))
    client = dashboard.DashboardClient(_AUTH, request=http, person="github:octocat")
    assert client.whoami() == seen
    assert "whoami=1" in http.requests[0]["url"]
    assert client.whoami() is None
