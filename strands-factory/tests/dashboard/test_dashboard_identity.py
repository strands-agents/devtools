"""Authorize the same canonical session key the native runtime/storage will use."""

import copy
import hashlib
from types import SimpleNamespace

import pytest
from test_dashboard_api import FakeReader, _evt, _receipt_event, handler
from test_dashboard_api import receipt_identity as receipt_identity

from strandly.core.session_ids import sanitize_session_id

ALIASES = ("{}", " \t{}", "//{}", "\\{}", "---{}", "\u2003{}", "\x00{}", "{} /",
           " \t/---{}---\\ ")


def _sid(claims):
    digest = hashlib.sha256(f"{claims['iss']}:{claims['sub']}".encode()).hexdigest()[:24]
    return f"acp-{digest}-" + "a" * 32


def _operation(operation, *, receipt=False):
    if operation == "poll":
        return "GET", {"result_id": "b" * 32, "since": "3"} if receipt else {"task_id": "task", "since": "3"}
    values = {"action": "cancel"} if operation == "cancel" else {"message": "hello"}
    return "POST", {**values, **({"result_id": "b" * 32} if receipt else {})}


@pytest.fixture
def invocations(monkeypatch):
    invoker = handler.RuntimeInvoker.__new__(handler.RuntimeInvoker)
    sent = []

    def invoke(session_id, payload):
        sent.append((session_id, payload))
        return {"status": "accepted"}

    monkeypatch.setattr(invoker, "_invoke", invoke)
    return invoker, sent


@pytest.mark.parametrize("alias", ALIASES)
@pytest.mark.parametrize("field", ["session_id", "sessionId"])
@pytest.mark.parametrize("operation", ["launch", "cancel", "poll"])
@pytest.mark.parametrize("receipt", [False, True])
def test_foreign_acp_alias_never_reaches_runtime(receipt_identity, invocations, alias, field, operation, receipt):
    invoker, sent = invocations
    victim = _sid(receipt_identity)
    foreign = {**receipt_identity, "sub": "23456789-1234-1234-1234-123456789abc"}
    method, values = _operation(operation, receipt=receipt)
    raw = alias.format(victim)
    assert sanitize_session_id(raw) == victim
    event = _receipt_event(foreign, method, **values, **{field: raw})
    original = copy.deepcopy(event)
    assert handler.route(event, None, invoker=invoker)[0] == 400
    assert sent == []
    assert event == original


@pytest.mark.parametrize("alias", ALIASES)
@pytest.mark.parametrize("field", ["session_id", "sessionId", "both"])
@pytest.mark.parametrize("operation", ["launch", "cancel", "poll"])
def test_owner_receipt_alias_forwards_only_canonical_id(receipt_identity, invocations, alias, field, operation):
    invoker, sent = invocations
    sid = _sid(receipt_identity)
    method, values = _operation(operation, receipt=True)
    fields = {field: alias.format(sid)} if field != "both" else {"session_id": sid, "sessionId": alias.format(sid)}
    event = _receipt_event(receipt_identity, method, **values, **fields)
    original = copy.deepcopy(event)
    assert handler.route(event, None, invoker=invoker)[0] == 200
    expected = {"sessionId": sid, "resultId": "b" * 32}
    if operation == "launch":
        expected.update(prompt="hello", triggerSource="acp:prompt", portalUser=receipt_identity["sub"],
                        portalIssuer=receipt_identity["iss"])
    else:
        expected["action"] = operation
        if operation == "poll":
            expected["since"] = 3
    assert sent == [(sid, expected)]
    assert event == original


@pytest.mark.parametrize("operation", ["launch", "cancel", "poll"])
@pytest.mark.parametrize("receipt", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("other", ["native-session", "acp-other", "", None])
def test_conflicting_session_spellings_are_rejected(receipt_identity, invocations, operation, receipt, reverse, other):
    invoker, sent = invocations
    first, second = " /" + _sid(receipt_identity), other
    if reverse:
        first, second = second, first
    method, values = _operation(operation, receipt=receipt)
    event = _receipt_event(receipt_identity, method, **values, session_id=first, sessionId=second)
    assert handler.route(event, None, invoker=invoker) == (400, {"error": "conflicting session_id and sessionId"})
    assert sent == []


@pytest.mark.parametrize("field", ["session_id", "sessionId"])
@pytest.mark.parametrize("operation", ["launch", "cancel", "poll"])
@pytest.mark.parametrize("raw", ["native-session", " /native session//item/ ", "native.id_1", "---", 123])
def test_native_chat_retains_normalization_and_behavior(receipt_identity, invocations, field, operation, raw):
    invoker, sent = invocations
    method, values = _operation(operation)
    event = _receipt_event(receipt_identity, method, **values, **{field: raw})
    assert handler.route(event, None, invoker=invoker)[0] == 200
    sid = sanitize_session_id(str(raw))
    expected = {"sessionId": sid}
    if operation == "launch":
        expected.update(prompt="hello", portalUser=receipt_identity["sub"], portalIssuer=receipt_identity["iss"])
    else:
        expected["action"] = operation
        if operation == "poll":
            expected.update(taskId="task", since=3)
    assert sent == [(sid, expected)]


@pytest.mark.parametrize("operation", ["launch", "cancel", "poll"])
@pytest.mark.parametrize("raw", ["", " \t", None])
def test_blank_native_session_stays_invalid(receipt_identity, invocations, operation, raw):
    invoker, sent = invocations
    method, values = _operation(operation)
    assert handler.route(_receipt_event(receipt_identity, method, **values, session_id=raw), None, invoker=invoker)[0] == 400
    assert sent == []


@pytest.mark.parametrize("operation", ["launch", "cancel", "poll"])
@pytest.mark.parametrize("field", ["portalUser", "portalIssuer", "portal_user", "portal_issuer", "scope",
                                  "sessionPrefix", "sessionKey", "sessionArn", "triggerSource", "invocationContext"])
def test_receipt_alias_does_not_enable_identity_or_scope_overrides(receipt_identity, invocations, operation, field):
    invoker, sent = invocations
    method, values = _operation(operation, receipt=True)
    event = _receipt_event(receipt_identity, method, **values, sessionId="/" + _sid(receipt_identity), **{field: "forged"})
    assert handler.route(event, None, invoker=invoker)[0] == 400
    assert sent == []


@pytest.mark.parametrize("change,status", [({"sub": "23456789-1234-1234-1234-123456789abc"}, 400),
                                          ({"iss": "https://foreign/pool"}, 401),
                                          ({"token_use": "id"}, 401), ({"sub": "mutable-name"}, 401)])
def test_receipt_alias_uses_immutable_verified_identity(receipt_identity, invocations, change, status):
    invoker, sent = invocations
    claims = {**receipt_identity, "email": "owner@example.com", "username": receipt_identity["sub"], **change}
    event = _receipt_event(claims, "POST", sessionId="/" + _sid(receipt_identity), result_id="b" * 32, message="hello")
    assert handler.route(event, None, invoker=invoker)[0] == status
    assert sent == []


@pytest.mark.parametrize("paged", [False, True])
@pytest.mark.parametrize("alias", ALIASES)
@pytest.mark.parametrize("acp", [False, True])
def test_session_reads_use_canonical_key_without_changing_team_read_policy(receipt_identity, alias, paged, acp):
    sid = _sid(receipt_identity) if acp else "native-session"
    raw = alias.format(sid)
    reads = []

    def transcript(session_id):
        reads.append(session_id)
        return [{"id": "message-1", "role": "assistant", "text": "hello"}]

    memory = SimpleNamespace(transcript=transcript, pagination_transcript=lambda sid: (transcript(sid), False))
    event = _evt("/api/sessions/alias", params={"id": raw}, query={"limit": "1"} if paged else {})
    original = copy.deepcopy(event)
    status, body = handler.route(event, FakeReader([]), memory=memory)
    assert status == 200 and body["session_id"] == sid
    assert reads == [sid]
    assert event == original


@pytest.mark.parametrize("field", ["session_id", "sessionId", "both"])
@pytest.mark.parametrize("alias", ALIASES)
def test_workproduct_reads_use_canonical_key(receipt_identity, alias, field):
    sid = _sid(receipt_identity)
    fields = {field: alias.format(sid)} if field != "both" else {"session_id": sid, "sessionId": alias.format(sid)}
    reads = []
    reader = SimpleNamespace(by_session=lambda session_id, **kwargs: reads.append(session_id) or [])
    event = _evt("/api/workproducts", query=fields)
    original = copy.deepcopy(event)
    assert handler.route(event, None, work_products=reader) == (200, {"items": []})
    assert reads == [sid]
    assert event == original


def test_workproduct_conflicting_session_spellings_do_not_read():
    reads = []
    reader = SimpleNamespace(by_session=lambda session_id, **kwargs: reads.append(session_id) or [])
    event = _evt("/api/workproducts", query={"session_id": "native", "sessionId": "/acp-other"})
    assert handler.route(event, None, work_products=reader)[0] == 400
    assert reads == []
