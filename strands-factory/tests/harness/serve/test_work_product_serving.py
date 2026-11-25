"""Work-product extraction, schema conversion, and persistence at the serving boundary."""

from __future__ import annotations

import asyncio

import pytest

from strandly.core.config import Config


async def _drain_background():
    for _ in range(100):
        await asyncio.sleep(0)


_WP_BLOCK = (
    "Here is my review.\n\n"
    "```work-product\n"
    '{"kind": "review", "verdict": "changes-requested", "tldr": "one real bug",\n'
    ' "repo": "o/r", "number": 42,\n'
    ' "evidence_ledger": {"branch_sha": "abc", "tests_passed": 12},\n'
    ' "findings": [{"id": "f1", "file": "x.py", "line": 3, "tier": "block",\n'
    '   "confidence": 0.9, "text": "off-by-one", "suppressed": false}]}\n'
    "```"
)


def test_extract_work_product_pulls_last_block_and_strips_prose():
    import strandly.serve.agentcore_app as ac

    text, data = ac._extract_work_product(_WP_BLOCK)
    assert data is not None
    assert data["kind"] == "review" and data["verdict"] == "changes-requested"
    assert text == "Here is my review."  # block gone from prose
    assert "work-product" not in text and "```" not in text


def test_extract_work_product_last_match_wins():
    import strandly.serve.agentcore_app as ac

    two = (
        '```work-product\n{"kind": "review", "verdict": "approve"}\n```\n'
        "middle\n"
        '```work-product\n{"kind": "triage", "verdict": "accept"}\n```'
    )
    text, data = ac._extract_work_product(two)
    assert data["kind"] == "triage"  # the LAST block wins
    assert "work-product" not in text and "middle" in text  # both fences stripped


def test_extract_work_product_no_block_returns_text_unchanged():
    import strandly.serve.agentcore_app as ac

    text, data = ac._extract_work_product("just a normal answer, no block")
    assert data is None and text == "just a normal answer, no block"


def test_extract_work_product_malformed_json_fails_open_but_still_strips():
    import strandly.serve.agentcore_app as ac

    text, data = ac._extract_work_product("prose\n```work-product\n{not valid json,,,}\n```")
    assert data is None  # fail-open: nothing persisted
    assert text == "prose"  # ...but the block is still stripped from the prose


def test_extract_work_product_non_object_body_fails_open():
    import strandly.serve.agentcore_app as ac

    text, data = ac._extract_work_product("p\n```work-product\n[1, 2, 3]\n```")
    assert data is None and text == "p"


@pytest.mark.parametrize(
    "block",
    [
        # YAML instead of JSON (the dominant prod failure shape)
        "```work-product\nkind: review\nverdict: approve\nfindings:\n  - severity: high\n"
        "    text: 'x: y'\n```",
        # prose around the object inside the fence
        'Summary below:\n{"kind": "review", "verdict": "approve"}\nthanks',
        # trailing commas
        '{"kind": "review", "verdict": "approve", "findings": [1, 2,],}',
        # double-fenced ```json inside the block
        '```json\n{"kind": "review", "verdict": "approve"}\n```',
    ],
    ids=["yaml", "prose", "trailing-comma", "double-fence"],
)
def test_extract_work_product_tolerates_slightly_malformed_bodies(block):
    import strandly.serve.agentcore_app as ac

    if not block.startswith("```work-product"):
        block = f"```work-product\n{block}\n```"
    text, data = ac._extract_work_product(f"prose\n{block}")
    assert data is not None and data["kind"] == "review" and data["verdict"] == "approve"
    assert text.startswith("prose") and "work-product" not in text


@pytest.mark.parametrize(
    "fence", ["```work_product", "```work-product json", "  ```work-product  "]
)
def test_extract_work_product_tolerates_info_string_variants(fence):
    import strandly.serve.agentcore_app as ac

    text, data = ac._extract_work_product(
        f'p\n{fence}\n{{"kind": "triage", "verdict": "accept"}}\n```\nafter'
    )
    assert data == {"kind": "triage", "verdict": "accept"}
    assert "work" not in text and "after" in text


def test_extract_work_product_unclosed_trailing_block_still_parses():
    import strandly.serve.agentcore_app as ac

    text, data = ac._extract_work_product('p\n```work-product\n{"kind": "review", "verdict": "x"}')
    assert data == {"kind": "review", "verdict": "x"} and text == "p"


def test_extract_work_product_yaml_dates_become_json_strings():
    import json

    import strandly.serve.agentcore_app as ac

    _t, data = ac._extract_work_product("```work-product\nkind: review\nverdict: ok\nday: 2026-11-04\n```")
    assert data["day"] == "2026-11-04"
    json.dumps(data)  # serializable like a JSON-parsed block


def test_extract_work_product_prose_yaml_is_not_an_artifact():
    import strandly.serve.agentcore_app as ac

    # Valid YAML (a plain string / an unrelated mapping) must not become a work product.
    for body in ("I looked at the PR and it is fine", "note: nothing to report"):
        _t, data = ac._extract_work_product(f"```work-product\n{body}\n```")
        assert data is None


def test_extract_work_product_drop_is_logged_and_counted(monkeypatch, caplog):
    import logging

    import strandly.serve.agentcore_app as ac

    emitted: list = []
    monkeypatch.setattr(ac.metrics, "emit", lambda doc, **kw: emitted.append(doc) or True)
    with caplog.at_level(logging.WARNING, logger=ac.logger.name):
        _t, data = ac._extract_work_product("```work-product\n{not valid json,,,}\n```")
    assert data is None
    assert emitted == [{"WorkProductParseFailed": 1}]
    assert any("work-product block dropped" in r.getMessage() and "not valid json" in r.getMessage()
               for r in caplog.records)


def test_work_product_from_block_builds_a_draft_artifact():
    import strandly.serve.agentcore_app as ac
    from strandly.ops.work_product import STATUS_DRAFT

    _text, data = ac._extract_work_product(_WP_BLOCK)
    wp = ac._work_product_from_block(data, session_id="gh-o-r-pr-42", run_task_id="task-1")
    assert wp is not None
    assert wp.session_id == "gh-o-r-pr-42" and wp.run_task_id == "task-1"
    assert wp.status == STATUS_DRAFT and wp.kind == "review" and wp.number == 42
    assert wp.evidence_ledger == {"branch_sha": "abc", "tests_passed": 12}
    assert [f.id for f in wp.findings] == ["f1"]
    # `included` is unset on the block, so it stays None on the dataclass and resolves to
    # "not suppressed" (→ True here) when the store serializes it.
    assert wp.findings[0].confidence == 0.9 and wp.findings[0].included is None
    assert wp.findings[0].to_item()["included"] is True


def test_work_product_from_block_requires_kind_and_verdict():
    import strandly.serve.agentcore_app as ac

    assert ac._work_product_from_block({"verdict": "approve"}, session_id="s", run_task_id="t") is None
    assert ac._work_product_from_block({"kind": "review"}, session_id="s", run_task_id="t") is None


def test_work_product_from_block_coerces_a_bad_number_to_none():
    import strandly.serve.agentcore_app as ac

    wp = ac._work_product_from_block(
        {"kind": "triage", "verdict": "accept", "number": "not-a-number"},
        session_id="s", run_task_id="t",
    )
    assert wp is not None and wp.number is None  # a bad number never drops the whole artifact


def test_work_product_from_block_ignores_an_artifacts_field():
    """A block that still carries an ``artifacts`` list parses; the field is dropped."""
    import strandly.serve.agentcore_app as ac

    wp = ac._work_product_from_block(
        {"kind": "review", "verdict": "approve", "artifacts": ["o/r/pr/1/pytest.log", 7]},
        session_id="s", run_task_id="t",
    )
    assert wp is not None and wp.verdict == "approve"
    assert "artifacts" not in wp.to_item()


def test_work_product_from_block_carries_suspected_area():
    import strandly.serve.agentcore_app as ac

    wp = ac._work_product_from_block(
        {"kind": "first-response", "verdict": "confirmed-bug", "suspected_area": "runtime/x.py:44"},
        session_id="s", run_task_id="t",
    )
    assert wp is not None and wp.suspected_area == "runtime/x.py:44"


def test_work_product_from_block_carries_title():
    import strandly.serve.agentcore_app as ac

    wp = ac._work_product_from_block(
        {"kind": "review", "verdict": "approve", "title": "Fix retry backoff overflow"},
        session_id="s", run_task_id="t",
    )
    assert wp is not None and wp.title == "Fix retry backoff overflow"


class _FakeWorkProductStore:
    """A WorkProductStore stand-in: records puts, enabled via from_config."""

    puts: list = []

    @classmethod
    def from_config(cls, config):
        cls.puts = []
        return cls()

    def put(self, wp):
        type(self).puts.append(wp)


@pytest.mark.asyncio
async def test_run_persists_work_product_and_strips_block_from_result(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac
    from strandly.core.events import HarnessEvent

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    monkeypatch.setattr(ac, "WorkProductStore", _FakeWorkProductStore)

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        async def gen():
            yield HarnessEvent(kind="turn_start", data={"reason": "start"})
            yield HarnessEvent(kind="text", text=_WP_BLOCK)
            yield HarnessEvent(kind="done", data={})

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)
    app = ac.build_app(Config(values={}))
    res = await app.handlers["main"]({"prompt": "review it", "sessionId": "gh-o-r-pr-42"})
    await _drain_background()

    assert len(_FakeWorkProductStore.puts) == 1
    wp = _FakeWorkProductStore.puts[0]
    assert wp.kind == "review" and wp.session_id == "gh-o-r-pr-42"
    assert wp.run_task_id == res["taskId"] and wp.status == "draft"

    poll = await app.handlers["main"](
        {"action": "poll", "taskId": res["taskId"], "sessionId": "gh-o-r-pr-42"}
    )
    assert poll["status"] == "completed"
    assert poll["result"] == "Here is my review."  # block never leaks into the prose
    assert "work-product" not in poll["result"]


@pytest.mark.asyncio
async def test_run_malformed_block_persists_nothing_but_completes(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac
    from strandly.core.events import HarnessEvent

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    monkeypatch.setattr(ac, "WorkProductStore", _FakeWorkProductStore)

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        async def gen():
            yield HarnessEvent(kind="text", text="done.\n```work-product\n{bad json,,}\n```")
            yield HarnessEvent(kind="done", data={})

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)
    app = ac.build_app(Config(values={}))
    res = await app.handlers["main"]({"prompt": "review it", "sessionId": "gh-o-r-pr-1"})
    await _drain_background()

    assert _FakeWorkProductStore.puts == []  # fail-open: nothing persisted
    poll = await app.handlers["main"](
        {"action": "poll", "taskId": res["taskId"], "sessionId": "gh-o-r-pr-1"}
    )
    assert poll["status"] == "completed"  # the run still completed
    assert poll["result"] == "done."  # block stripped even though it didn't parse


@pytest.mark.asyncio
async def test_run_word_confidence_still_completes_and_persists(monkeypatch):
    """Regression (prod 2026-09-25): ``"confidence": "high"`` raised ValueError after
    ``store.finish()`` and flipped a completed run to failed. It must now map to a number."""
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac
    from strandly.core.events import HarnessEvent

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    monkeypatch.setattr(ac, "WorkProductStore", _FakeWorkProductStore)
    block = (
        "Reviewed.\n```work-product\n"
        '{"kind": "review", "verdict": "approve", "findings": ['
        '{"id": "f1", "line": "88-90", "confidence": "high", "text": "x"},'
        '{"id": "f2", "confidence": "dunno", "text": "y"}]}\n```'
    )

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        async def gen():
            yield HarnessEvent(kind="text", text=block)
            yield HarnessEvent(kind="done", data={})

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)
    app = ac.build_app(Config(values={}))
    res = await app.handlers["main"]({"prompt": "review it", "sessionId": "gh-o-r-pr-7"})
    await _drain_background()

    poll = await app.handlers["main"](
        {"action": "poll", "taskId": res["taskId"], "sessionId": "gh-o-r-pr-7"}
    )
    assert poll["status"] == "completed"
    assert poll["result"] == "Reviewed."
    [wp] = _FakeWorkProductStore.puts
    f1, f2 = wp.findings
    assert f1.confidence == 0.9 and f1.line == 88
    assert f2.confidence is None  # unreadable → None, the finding itself is kept


class _ExplodingWorkProductStore(_FakeWorkProductStore):
    def put(self, wp):
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_run_work_product_persistence_error_never_fails_the_run(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac
    from strandly.core.events import HarnessEvent

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    monkeypatch.setattr(ac, "WorkProductStore", _ExplodingWorkProductStore)

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        async def gen():
            yield HarnessEvent(kind="text", text=_WP_BLOCK)
            yield HarnessEvent(kind="done", data={})

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)
    app = ac.build_app(Config(values={}))
    res = await app.handlers["main"]({"prompt": "review it", "sessionId": "gh-o-r-pr-8"})
    await _drain_background()

    poll = await app.handlers["main"](
        {"action": "poll", "taskId": res["taskId"], "sessionId": "gh-o-r-pr-8"}
    )
    assert poll["status"] == "completed"
    assert poll["result"] == "Here is my review."


def test_persist_work_product_swallows_build_errors(monkeypatch, caplog):
    import strandly.serve.agentcore_app as ac

    def boom(*a, **k):
        raise ValueError("could not convert string to float: 'high'")

    monkeypatch.setattr(ac, "_work_product_from_block", boom)
    store = _FakeWorkProductStore.from_config(None)
    with caplog.at_level("WARNING"):
        ac._persist_work_product(
            store, {"kind": "review", "verdict": "approve"}, session_id="s", task_id="t"
        )
    assert _FakeWorkProductStore.puts == []
    assert "work-product persistence failed" in caplog.text


def test_persist_work_product_skips_without_session_id():
    import strandly.serve.agentcore_app as ac

    store = _FakeWorkProductStore.from_config(None)
    ac._persist_work_product(
        store, {"kind": "review", "verdict": "approve"}, session_id=None, task_id="t"
    )
    assert _FakeWorkProductStore.puts == []


@pytest.mark.asyncio
async def test_run_without_a_session_id_skips_work_product_persist(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac
    from strandly.core.events import HarnessEvent

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    monkeypatch.setattr(ac, "WorkProductStore", _FakeWorkProductStore)

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        async def gen():
            yield HarnessEvent(kind="text", text=_WP_BLOCK)
            yield HarnessEvent(kind="done", data={})

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)
    app = ac.build_app(Config(values={}))
    await app.handlers["main"]({"prompt": "review it"})  # no sessionId → no by_session key
    await _drain_background()
    assert _FakeWorkProductStore.puts == []
