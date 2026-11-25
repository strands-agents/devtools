"""The session store over the SDK's real ``S3Storage`` with botocore's real error shapes (offline).

Two S3 doubles, both behind the unmodified ``strands.storage.S3Storage``:

- botocore ``Stubber`` on a real S3 client, for single-call semantics (404 vs 403 vs 503);
- ``fake_s3.FakeS3Client``, an in-memory bucket raising botocore's real ``NoSuchKey`` /
  ``ClientError``, for whole agent turns (dozens of calls in an order the SDK decides).

What is pinned: only a genuine not-found reads as "no session"; every other read error stops the
agent build; every write error fails the turn; each failure logs ``SESSION_STORAGE_FAILURE`` and
emits its metric; the run is recorded ``failed``; successful saves emit size/latency metrics.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from botocore.stub import Stubber
from strands import Agent

from fake_s3 import (
    ACCESS_DENIED,
    BUCKET,
    INTERNAL,
    KMS_DENIED,
    SLOW_DOWN,
    FakeS3Client,
    body,
    real_s3_client,
    s3_storage,
)
from scripted_model import ScriptedModel, text_events
from strandly.core.config import Config
from strandly.core.context import RuntimeContext
from strandly.core.session_store_reader import snapshot_key
from strandly.memory.session import build_session_manager
from strandly.memory.session_store import FailClosedStorage, SessionStorageError
from strandly.ops import metrics

TOKEN = "SESSION_STORAGE_FAILURE"


@pytest.fixture
def emitted(monkeypatch, capsys) -> Any:
    """Every EMF document written while the test runs (metrics enabled under a test namespace)."""
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "StrandlyTest")

    def read() -> list[dict[str, Any]]:
        docs = []
        for line in capsys.readouterr().out.splitlines():
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict) and "_aws" in doc:
                docs.append(doc)
        return docs

    return read


def _names(docs: list[dict[str, Any]]) -> set[str]:
    return {name for d in docs for name in d if not name.startswith("_") and name[0].isupper()}


def _store(client: Any) -> FailClosedStorage:
    return FailClosedStorage(s3_storage(client), backend=f"s3://{BUCKET}")


def _agent(storage: Any, session_id: str, model: Any) -> Agent:
    mgr = build_session_manager(Config(values={}), RuntimeContext(session_id=session_id), storage)
    return Agent(
        model=model,
        session_manager=mgr,
        storage=storage,
        context_manager="auto",
        callback_handler=None,
    )


# --- single calls through Stubber -----------------------------------------------------------


def test_not_found_is_the_only_read_error_that_means_missing():
    client = real_s3_client()
    with Stubber(client) as stub:
        stub.add_client_error(
            "get_object", service_error_code="NoSuchKey", http_status_code=404,
            expected_params={"Bucket": BUCKET, "Key": "session/s/x.json"},
        )
        assert asyncio.run(_store(client).read("session/s/x.json")) is None
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "shape", [ACCESS_DENIED, KMS_DENIED, SLOW_DOWN, INTERNAL], ids=["403", "kms", "503", "500"]
)
@pytest.mark.parametrize("key", ["session/s/snap.json", "context/s/ref_0", "session/s/memory-actor.json"])
def test_any_other_read_error_raises_logs_the_token_and_counts(shape, key, caplog, emitted):
    code, status, message = shape
    client = real_s3_client()
    with Stubber(client) as stub:
        stub.add_client_error(
            "get_object", service_error_code=code, http_status_code=status,
            service_message=message,
        )
        with pytest.raises(SessionStorageError, match=rf"{TOKEN}: session store read .*\({code}\)"):
            asyncio.run(_store(client).read(key))
    assert f"{TOKEN} op=read" in caplog.text and f"code={code}" in caplog.text
    assert metrics.SESSION_READ_FAILED in _names(emitted())


def test_a_failed_list_raises_instead_of_reading_as_empty(caplog, emitted):
    # Was: an unlistable session/ prefix returned [] and fenced (R5 mutant M13 had no test).
    client = real_s3_client()
    with Stubber(client) as stub:
        stub.add_client_error("list_objects_v2", service_error_code="AccessDenied", http_status_code=403)
        with pytest.raises(SessionStorageError, match="session store list"):
            asyncio.run(_store(client).list("session/s/"))
    assert f"{TOKEN} op=list" in caplog.text
    assert metrics.SESSION_READ_FAILED in _names(emitted())


@pytest.mark.parametrize("shape", [ACCESS_DENIED, KMS_DENIED, SLOW_DOWN], ids=["403", "kms", "503"])
def test_a_failed_write_raises_logs_the_token_and_counts(shape, caplog, emitted):
    code, status, message = shape
    client = real_s3_client()
    with Stubber(client) as stub:
        stub.add_client_error(
            "put_object", service_error_code=code, http_status_code=status, service_message=message
        )
        with pytest.raises(SessionStorageError, match=rf"session store write .*\({code}\)"):
            asyncio.run(_store(client).write("session/s/snap.json", b"{}"))
    assert f"{TOKEN} op=write" in caplog.text
    names = _names(emitted())
    assert metrics.SESSION_WRITE_FAILED in names
    assert metrics.SESSION_SNAPSHOT_BYTES not in names  # nothing was saved


def test_a_failed_delete_raises():
    client = real_s3_client()
    with Stubber(client) as stub:
        stub.add_client_error("delete_object", service_error_code="AccessDenied", http_status_code=403)
        with pytest.raises(SessionStorageError, match="session store delete"):
            asyncio.run(_store(client).delete("session/s/snap.json"))


def test_a_successful_snapshot_write_emits_size_and_latency(emitted):
    client = real_s3_client()
    with Stubber(client) as stub:
        stub.add_response("put_object", {"ETag": '"e"'}, {"Bucket": BUCKET, "Key": "session/s/a.json",
                                                         "Body": b"12345"})
        stub.add_response("get_object", {"Body": body(b"12345")}, {"Bucket": BUCKET,
                                                                   "Key": "session/s/a.json"})
        asyncio.run(_store(client).write("session/s/a.json", b"12345"))
        assert asyncio.run(_store(client).read("session/s/a.json")) == b"12345"
    docs = [d for d in emitted() if metrics.SESSION_SNAPSHOT_BYTES in d]
    assert len(docs) == 1
    assert docs[0][metrics.SESSION_SNAPSHOT_BYTES] == 5
    assert isinstance(docs[0][metrics.SESSION_WRITE_MS], (int, float))


# --- whole turns over the in-memory bucket --------------------------------------------------


def _seeded(sid: str) -> FakeS3Client:
    fake = FakeS3Client()
    _agent(_store(fake), sid, ScriptedModel(text_events("first answer")))("first question")
    assert snapshot_key(sid) in fake.objects
    return fake


def test_round_trip_over_s3_semantics():
    sid = "gh-o-r-issue-3"
    fake = _seeded(sid)
    agent = _agent(_store(fake), sid, ScriptedModel(text_events("second answer")))
    assert [m["role"] for m in agent.messages] == ["user", "assistant"]
    agent("second question")
    again = _agent(_store(fake), sid, ScriptedModel())
    assert len(again.messages) == 4


@pytest.mark.parametrize("shape", [ACCESS_DENIED, KMS_DENIED, SLOW_DOWN], ids=["403", "kms", "503"])
def test_an_unreadable_snapshot_stops_the_build_and_writes_nothing(shape, caplog):
    sid = "slack-T1-D1"
    fake = _seeded(sid)
    good = fake.objects[snapshot_key(sid)][0]
    fake.fail_when("get_object", shape, lambda key: key.startswith("session/"))
    puts_before = sum(op == "put_object" for op, _ in fake.calls)

    with pytest.raises(SessionStorageError, match=TOKEN):
        _agent(_store(fake), sid, ScriptedModel(text_events("never")))

    assert f"{TOKEN} op=read" in caplog.text
    assert sum(op == "put_object" for op, _ in fake.calls) == puts_before
    assert fake.objects[snapshot_key(sid)][0] == good
    # The fault clears: the next build restores the full history (no fence outlives a failure).
    fake.fail.clear()
    assert len(_agent(_store(fake), sid, ScriptedModel()).messages) == 2


def test_a_failed_snapshot_write_fails_the_turn(caplog):
    sid = "portal-u-20260101-ab"
    fake = _seeded(sid)
    fake.fail_when("put_object", SLOW_DOWN, lambda key: key.startswith("session/"))
    agent = _agent(_store(fake), sid, ScriptedModel(text_events("lost")))
    with pytest.raises(SessionStorageError, match="session store write"):
        agent("next question")
    assert f"{TOKEN} op=write" in caplog.text


# --- the wiring: build_agent and the run record ---------------------------------------------


@pytest.mark.asyncio
async def test_build_agent_refuses_to_build_over_an_unreadable_session(fake_model, tmp_path, monkeypatch):
    import strandly.memory.session_store as session_store
    from strandly.core.agent import build_agent

    fake = FakeS3Client()
    fake.fail_when("get_object", KMS_DENIED, lambda key: True)
    monkeypatch.setattr(session_store, "_s3_storage", lambda *a: s3_storage(fake))
    config = Config(values={"STRANDLY_SESSION_BUCKET": BUCKET, "AWS_REGION": "us-east-1"})
    with pytest.raises(SessionStorageError, match=TOKEN):
        await build_agent(config, RuntimeContext(cwd=str(tmp_path), session_id="s-kms"), model=fake_model)
    assert not any(op == "put_object" for op, _ in fake.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["get_object", "put_object"], ids=["read-at-build", "write-mid-turn"])
async def test_a_storage_failure_records_the_run_as_failed(op, monkeypatch, caplog):
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    fails: list[dict[str, Any]] = []
    finishes: list[dict[str, Any]] = []

    class FakeLedger:
        @staticmethod
        def from_config(config: Any) -> FakeLedger:
            return FakeLedger()

        def start(self, *a: Any, **kw: Any) -> None:
            pass

        def finish(self, *a: Any, **kw: Any) -> None:
            finishes.append(kw)

        def fail(self, *a: Any, **kw: Any) -> None:
            fails.append(kw)

    monkeypatch.setattr(ac, "RunLedger", FakeLedger)
    fake = _seeded("s-run")
    fake.fail_when(op, ACCESS_DENIED, lambda key: True)

    def run_turn(config: Any, user_input: Any, ctx: Any, **kw: Any) -> Any:
        async def gen() -> Any:
            # The real SDK agent over the real S3Storage: construction raises on a read failure,
            # the turn raises on a write failure.
            agent = _agent(_store(fake), "s-run", ScriptedModel(text_events("never")))
            await agent.invoke_async("continue")
            yield  # pragma: no cover

        return gen()

    monkeypatch.setattr(ac, "run_turn", run_turn)
    app = ac.build_app(Config(values={}))
    res = await app.handlers["main"]({"prompt": "continue", "sessionId": "s-run"})
    # Wait for the fire-and-forget run to settle rather than yield a fixed number of times: on a
    # slow runner it can still be running after any fixed count (seen on CI py3.10).
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    while True:
        poll = await app.handlers["main"]({"action": "poll", "taskId": res["taskId"]})
        if poll["status"] != "running" or loop.time() > deadline:
            break
        await asyncio.sleep(0.01)
    assert poll["status"] == "failed"
    assert not finishes
    (fail_kw,) = fails
    assert TOKEN in fail_kw["error"]
    assert f"{TOKEN} op={'read' if op == 'get_object' else 'write'}" in caplog.text


@pytest.mark.asyncio
async def test_a_poll_whose_session_read_fails_logs_the_token(monkeypatch, caplog):
    pytest.importorskip("bedrock_agentcore")
    import strandly.memory.session as session_mod
    import strandly.serve.agentcore_app as ac
    from strandly.core.session_store_reader import SessionStoreReader

    fake = FakeS3Client()
    fake.fail_when("get_object", KMS_DENIED, lambda key: True)
    monkeypatch.setattr(session_mod, "build_reader", lambda config: SessionStoreReader(BUCKET, client=fake))
    app = ac.build_app(Config(values={}))
    poll = await app.handlers["main"]({"action": "poll", "taskId": "t-unknown", "sessionId": "s-poll"})
    assert poll["status"] != "completed"  # degraded to the (absent) sentinel, never a fake answer
    assert f"{TOKEN} op=poll-read session=s-poll" in caplog.text


@pytest.mark.asyncio
async def test_a_failed_build_is_never_cached_so_the_next_invocation_retries():
    # Replaces the old "how long does a fenced agent live in AgentCache" gap: nothing half-built is
    # cached, so the first invocation after the fault clears restores the full history.
    from strandly.serve.cache import AgentCache

    sid = "gh-o-r-issue-77"
    fake = _seeded(sid)
    fake.fail_when("get_object", ACCESS_DENIED, lambda key: True)
    cache = AgentCache()

    async def build() -> Agent:
        return _agent(_store(fake), sid, ScriptedModel())

    with pytest.raises(SessionStorageError):
        await cache.get_or_build(sid, build)
    assert sid not in cache._agents
    fake.fail.clear()
    agent = await cache.get_or_build(sid, build)
    assert len(agent.messages) == 2 and cache._agents[sid] is agent
