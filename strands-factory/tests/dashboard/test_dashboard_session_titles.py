"""Session titles on the dashboard API: list/detail carry them, PATCH sets/clears them with the same
existence rule as the detail route, and the title store's batch read/write shapes."""

from __future__ import annotations

import base64
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from strandly.core.session_ids import SESSION_TITLE_MAX_LEN, session_title_key

_API_DIR = Path(__file__).resolve().parents[2] / "dashboard" / "api"
sys.path.insert(0, str(_API_DIR))
handler = importlib.import_module("handler")

TABLE = "strandly-test-runs"
SUB = "11111111-1111-4111-8111-111111111111"

ROWS = [
    {"task_id": "t2", "session_id": "s1", "status": "completed",
     "started_at": "2999-01-01T12:00:00+00:00", "prompt": "second"},
    {"task_id": "t1", "session_id": "s2", "status": "completed",
     "started_at": "2999-01-01T11:00:00+00:00", "prompt": "other"},
]


class FakeReader:
    def __init__(self, items: list[dict[str, Any]]):
        self._items = items

    def recent(self, limit: int) -> list[dict[str, Any]]:
        return self._items[:limit]


class FakeTable:
    def __init__(self, resource: FakeResource):
        self._r = resource

    def get_item(self, *, Key: dict[str, Any], **_: Any) -> dict[str, Any]:
        if self._r.fail:
            raise RuntimeError("throttled")
        item = self._r.rows.get(Key["task_id"])
        return {"Item": item} if item else {}

    def put_item(self, *, Item: dict[str, Any], **kwargs: Any) -> None:
        if self._r.fail:
            raise RuntimeError("AccessDenied")
        self._r.puts.append(Item)
        self._r.rows[Item["task_id"]] = Item


class FakeResource:
    """The DynamoDB service resource: ``batch_get_item`` plus ``Table(name)``."""

    def __init__(self, rows: dict[str, dict[str, Any]] | None = None, *, unprocessed_once: bool = False):
        self.rows = dict(rows or {})
        self.puts: list[dict[str, Any]] = []
        self.batches: list[list[str]] = []
        self.fail = False
        self._unprocessed_once = unprocessed_once

    def batch_get_item(self, *, RequestItems: dict[str, Any]) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("throttled")
        assert RequestItems[TABLE]["ExpressionAttributeNames"] == {"#key": "task_id", "#title": "title"}
        keys = [k["task_id"] for k in RequestItems[TABLE]["Keys"]]
        self.batches.append(keys)
        if self._unprocessed_once and len(keys) > 1:
            self._unprocessed_once = False
            served, rest = keys[:1], keys[1:]
            return {"Responses": {TABLE: [self.rows[k] for k in served if k in self.rows]},
                    "UnprocessedKeys": {TABLE: {**RequestItems[TABLE],
                                                "Keys": [{"task_id": k} for k in rest]}}}
        return {"Responses": {TABLE: [self.rows[k] for k in keys if k in self.rows]}}

    def Table(self, name: str) -> FakeTable:  # noqa: N802 - boto3's name
        assert name == TABLE
        return FakeTable(self)


def _store(rows: dict[str, str] | None = None, **kw: Any) -> tuple[handler.SessionTitleStore, FakeResource]:
    resource = FakeResource({session_title_key(sid): {"task_id": session_title_key(sid), "title": t}
                             for sid, t in (rows or {}).items()}, **kw)
    return handler.SessionTitleStore(resource, TABLE), resource


def _evt(path: str, method: str = "GET", sid: str | None = None, body: Any = None,
         claims: dict | None = None) -> dict[str, Any]:
    evt: dict[str, Any] = {
        "rawPath": path,
        "requestContext": {"http": {"method": method, "path": path}},
        "pathParameters": {"id": sid} if sid else {},
        "queryStringParameters": {},
    }
    if body is not None:
        evt["body"] = body if isinstance(body, str) else json.dumps(body)
    if claims is not None:
        evt["requestContext"]["authorizer"] = {"jwt": {"claims": claims}}
    return evt


def _patch(sid: str, body: Any, **kw: Any) -> dict[str, Any]:
    return _evt(f"/api/sessions/{sid}", "PATCH", sid, body, **kw)


# --- reads -------------------------------------------------------------------------------------


def test_session_list_carries_titles():
    store, resource = _store({"s1": "Release prep"})
    status, body = handler.route(_evt("/api/sessions"), FakeReader(ROWS), titles=store)
    assert status == 200
    assert {s["session_id"]: s["title"] for s in body["sessions"]} == {"s1": "Release prep", "s2": None}
    assert resource.batches == [[session_title_key("s1"), session_title_key("s2")]]


def test_session_list_without_a_store_has_null_titles():
    status, body = handler.route(_evt("/api/sessions"), FakeReader(ROWS))
    assert status == 200 and all(s["title"] is None for s in body["sessions"])


def test_session_list_survives_a_failed_title_read():
    store, resource = _store({"s1": "Release prep"})
    resource.fail = True
    status, body = handler.route(_evt("/api/sessions"), FakeReader(ROWS), titles=store)
    assert status == 200 and all(s["title"] is None for s in body["sessions"])


def test_title_batches_retry_unprocessed_keys_and_split_at_100():
    store, resource = _store({"s0": "zero", "s50": "fifty", "s120": "later"}, unprocessed_once=True)
    ids = [f"s{i}" for i in range(150)]
    assert store.titles(ids) == {"s0": "zero", "s50": "fifty", "s120": "later"}
    assert [len(b) for b in resource.batches] == [100, 99, 50]


def test_title_batches_fail_after_persistent_unprocessed_keys():
    class Stuck(FakeResource):
        def batch_get_item(self, *, RequestItems: dict[str, Any]) -> dict[str, Any]:
            return {"Responses": {TABLE: []}, "UnprocessedKeys": RequestItems}

    with pytest.raises(RuntimeError):
        handler.SessionTitleStore(Stuck(), TABLE).titles(["s1"])


@pytest.mark.parametrize("query", [{}, {"limit": "10"}])
def test_session_detail_carries_the_title(query):
    store, _ = _store({"s1": "Release prep"})
    evt = _evt("/api/sessions/s1", sid="s1")
    evt["queryStringParameters"] = query
    status, body = handler.route(evt, FakeReader(ROWS), titles=store)
    assert status == 200 and body["title"] == "Release prep"
    status, body = handler.route(_evt("/api/sessions/s2", sid="s2"), FakeReader(ROWS), titles=store)
    assert status == 200 and body["title"] is None


# --- PATCH -------------------------------------------------------------------------------------


def test_patch_sets_the_title_and_records_the_person(monkeypatch):
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-west-2_pool")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    claims = {"sub": SUB, "iss": "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_pool",
              "token_use": "access"}
    store, resource = _store()
    status, body = handler.route(_patch("s1", {"title": "  Release\nprep "}, claims=claims),
                                 FakeReader(ROWS), titles=store)
    assert (status, body) == (200, {"session_id": "s1", "title": "Release prep"})
    (row,) = resource.puts
    assert row["task_id"] == session_title_key("s1") and row["title"] == "Release prep"
    assert row["updated_by"] == f"portal:{SUB}" and "gsi_pk" not in row
    status, body = handler.route(_evt("/api/sessions"), FakeReader(ROWS), titles=store)
    assert body["sessions"][0]["title"] == "Release prep"


def test_patch_without_a_verified_subject_is_still_a_person_edit():
    store, resource = _store()
    status, _ = handler.route(_patch("s1", {"title": "x"}), FakeReader(ROWS), titles=store)
    assert status == 200 and resource.puts[0]["updated_by"] == "portal"


@pytest.mark.parametrize("cleared", ["", "   ", None])
def test_patch_empty_or_null_clears(cleared):
    store, resource = _store({"s1": "Old"})
    status, body = handler.route(_patch("s1", {"title": cleared}), FakeReader(ROWS), titles=store)
    assert (status, body) == (200, {"session_id": "s1", "title": None})
    assert "title" not in resource.rows[session_title_key("s1")]
    _, detail = handler.route(_evt("/api/sessions/s1", sid="s1"), FakeReader(ROWS), titles=store)
    assert detail["title"] is None


@pytest.mark.parametrize("body", [
    {}, {"name": "x"}, {"title": "x", "extra": 1}, {"title": 7}, {"title": ["x"]},
    {"title": "x" * (SESSION_TITLE_MAX_LEN + 1)}, "not json", "[1]",
])
def test_patch_rejects_invalid_bodies(body):
    store, resource = _store()
    status, _ = handler.route(_patch("s1", body), FakeReader(ROWS), titles=store)
    assert status == 400 and resource.puts == []


def test_patch_accepts_a_base64_body():
    store, _ = _store()
    evt = _patch("s1", None)
    evt["body"] = base64.b64encode(json.dumps({"title": "b64"}).encode()).decode()
    evt["isBase64Encoded"] = True
    assert handler.route(evt, FakeReader(ROWS), titles=store)[0] == 200


def test_patch_unknown_session_is_404_and_writes_nothing():
    store, resource = _store()
    status, _ = handler.route(_patch("nope", {"title": "x"}), FakeReader(ROWS), titles=store)
    assert status == 404 and resource.puts == []


def test_patch_accepts_a_session_known_only_from_its_stored_transcript():
    class Memory:
        def transcript(self, sid: str) -> list[dict[str, Any]]:
            return [{"role": "user", "text": "hi"}] if sid == "old" else []

    store, resource = _store()
    status, _ = handler.route(_patch("old", {"title": "x"}), FakeReader(ROWS), memory=Memory(), titles=store)
    assert status == 200 and resource.puts[0]["session_id"] == "old"
    status, _ = handler.route(_patch("gone", {"title": "x"}), FakeReader(ROWS), memory=Memory(), titles=store)
    assert status == 404


def test_patch_canonicalizes_the_session_id():
    store, resource = _store()
    rows = [{**ROWS[0], "session_id": "a-b"}]
    evt = _evt("/api/sessions/a%2Fb", "PATCH", "a/b", {"title": "x"})
    status, body = handler.route(evt, FakeReader(rows), titles=store)
    assert status == 200 and body["session_id"] == "a-b"
    assert resource.puts[0]["task_id"] == session_title_key("a-b")


def test_patch_on_the_list_route_is_405():
    store, _ = _store()
    assert handler.route(_evt("/api/sessions", "PATCH", body={"title": "x"}), FakeReader(ROWS),
                         titles=store)[0] == 405


def test_patch_without_a_store_is_503():
    assert handler.route(_patch("s1", {"title": "x"}), FakeReader(ROWS))[0] == 503


def test_patch_write_failure_is_503():
    store, resource = _store()
    resource.fail = True
    assert handler.route(_patch("s1", {"title": "x"}), FakeReader(ROWS), titles=store)[0] == 503


def test_lambda_handler_wires_the_title_store(monkeypatch):
    store, _ = _store({"s1": "Wired"})
    monkeypatch.setattr(handler, "_build_reader", lambda: FakeReader(ROWS))
    monkeypatch.setattr(handler, "_build_invoker", lambda: None)
    monkeypatch.setattr(handler, "_build_transcript_reader", lambda: None)
    monkeypatch.setattr(handler, "_build_work_product_reader", lambda: None)
    monkeypatch.setattr(handler, "_build_title_store", lambda: store)
    out = handler.lambda_handler(_evt("/api/sessions"))
    assert json.loads(out["body"])["sessions"][0]["title"] == "Wired"
