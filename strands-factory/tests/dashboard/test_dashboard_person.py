"""The caller's person: verified Cognito sub -> bound people-directory row -> runtime payload.

Through API Gateway ``X-Strandly-Person`` is advisory: it never selects a person. A direct Lambda
invoke (no API Gateway and no authorizer context) names its person with it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from test_actions_ingress import POLICY, FakeInvoker, actions
from test_actions_ingress import _evt as _actions_evt
from test_dashboard_api import FakeReader, _evt, _receipt_event, handler
from test_dashboard_api import receipt_identity as receipt_identity

from strandly.ops import people

SUB_A = "12345678-1234-1234-1234-123456789abc"  # receipt_identity's sub, bound to octo
SUB_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"  # bound to hubot
SUB_UNBOUND = "fedcba98-7654-4321-8fed-cba987654321"
ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/pool"

ROW = {
    "person_id": {"S": "octo"},
    "name": {"S": "Octo Cat"},
    "role": {"S": "maintainer"},
    "github_login": {"S": "octocat"},
    "slack_user_id": {"S": "U0OCTO"},
    "cognito_sub": {"S": SUB_A},
}
OTHER = {
    "person_id": {"S": "hubot"},
    "name": {"S": "Hu Bot"},
    "github_login": {"S": "hubot"},
    "slack_user_id": {"S": "U0HUBOT"},
    "cognito_sub": {"S": SUB_B},
}


class FakeDynamo:
    """A low-level DynamoDB client answering the people GSI Queries from fixed rows."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, error: Exception | None = None):
        self.rows = rows if rows is not None else [ROW, OTHER]
        self.error = error
        self.queries: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        if self.error:
            raise self.error
        attr = kwargs["ExpressionAttributeNames"]["#k"]
        key = kwargs["ExpressionAttributeValues"][":v"]["S"]
        return {"Items": [r for r in self.rows if r.get(attr, {}).get("S") == key]}


@pytest.fixture(autouse=True)
def _fresh_people_cache(monkeypatch):
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "pool")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    people.reset_cache()
    yield
    people.reset_cache()


class Launches:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {"status": "accepted", "taskId": "t-1"}


@pytest.fixture
def launches(monkeypatch):
    invoker = handler.RuntimeInvoker.__new__(handler.RuntimeInvoker)
    sent = Launches()
    monkeypatch.setattr(invoker, "_invoke", sent)
    return invoker, sent


def _signed_in(event: dict[str, Any], sub: str | None) -> dict[str, Any]:
    """An API Gateway event (``apiId``), with the authorizer claims of ``sub`` unless it is None."""
    event["requestContext"]["apiId"] = "api123"
    if sub is not None:
        event["requestContext"]["authorizer"] = {
            "jwt": {"claims": {"sub": sub, "token_use": "access", "iss": ISSUER}}
        }
    return event


def _chat(header: str | None = None, *, sub: str | None = SUB_A, header_name: str = "x-strandly-person",
          direct: bool = False, **body: Any) -> dict[str, Any]:
    event = _evt("/api/chat", method="POST")
    event["body"] = json.dumps({"session_id": "portal-1", "message": "hi", "trigger_source": "portal:chat", **body})
    if header is not None:
        event["headers"] = {header_name: header}
    return event if direct else _signed_in(event, sub)


def _directory(client: FakeDynamo | None = None) -> people.PeopleDirectory:
    return people.PeopleDirectory(client or FakeDynamo(), "strandly-dev-people")


def _person(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k.startswith("person")}


OCTO_FIELDS = {"personGithub": "octocat", "personId": "octo", "personName": "Octo Cat",
               "personRole": "maintainer", "personSlackUserId": "U0OCTO"}


def test_the_bound_person_of_the_verified_sub_rides_to_the_runtime(launches):
    invoker, sent = launches
    dynamo = FakeDynamo()
    status, _ = handler.route(_chat(), None, invoker=invoker, people=_directory(dynamo))
    assert status == 200
    (payload,) = sent.payloads
    assert _person(payload) == OCTO_FIELDS
    assert payload["portalUser"] == SUB_A and payload["portalIssuer"] == ISSUER
    (query,) = dynamo.queries
    assert query["IndexName"] == "by_cognito" and query["TableName"] == "strandly-dev-people"
    assert query["ExpressionAttributeValues"] == {":v": {"S": SUB_A}}


def test_a_header_naming_another_person_never_selects_them(launches, caplog):
    invoker, sent = launches
    dynamo = FakeDynamo()
    with caplog.at_level(logging.WARNING):
        handler.route(_chat("github:hubot"), None, invoker=invoker, people=_directory(dynamo))
    assert _person(sent.payloads[0]) == OCTO_FIELDS  # sub A's person, never B
    assert all(q["IndexName"] == "by_cognito" for q in dynamo.queries)  # the login is never looked up
    assert "ignoring x-strandly-person github:hubot" in caplog.text and "bound to octo" in caplog.text


def test_a_header_naming_a_person_is_ignored_for_an_unbound_user(launches, caplog):
    invoker, sent = launches
    with caplog.at_level(logging.WARNING):
        handler.route(_chat("github:octocat", sub=SUB_UNBOUND), None, invoker=invoker, people=_directory())
    (payload,) = sent.payloads
    assert _person(payload) == {}  # team memory; the schedule tool owns jobs as portal:<sub>
    assert payload["portalUser"] == SUB_UNBOUND and payload["portalIssuer"] == ISSUER
    assert "bound to no person" in caplog.text


def test_the_own_login_in_the_header_is_not_a_mismatch(launches, caplog):
    invoker, sent = launches
    with caplog.at_level(logging.WARNING):
        handler.route(_chat("github:OctoCat", header_name="X-Strandly-Person"), None, invoker=invoker,
                      people=_directory())
    assert sent.payloads[0]["personId"] == "octo" and "ignoring" not in caplog.text


@pytest.mark.parametrize("header", [None, "", "octocat", "gitlab:octocat", "github:", "github:-bad",
                                    "github:a--b", "github:" + "a" * 40, "github:octo cat", "github:octo\ncat"])
def test_no_or_malformed_header_changes_nothing(launches, header):
    invoker, sent = launches
    handler.route(_chat(header), None, invoker=invoker, people=_directory())
    assert _person(sent.payloads[0]) == OCTO_FIELDS


def test_without_a_verified_sub_there_is_no_person(launches, monkeypatch):
    invoker, sent = launches
    dynamo = FakeDynamo()
    handler.route(_chat("github:octocat", sub=None), None, invoker=invoker, people=_directory(dynamo))
    wrong_issuer = _chat("github:octocat")
    wrong_issuer["requestContext"]["authorizer"]["jwt"]["claims"]["iss"] = "https://evil.example/pool"
    handler.route(wrong_issuer, None, invoker=invoker, people=_directory(dynamo))
    for payload in sent.payloads:
        assert _person(payload) == {} and "portalUser" not in payload
    assert dynamo.queries == []


def test_a_disabled_row_or_a_directory_error_is_no_person(launches):
    invoker, sent = launches
    disabled = dict(ROW, enabled={"BOOL": False})
    handler.route(_chat(), None, invoker=invoker, people=_directory(FakeDynamo([disabled])))
    people.reset_cache()
    handler.route(_chat(), None, invoker=invoker, people=_directory(FakeDynamo(error=RuntimeError("throttled"))))
    people.reset_cache()
    handler.route(_chat(), None, invoker=invoker, people=_directory(FakeDynamo([ROW, dict(OTHER, cognito_sub=ROW["cognito_sub"])])))
    for payload in sent.payloads:
        assert _person(payload) == {}


def test_without_a_directory_there_is_no_person(launches):
    invoker, sent = launches
    handler.route(_chat("github:octocat"), None, invoker=invoker)
    assert _person(sent.payloads[0]) == {}


def test_a_bound_person_without_a_login_sends_no_login(launches):
    invoker, sent = launches
    slack_only = {k: v for k, v in ROW.items() if k != "github_login"}
    handler.route(_chat(), None, invoker=invoker, people=_directory(FakeDynamo([slack_only])))
    assert "personGithub" not in sent.payloads[0] and sent.payloads[0]["personId"] == "octo"


def test_the_body_cannot_set_person_fields(launches):
    invoker, sent = launches
    handler.route(_chat(None, sub=SUB_UNBOUND, personId="admin", personGithub="admin"), None, invoker=invoker,
                  people=_directory())
    assert _person(sent.payloads[0]) == {}


def test_an_acp_launch_carries_only_the_bound_person(receipt_identity):
    class Invoker:
        def __init__(self) -> None:
            self.payloads: list[dict[str, Any]] = []

        def receipt(self, payload: dict[str, Any]) -> dict[str, Any]:
            if payload.get("capabilities") == "runReceipts":
                return {"runReceipts": 1}
            self.payloads.append(payload)
            return {"status": "accepted"}

    invoker = Invoker()
    status, caps = handler.route(_receipt_event(receipt_identity, capabilities="acp"), None, invoker=invoker)
    sid = caps["sessionPrefix"] + "a" * 32
    event = _receipt_event(receipt_identity, "POST", session_id=sid, result_id="b" * 32, message="hello")
    event["headers"] = {"x-strandly-person": "github:hubot"}
    assert handler.route(event, None, invoker=invoker, people=_directory())[0] == 200
    (payload,) = invoker.payloads
    assert payload["triggerSource"] == "acp:prompt" and payload["portalUser"] == receipt_identity["sub"]
    assert payload["personId"] == "octo" and payload["personSlackUserId"] == "U0OCTO"

    unbound = dict(receipt_identity, sub=SUB_UNBOUND)
    status, caps = handler.route(_receipt_event(unbound, capabilities="acp"), None, invoker=invoker)
    sid = caps["sessionPrefix"] + "a" * 32
    event = _receipt_event(unbound, "POST", session_id=sid, result_id="b" * 32, message="hello")
    event["headers"] = {"x-strandly-person": "github:octocat"}
    assert handler.route(event, None, invoker=invoker, people=_directory())[0] == 200
    assert _person(invoker.payloads[-1]) == {} and invoker.payloads[-1]["portalUser"] == SUB_UNBOUND


def test_whoami_returns_only_the_callers_own_row():
    event = _signed_in(_evt("/api/chat", query={"whoami": "1"}), SUB_A)
    event["headers"] = {"x-strandly-person": "github:hubot"}
    status, body = handler.route(event, None, invoker=None, people=_directory())
    assert status == 200
    assert body == {"github": "octocat", "person": {"person_id": "octo", "name": "Octo Cat", "role": "maintainer",
                                                    "github_login": "octocat", "slack_user_id": "U0OCTO"}}
    unbound = _signed_in(_evt("/api/chat", query={"whoami": "1"}), SUB_UNBOUND)
    unbound["headers"] = {"x-strandly-person": "github:octocat"}
    assert handler.route(unbound, None, people=_directory()) == (200, {"github": None, "person": None})
    anonymous = _evt("/api/chat", query={"whoami": "1"})
    assert handler.route(anonymous, None, people=_directory()) == (200, {"github": None, "person": None})


def test_a_rename_records_the_bound_person_not_the_header():
    class Titles:
        def __init__(self) -> None:
            self.puts: list[tuple[str, str, dict[str, Any]]] = []

        def put(self, session_id: str, title: str, **kw: Any) -> None:
            self.puts.append((session_id, title, kw))

    titles = Titles()
    reader = FakeReader([{"task_id": "a", "session_id": "portal-1", "status": "completed",
                          "started_at": "2999-01-01T10:00:00+00:00"}])
    for sub in (SUB_A, SUB_UNBOUND):
        event = _signed_in(_evt("/api/sessions/portal-1", method="PATCH", params={"id": "portal-1"}), sub)
        event["body"] = json.dumps({"title": "Release prep"})
        event["headers"] = {"x-strandly-person": "github:hubot"}
        assert handler.route(event, reader, titles=titles, people=_directory())[0] == 200
    assert titles.puts == [
        ("portal-1", "Release prep", {"updated_by": f"portal:{SUB_A}", "person": "github:octocat"}),
        ("portal-1", "Release prep", {"updated_by": f"portal:{SUB_UNBOUND}", "person": None}),
    ]


def test_the_title_store_writes_the_person_attribute():
    class Resource:
        def __init__(self) -> None:
            self.items: list[dict[str, Any]] = []

        def Table(self, name: str) -> Any:  # noqa: N802 - boto3's spelling
            return self

        def put_item(self, Item: dict[str, Any]) -> None:  # noqa: N803 - boto3's spelling
            self.items.append(Item)

    resource = Resource()
    store = handler.SessionTitleStore(resource, "ledger")
    store.put("portal-1", "T", updated_by="portal:x", person="github:octocat")
    store.put("portal-1", "T", updated_by="portal:x")
    assert resource.items[0]["person"] == "github:octocat" and "person" not in resource.items[1]


def test_the_actions_ingress_ignores_the_header():
    invoker = FakeInvoker()
    event = _actions_evt(body={"prompt": "Triage this issue.", "session": "issue:12"})
    event["headers"] = {"x-strandly-person": "github:octocat"}
    assert actions.route(event, invoker, POLICY)[0] == 200
    ((_, payload),) = invoker.calls
    assert not any(k.startswith("person") for k in payload)


def test_lambda_handler_wires_the_directory_from_env(monkeypatch):
    monkeypatch.delenv(people.PEOPLE_TABLE_ENV, raising=False)
    monkeypatch.setattr(handler, "_PEOPLE", [])
    assert handler._build_people() is None
    monkeypatch.setenv(people.PEOPLE_TABLE_ENV, "strandly-dev-people")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    directory = handler._build_people()
    assert directory is not None and directory.enabled and handler._build_people() is directory


# --- direct Lambda invokes (IAM-trusted, no API Gateway or authorizer context) -------------------


def test_a_direct_invoke_names_its_person_with_the_header(launches):
    invoker, sent = launches
    dynamo = FakeDynamo()
    event = _chat("github:OctoCat", direct=True)
    assert handler.is_direct_invoke(event)
    assert handler.route(event, None, invoker=invoker, people=_directory(dynamo))[0] == 200
    (payload,) = sent.payloads
    assert _person(payload) == OCTO_FIELDS and payload["directInvoke"] is True
    assert "portalUser" not in payload and payload["triggerSource"] == "portal:chat"
    (query,) = dynamo.queries
    assert query["IndexName"] == "by_github" and query["ExpressionAttributeValues"] == {":v": {"S": "octocat"}}


def test_a_direct_invoke_person_defaults_the_trigger_to_portal_chat(launches):
    invoker, sent = launches
    event = _evt("/api/chat", method="POST")
    event["body"] = json.dumps({"session_id": "portal-1", "message": "hi"})
    event["headers"] = {"X-Strandly-Person": "github:octocat"}
    handler.route(event, None, invoker=invoker, people=_directory())
    assert sent.payloads[0]["triggerSource"] == "portal:chat" and sent.payloads[0]["personId"] == "octo"


@pytest.mark.parametrize("header", [None, "github:nobody", "octocat", "github:-bad"])
def test_a_direct_invoke_without_a_known_person_has_none(launches, header):
    invoker, sent = launches
    handler.route(_chat(header, direct=True), None, invoker=invoker, people=_directory())
    (payload,) = sent.payloads
    assert _person(payload) == {} and "directInvoke" not in payload and "portalUser" not in payload


def test_a_direct_invoke_naming_a_disabled_or_ambiguous_login_has_no_person(launches):
    invoker, sent = launches
    disabled = dict(ROW, enabled={"BOOL": False})
    handler.route(_chat("github:octocat", direct=True), None, invoker=invoker,
                  people=_directory(FakeDynamo([disabled])))
    people.reset_cache()
    twin = dict(OTHER, github_login={"S": "octocat"})
    handler.route(_chat("github:octocat", direct=True), None, invoker=invoker,
                  people=_directory(FakeDynamo([ROW, twin])))
    for payload in sent.payloads:
        assert _person(payload) == {} and "directInvoke" not in payload


def test_an_api_gateway_event_is_never_a_direct_invoke(launches):
    """``apiId`` or an authorizer context means API Gateway: the header selects nobody there."""
    invoker, sent = launches
    dynamo = FakeDynamo()
    handler.route(_chat("github:octocat", sub=None), None, invoker=invoker, people=_directory(dynamo))
    handler.route(_chat("github:hubot", sub=SUB_UNBOUND), None, invoker=invoker, people=_directory(dynamo))
    no_api_id = _chat("github:hubot", sub=SUB_UNBOUND)
    del no_api_id["requestContext"]["apiId"]  # an authorizer alone still means verified claims only
    handler.route(no_api_id, None, invoker=invoker, people=_directory(dynamo))
    for payload in sent.payloads:
        assert _person(payload) == {} and "directInvoke" not in payload
    assert all(q["IndexName"] == "by_cognito" for q in dynamo.queries)


def test_the_body_and_headers_cannot_set_the_direct_invoke_marker(launches):
    invoker, sent = launches
    handler.route(_chat("github:octocat", sub=SUB_UNBOUND, directInvoke=True, personId="octo"), None,
                  invoker=invoker, people=_directory())
    signed_in = _chat(None, directInvoke=True)
    signed_in["headers"] = {"directInvoke": "true", "x-strandly-direct-invoke": "1"}
    handler.route(signed_in, None, invoker=invoker, people=_directory())
    unknown = _chat("github:nobody", direct=True, directInvoke=True, personId="octo")
    handler.route(unknown, None, invoker=invoker, people=_directory())
    first, second, third = sent.payloads
    assert "directInvoke" not in first and _person(first) == {}
    assert "directInvoke" not in second and _person(second) == OCTO_FIELDS  # the bound person only
    assert "directInvoke" not in third and _person(third) == {}


def test_whoami_on_a_direct_invoke_is_the_named_person():
    event = _evt("/api/chat", query={"whoami": "1"})
    event["headers"] = {"x-strandly-person": "github:hubot"}
    status, body = handler.route(event, None, invoker=None, people=_directory())
    assert status == 200 and body["github"] == "hubot" and body["person"]["person_id"] == "hubot"
    bare = _evt("/api/chat", query={"whoami": "1"})
    assert handler.route(bare, None, people=_directory()) == (200, {"github": None, "person": None})


def test_a_direct_invoke_rename_records_the_named_person():
    class Titles:
        def __init__(self) -> None:
            self.puts: list[tuple[str, str, dict[str, Any]]] = []

        def put(self, session_id: str, title: str, **kw: Any) -> None:
            self.puts.append((session_id, title, kw))

    titles = Titles()
    reader = FakeReader([{"task_id": "a", "session_id": "portal-1", "status": "completed",
                          "started_at": "2999-01-01T10:00:00+00:00"}])
    for header in ("github:hubot", None):
        event = _evt("/api/sessions/portal-1", method="PATCH", params={"id": "portal-1"})
        event["body"] = json.dumps({"title": "Release prep"})
        if header:
            event["headers"] = {"x-strandly-person": header}
        assert handler.route(event, reader, titles=titles, people=_directory())[0] == 200
    assert titles.puts == [
        ("portal-1", "Release prep", {"updated_by": "portal", "person": "github:hubot"}),
        ("portal-1", "Release prep", {"updated_by": "portal", "person": None}),
    ]
