"""The GitHub Actions ingress (``dashboard/api/actions.py``): claim rules, sessions and routing.

Hermetic: the handler module is imported from ``dashboard/api`` like the read API's tests, and the
runtime invoker is a fake. API Gateway's JWT authorizer (signature, expiry, issuer, audience) is
covered by the infra tests; these re-check what the Lambda decides from the verified claims.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_API_DIR = Path(__file__).resolve().parents[2] / "dashboard" / "api"
sys.path.insert(0, str(_API_DIR))
actions = importlib.import_module("actions")
handler = importlib.import_module("handler")

POLICY = actions.Policy(repositories=("strands-agents/*", "strands-labs/*"))
CLAIMS: dict[str, Any] = {
    "iss": "https://token.actions.githubusercontent.com",
    "aud": "strandly",
    "sub": "repo:strands-agents/harness-sdk:ref:refs/heads/main",
    "repository": "strands-agents/harness-sdk",
    "repository_id": "1000001",
    "repository_owner": "strands-agents",
    "repository_owner_id": "2000002",
    "ref": "refs/heads/main",
    "ref_type": "branch",
    "workflow": "Triage",
    "workflow_ref": "strands-agents/harness-sdk/.github/workflows/triage.yml@refs/heads/main",
    "job_workflow_ref": "strands-agents/harness-sdk/.github/workflows/triage.yml@refs/heads/main",
    "event_name": "issues",
    "head_ref": "",
    "base_ref": "",
    "run_id": "987654321",
    "run_attempt": "2",
    "actor": "octocat",
    "actor_id": "583231",
    "sha": "0123abc",
}


class FakeInvoker:
    def __init__(self, launch: dict[str, Any] | None = None, poll: dict[str, Any] | None = None,
                 starting: bool = False):
        self.calls: list[tuple[str, Any]] = []
        self._launch = launch or {"status": "accepted", "taskId": "t-1"}
        self._poll = poll or {"status": "completed", "result": "done"}
        self._starting = starting

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("invoke", payload))
        return dict(self._launch)

    def poll(self, session_id: str, task_id: str, since: int | None = None, *,
             issued_only: bool = False) -> dict[str, Any]:
        self.calls.append(("poll", (session_id, task_id)))
        self.issued_only = issued_only
        if self._starting:
            raise handler.RuntimeStarting(session_id)
        return dict(self._poll)


def _evt(method: str = "POST", body: Any = None, query: dict | None = None,
         claims: dict | None = None, path: str = "/api/actions/invoke") -> dict[str, Any]:
    return {
        "rawPath": path,
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {"jwt": {"claims": CLAIMS if claims is None else claims, "scopes": None}},
        },
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
    }


# ---- claim rules -----------------------------------------------------------------------------


def test_a_default_branch_workflow_in_an_allowed_org_is_admitted():
    caller = actions.verify_claims(CLAIMS, POLICY)
    assert caller.repository == "strands-agents/harness-sdk"
    assert caller.workflow == ".github/workflows/triage.yml"
    assert caller.trigger_source == "github-actions:strands-agents/harness-sdk/triage.yml"
    assert (caller.run_id, caller.run_attempt, caller.actor) == ("987654321", "2", "octocat")
    labs = {**CLAIMS, "repository": "strands-labs/x", "repository_owner": "strands-labs",
            "workflow_ref": "strands-labs/x/.github/workflows/a.yml@refs/heads/main"}
    assert actions.verify_claims(labs, POLICY).repository == "strands-labs/x"


@pytest.mark.parametrize("event_name", ["issues", "issue_comment", "workflow_dispatch", "schedule",
                                        "push", "discussion", "workflow_run", "release"])
def test_default_branch_events_are_admitted(event_name):
    assert actions.verify_claims({**CLAIMS, "event_name": event_name}, POLICY).event_name == event_name


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        # Wrong issuer or audience (API Gateway rejects these first; the Lambda re-checks).
        ({"iss": "https://accounts.google.com"}, "issuer"),
        ({"iss": None}, "issuer"),
        ({"aud": "sts.amazonaws.com"}, "audience"),
        ({"aud": "[sts.amazonaws.com other]"}, "audience"),
        ({"aud": None}, "audience"),
        # Repository not allowed.
        ({"repository": "someone/harness-sdk", "repository_owner": "someone",
          "workflow_ref": "someone/harness-sdk/.github/workflows/triage.yml@refs/heads/main"}, "not allowed"),
        ({"repository": "strands-agents-evil/x", "repository_owner": "strands-agents-evil",
          "workflow_ref": "strands-agents-evil/x/.github/workflows/t.yml@refs/heads/main"}, "not allowed"),
        # Malformed identity claims.
        ({"repository": "strands-agents"}, "repository"),
        ({"repository_owner": "someone"}, "repository"),
        ({"repository_id": ""}, "repository_id"),
        ({"run_id": "abc"}, "run_id"),
        ({"run_attempt": None}, "run_attempt"),
        ({"actor_id": "0"}, "actor_id"),
        ({"actor": "bad actor!"}, "actor"),
        # Fork and pull request triggers: pull_request_target runs base code for a fork's PR, and
        # no claim tells a fork apart, so pull request events are denied by name.
        ({"event_name": "pull_request_target", "head_ref": "fork-branch"}, "pull request"),
        ({"event_name": "pull_request_target", "head_ref": ""}, "pull request"),
        ({"event_name": "pull_request", "head_ref": "feature", "ref": "refs/pull/5/merge"}, "pull request"),
        ({"event_name": "pull_request_review", "head_ref": "feature"}, "pull request"),
        ({"event_name": ""}, "event"),
        # Non-default refs.
        ({"ref": "refs/heads/feature"}, "refs/heads/main"),
        ({"ref": "refs/tags/v1"}, "refs/heads/main"),
        ({"ref": "refs/pull/5/merge", "event_name": "workflow_dispatch"}, "refs/heads/main"),
        ({"ref": ""}, "refs/heads/main"),
        # The workflow file must come from the same repository's default branch.
        ({"workflow_ref": "strands-agents/harness-sdk/.github/workflows/triage.yml@refs/heads/dev"},
         "workflow must come"),
        ({"workflow_ref": "strands-agents/other/.github/workflows/triage.yml@refs/heads/main"},
         "workflow must come"),
        ({"workflow_ref": "strands-agents/harness-sdk/x.yml@refs/heads/main"}, "workflow_ref"),
        ({"workflow_ref": "strands-agents/harness-sdk/.github/workflows/../x.yml@refs/heads/main"},
         "workflow_ref"),
        ({"workflow_ref": ""}, "workflow must come"),
    ],
)
def test_deny_matrix(change, reason):
    with pytest.raises(actions.Denied, match=reason):
        actions.verify_claims({**CLAIMS, **change}, POLICY)


def test_an_unconfigured_policy_denies_everyone():
    empty = actions.Policy.from_env({})
    with pytest.raises(actions.Denied, match="audience"):
        actions.verify_claims(CLAIMS, empty)
    with pytest.raises(actions.Denied, match="not allowed"):
        actions.verify_claims(CLAIMS, actions.Policy.from_env({"STRANDLY_ACTIONS_AUDIENCE": "strandly",
                                                                "STRANDLY_ACTIONS_DEFAULT_BRANCH": "main"}))


def test_policy_from_env_and_custom_rules():
    policy = actions.Policy.from_env({
        "STRANDLY_ACTIONS_AUDIENCE": "my-agent",
        "STRANDLY_ACTIONS_REPOSITORIES": " example-org/app ,example-org/lib-* ",
        "STRANDLY_ACTIONS_WORKFLOWS": "example-org/*/.github/workflows/strandly-*.yml",
        "STRANDLY_ACTIONS_DEFAULT_BRANCH": "trunk",
    })
    assert policy.repositories == ("example-org/app", "example-org/lib-*")
    claims = {**CLAIMS, "aud": "my-agent", "repository": "Example-Org/lib-core",
              "repository_owner": "Example-Org", "ref": "refs/heads/trunk",
              "workflow_ref": "Example-Org/lib-core/.github/workflows/strandly-triage.yml@refs/heads/trunk"}
    assert actions.verify_claims(claims, policy).repository == "Example-Org/lib-core"
    with pytest.raises(actions.Denied, match="workflow .* is not allowed"):
        actions.verify_claims(
            {**claims, "workflow_ref": "Example-Org/lib-core/.github/workflows/ci.yml@refs/heads/trunk"},
            policy,
        )
    with pytest.raises(actions.Denied, match="refs/heads/trunk"):
        actions.verify_claims({**claims, "ref": "refs/heads/main"}, policy)
    with pytest.raises(actions.Denied, match="audience"):
        actions.verify_claims({**claims, "aud": "strandly"}, policy)


# ---- sessions --------------------------------------------------------------------------------


CALLER = actions.verify_claims(CLAIMS, POLICY)


@pytest.mark.parametrize(
    ("raw", "session", "url"),
    [
        (None, "gha-1000001-run-987654321", None),
        ("", "gha-1000001-run-987654321", None),
        ("issue:12", "gh-strands-agents-harness-sdk-e0b64059-issue-12",
         "https://github.com/strands-agents/harness-sdk/issues/12"),
        ("pr:7", "gh-strands-agents-harness-sdk-e0b64059-pr-7", "https://github.com/strands-agents/harness-sdk/pull/7"),
        ("discussion:3", "gh-strands-agents-harness-sdk-e0b64059-disc-3",
         "https://github.com/strands-agents/harness-sdk/discussions/3"),
        ("port-harness-sdk-42", "gha-1000001-port-harness-sdk-42", None),
    ],
)
def test_sessions_resolve_inside_the_callers_repository(raw, session, url):
    assert actions.resolve_session(CALLER, raw) == (session, url)
    assert actions.owns_session(CALLER, session)


@pytest.mark.parametrize("raw", ["issue:0", "issue:-1", "pr:x", "gh-other-repo-a811ea4c-issue-1/../x", "a b",
                                 "strands-agents/other#1", "x" * 49, 12])
def test_bad_session_values_are_rejected(raw):
    with pytest.raises(ValueError):
        actions.resolve_session(CALLER, raw)


@pytest.mark.parametrize("session", [
    "gh-strands-agents-other-2a878d70-issue-1",
    "gh-strands-agents-harness-sdk-x-7bddb793-issue-1",  # a sibling repository named harness-sdk-x
    "gh-strands-agents-harness-sdk-x-issue-1",
    "gh-strands-agents-harness-sdk-issue-12",  # the name-only format, retired
    "gh-strands-agents-harness-sdk-00000000-issue-12",  # another repository's digest
    "gh-strands-agents-harness-sdk-E0B64059-issue-12",
    "gh-strands-agents-harness-sdk-e0b64059-issue-012",
    "gh-strands-agents-harness-sdk-e0b64059-issue-12x",
    "gh-strands-agents-harness-sdk-e0b64059-987654321",
    "gh-strands-agents-harness-sdk-987654321",
    "gha-1000002-run-1",
    "gha-1000001-",
    "slack-T1-D1-1.2",
    "acp-0123",
])
def test_other_sessions_are_not_owned(session):
    assert not actions.owns_session(CALLER, session)


def _caller(repository: str, repository_id: str = "1000001") -> Any:
    owner = repository.split("/")[0]
    claims = {**CLAIMS, "repository": repository, "repository_owner": owner, "repository_id": repository_id,
              "workflow_ref": f"{repository}/.github/workflows/triage.yml@refs/heads/main"}
    return actions.verify_claims(claims, actions.Policy(repositories=("*",)))


@pytest.mark.parametrize(("ours", "theirs"), [
    ("acme/labs-tool", "acme-labs/tool"),
    ("strands-agents/sdk-python", "strands-agents-sdk/python"),
])
def test_repositories_whose_names_read_the_same_never_share_a_session(ours, theirs):
    mine, other = _caller(ours), _caller(theirs, "1000002")
    sid, _ = actions.resolve_session(mine, "issue:12")
    their_sid, _ = actions.resolve_session(other, "issue:12")
    assert sid != their_sid
    assert actions.owns_session(mine, sid) and actions.owns_session(other, their_sid)
    assert not actions.owns_session(mine, their_sid)
    assert not actions.owns_session(other, sid)


def test_a_repository_keeps_one_session_however_its_name_is_cased():
    cased, lower = _caller("Acme/Labs-Tool"), _caller("acme/labs-tool")
    sid, _ = actions.resolve_session(cased, "issue:12")
    assert sid == actions.resolve_session(lower, "issue:12")[0] == "gh-acme-labs-tool-7d6b2707-issue-12"
    assert actions.owns_session(cased, sid) and actions.owns_session(lower, sid)
    assert not actions.owns_session(cased, "gh-Acme-Labs-Tool-7d6b2707-issue-12")


def test_item_numbers_are_matched_exactly():
    one, _ = actions.resolve_session(CALLER, "issue:1")
    twelve, _ = actions.resolve_session(CALLER, "issue:12")
    assert one != twelve and one == "gh-strands-agents-harness-sdk-e0b64059-issue-1"
    assert actions.owns_session(CALLER, one) and actions.owns_session(CALLER, twelve)
    assert not actions.owns_session(CALLER, one + "2x")


def test_a_repository_id_prefix_never_claims_a_longer_ids_sessions():
    assert not actions.owns_session(_caller("strands-agents/harness-sdk", "12"), "gha-123-run-1")
    assert actions.owns_session(_caller("strands-agents/harness-sdk", "12"), "gha-12-run-1")


# ---- routing ---------------------------------------------------------------------------------


def test_launch_builds_a_verified_payload():
    invoker = FakeInvoker()
    status, body = actions.route(_evt(body={"prompt": "Triage this issue.", "session": "issue:12"}),
                                 invoker, POLICY)
    assert status == 200
    assert body == {"status": "accepted", "taskId": "t-1", "sessionId": "gh-strands-agents-harness-sdk-e0b64059-issue-12"}
    (kind, payload), = invoker.calls
    assert kind == "invoke"
    run = {
        "issuer": "https://token.actions.githubusercontent.com",
        "repository": "strands-agents/harness-sdk", "repositoryId": "1000001",
        "workflow": ".github/workflows/triage.yml", "runId": "987654321", "runAttempt": "2",
        "actor": "octocat", "actorId": "583231", "eventName": "issues", "sha": "0123abc",
    }
    url = "https://github.com/strands-agents/harness-sdk/issues/12"
    assert payload == {
        "prompt": "Triage this issue.",
        "sessionId": "gh-strands-agents-harness-sdk-e0b64059-issue-12",
        "triggerSource": "github-actions:strands-agents/harness-sdk/triage.yml",
        "triggerId": "gha-1000001-987654321-2",
        "githubActions": run,
        "githubUrl": url,
        "githubContext": {"repository": "strands-agents/harness-sdk", "event": {"issue": {"html_url": url}}},
        "invocationContext": {
            "githubActions": {**run, "runUrl": "https://github.com/strands-agents/harness-sdk/actions/runs/987654321"},
            "item": url,
        },
    }


def test_launch_without_a_session_is_per_run_and_carries_no_item():
    invoker = FakeInvoker()
    status, body = actions.route(_evt(body={"prompt": "Summarize the week."}), invoker, POLICY)
    assert status == 200 and body["sessionId"] == "gha-1000001-run-987654321"
    payload = invoker.calls[0][1]
    assert "githubUrl" not in payload and "githubContext" not in payload
    assert "item" not in payload["invocationContext"]


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({}, "prompt required"),
        ({"prompt": "   "}, "prompt required"),
        ({"prompt": 5}, "prompt required"),
        ({"prompt": "x" * 32_001}, "exceeds"),
        ({"prompt": "hi", "session": "a b"}, "session must be"),
        # Callers send text only: no trigger, identity, mode or context overrides.
        ({"prompt": "hi", "triggerSource": "portal:chat"}, "unsupported fields: triggerSource"),
        ({"prompt": "hi", "sessionId": "gh-x-y-bd3c9047-issue-1"}, "unsupported fields: sessionId"),
        ({"prompt": "hi", "githubActions": {}}, "unsupported fields"),
        ({"prompt": "hi", "mode": "post"}, "unsupported fields"),
    ],
)
def test_launch_rejects_anything_but_prompt_and_session(body, error):
    invoker = FakeInvoker()
    status, out = actions.route(_evt(body=body), invoker, POLICY)
    assert status == 400 and error in out["error"]
    assert invoker.calls == []


def test_denied_claims_never_reach_the_runtime():
    invoker = FakeInvoker()
    status, out = actions.route(_evt(body={"prompt": "hi"}, claims={**CLAIMS, "ref": "refs/heads/x"}),
                                invoker, POLICY)
    assert status == 403 and "refs/heads/main" in out["error"]
    status, _ = actions.route(_evt(body={"prompt": "hi"}, claims={}), invoker, POLICY)
    assert status == 403
    status, _ = actions.route(_evt("GET", query={"session_id": "gha-1000001-run-1", "task_id": "t"},
                                   claims={**CLAIMS, "event_name": "pull_request_target"}), invoker, POLICY)
    assert status == 403
    assert invoker.calls == []


def test_poll_reads_an_owned_session():
    invoker = FakeInvoker()
    status, body = actions.route(
        _evt("GET", query={"session_id": "gh-strands-agents-harness-sdk-e0b64059-issue-12", "task_id": "t-1"}),
        invoker, POLICY,
    )
    assert (status, body) == (200, {"status": "completed", "result": "done"})
    assert invoker.calls == [("poll", ("gh-strands-agents-harness-sdk-e0b64059-issue-12", "t-1"))]
    assert invoker.issued_only is True


def test_poll_of_a_task_the_runtime_did_not_issue_for_the_session_is_not_found():
    invoker = FakeInvoker(poll={"taskId": "made-up", "status": "unknown", handler.POLL_ISSUED_KEY: False})
    status, body = actions.route(
        _evt("GET", query={"session_id": "gh-strands-agents-harness-sdk-e0b64059-issue-12", "task_id": "made-up"}),
        invoker, POLICY,
    )
    assert status == 404 and "was not issued" in body["error"]
    assert "result" not in body


def test_poll_of_another_repositorys_session_is_forbidden():
    invoker = FakeInvoker()
    status, body = actions.route(
        _evt("GET", query={"session_id": "gh-strands-agents-other-2a878d70-issue-12", "task_id": "t-1"}), invoker, POLICY,
    )
    assert status == 403 and "does not belong" in body["error"]
    assert invoker.calls == []


@pytest.mark.parametrize("query", [None, {"session_id": "gha-1000001-run-1"}, {"task_id": "t"}])
def test_poll_requires_session_and_task(query):
    status, _ = actions.route(_evt("GET", query=query), FakeInvoker(), POLICY)
    assert status == 400


def test_a_cold_poll_is_retryable():
    status, body = actions.route(
        _evt("GET", query={"session_id": "gha-1000001-run-987654321", "task_id": "t"}),
        FakeInvoker(starting=True), POLICY,
    )
    assert status == 504 and body["status"] == "starting" and body["retryable"] is True


def test_a_cold_launch_reports_starting_with_the_session():
    invoker = FakeInvoker(launch={"status": "starting", "sessionId": "gha-1000001-run-987654321"})
    status, body = actions.route(_evt(body={"prompt": "hi"}), invoker, POLICY)
    assert status == 200 and body == {"status": "starting", "sessionId": "gha-1000001-run-987654321"}


def test_routing_edges():
    assert actions.route(_evt(path="/api/chat"), FakeInvoker(), POLICY)[0] == 404
    assert actions.route(_evt("DELETE"), FakeInvoker(), POLICY)[0] == 405
    assert actions.route(_evt(body={"prompt": "hi"}), None, POLICY)[0] == 503
    # The read API never serves the Actions route, whatever authorizer context it carries.
    assert handler.route(_evt(body={"prompt": "hi"}), None, FakeInvoker())[0] == 404


def test_lambda_handler_reads_policy_from_env(monkeypatch):
    monkeypatch.delenv("STRANDLY_RUNTIME_ARN", raising=False)
    monkeypatch.setenv("STRANDLY_ACTIONS_AUDIENCE", "strandly")
    monkeypatch.setenv("STRANDLY_ACTIONS_REPOSITORIES", "strands-agents/*")
    monkeypatch.setenv("STRANDLY_ACTIONS_DEFAULT_BRANCH", "main")
    out = actions.lambda_handler(_evt(body={"prompt": "hi"}))
    assert out["statusCode"] == 503  # admitted, but no runtime configured
    monkeypatch.setenv("STRANDLY_ACTIONS_REPOSITORIES", "example-org/*")
    out = actions.lambda_handler(_evt(body={"prompt": "hi"}))
    assert out["statusCode"] == 403 and json.loads(out["body"])["error"].startswith("repository")
    assert out["headers"]["cache-control"] == "no-store"
