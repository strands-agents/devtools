"""The one-off knowledge-base → AgentCore Memory migration (synthetic fixture only, never real data)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT / "tests"))
from fake_agentcore import MEMORY_ID, THROTTLED, FakeAgentCoreClient  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "migrate_kb", REPO_ROOT / "infra" / "scripts" / "migrate_kb_to_agentcore_memory.py"
)
assert _SPEC and _SPEC.loader
mig = importlib.util.module_from_spec(_SPEC)
sys.modules["migrate_kb"] = mig
_SPEC.loader.exec_module(mig)


def _vector(text: str, source: str, *, namespace: str | None = None, created: str = "2025-03-01T10:00:00Z") -> dict:
    meta: dict[str, Any] = {
        "x-amz-bedrock-kb-source-file-modality": "TEXT",
        "x-amz-bedrock-kb-data-source-id": "DS0000000",
        "AMAZON_BEDROCK_TEXT": text,
        "AMAZON_BEDROCK_METADATA": json.dumps(
            {"createDate": created, "modifiedDate": created, "sourceDocumentId": f"chunk-{text[:4]}",
             "source": {"sourceLocation": source, "sourceType": "CUSTOM"}}
        ),
    }
    if namespace is not None:
        meta["namespace"] = namespace
    return {"key": f"k-{source}-{text[:6]}", "metadata": meta}


FIXTURE = [
    _vector("the build uses uv", "doc-1"),
    _vector("tests run with pytest", "doc-2"),
    _vector("part one of a long doc", "doc-3"),
    _vector("part two of a long doc", "doc-3"),
    _vector("the build uses uv", "doc-9"),  # exact duplicate text in the same scope
    _vector("prefers short answers", "doc-4", namespace="slack-user-U0AAA", created="2024-12-24T08:30:00Z"),
    _vector("likes tables", "doc-5", namespace="slack-user-U0BBB"),
    _vector("the build uses uv", "doc-6", namespace="slack-user-U0AAA"),  # same text, other scope: kept
    _vector("odd scope", "doc-7", namespace="someone-else"),
    {"key": "no-meta"},
    _vector("", "doc-8"),
]


def test_the_plan_maps_scopes_drops_duplicates_and_rejects_the_unreadable():
    plan = mig.build_plan(FIXTURE)
    assert plan.vectors == len(FIXTURE)
    assert plan.per_namespace() == {
        "/facts/team/": 4,
        "/facts/slack-user-U0AAA/": 2,
        "/facts/slack-user-U0BBB/": 1,
    }
    assert plan.duplicates == 1
    assert plan.multi_chunk_documents == 1
    assert dict(plan.rejects) == {"unknown namespace value": 1, "no metadata": 1, "no text": 1}
    personal = next(r for r in plan.records if r.text == "prefers short answers")
    assert personal.timestamp == datetime(2024, 12, 24, 8, 30, tzinfo=timezone.utc)


def test_request_ids_are_stable_whatever_the_listing_order_and_fit_the_api():
    import re

    a = {r.request_id for r in mig.build_plan(FIXTURE).records}
    b = {r.request_id for r in mig.build_plan(list(reversed(FIXTURE))).records}
    assert a == b and len(a) == 7
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", rid) for rid in a)


def test_an_over_long_chunk_is_split():
    plan = mig.build_plan([_vector("z" * (mig.RECORD_MAX_CHARS + 10), "doc-big")])
    assert plan.splits == 1 and [len(r.text) for r in plan.records] == [mig.RECORD_MAX_CHARS, 10]
    assert len({r.request_id for r in plan.records}) == 2


def test_apply_writes_every_record_in_batches_then_verifies(capsys):
    memory = FakeAgentCoreClient()
    plan = mig.build_plan(FIXTURE * 1 + [_vector(f"fact {i}", f"bulk-{i}") for i in range(150)])
    result = mig.apply(plan, memory, MEMORY_ID, pause=0, sleep=lambda _s: None, out=lambda _l: None)
    assert result.written == len(plan.records) and result.failed == []
    calls = [p for op, p in memory.calls if op == "BatchCreateMemoryRecords"]
    assert all(len(p["records"]) <= 100 for p in calls) and all(p["clientToken"] for p in calls)
    rec = next(r for r in memory.records if r["content"]["text"] == "prefers short answers")
    assert rec["namespaces"] == ["/facts/slack-user-U0AAA/"]
    assert rec["metadata"]["origin"] == {"stringValue": "kb-migration"}
    assert mig.verify(plan, memory, MEMORY_ID, sleep=lambda _s: None, out=lambda _l: None) is True


def test_a_rerun_resumes_and_writes_nothing_twice():
    memory = FakeAgentCoreClient()
    plan = mig.build_plan(FIXTURE)
    half = mig.Plan(records=plan.records[:3])
    mig.apply(half, memory, MEMORY_ID, pause=0, sleep=lambda _s: None, out=lambda _l: None)
    again = mig.apply(plan, memory, MEMORY_ID, pause=0, sleep=lambda _s: None, out=lambda _l: None)
    assert again.skipped == 3 and again.written == len(plan.records) - 3
    assert len(memory.records) == len(plan.records)


def test_throttling_backs_off_and_lands():
    memory = FakeAgentCoreClient()
    memory.fail("BatchCreateMemoryRecords", THROTTLED, times=3)
    pauses: list[float] = []
    plan = mig.build_plan(FIXTURE)
    result = mig.apply(plan, memory, MEMORY_ID, pause=0, sleep=pauses.append, out=lambda _l: None)
    assert result.written == len(plan.records)
    assert pauses[:3] == [0.5, 1.0, 2.0]


def test_verify_reports_missing_records():
    memory = FakeAgentCoreClient()
    plan = mig.build_plan(FIXTURE)
    lines: list[str] = []
    assert mig.verify(plan, memory, MEMORY_ID, sleep=lambda _s: None, out=lines.append) is False
    assert any("missing 4" in line for line in lines)


def test_the_cli_defaults_to_a_dry_run_that_writes_nothing(tmp_path, capsys):
    dump = tmp_path / "dump.json"
    dump.write_text(json.dumps({"vectors": FIXTURE}))
    code = mig.main(["--from-file", str(dump)])
    out = capsys.readouterr().out
    assert code == 1  # rejects are reported as a non-zero exit, so an operator looks at them
    assert "records to write:      7" in out and "dry run: nothing was written" in out
    assert "U0AAA" not in out  # personal namespaces masked by default


def test_apply_needs_a_memory_id_and_explicit_confirmation(tmp_path, capsys):
    dump = tmp_path / "dump.json"
    dump.write_text(json.dumps({"vectors": FIXTURE[:2]}))
    assert mig.main(["--from-file", str(dump), "--apply"]) == 2
    assert mig.main(["--from-file", str(dump), "--apply", "--memory-id", MEMORY_ID]) == 2
    assert "--yes" in capsys.readouterr().err


def test_the_cli_applies_through_the_session_it_is_given(tmp_path):
    dump = tmp_path / "dump.json"
    dump.write_text(json.dumps([{"vectors": FIXTURE[:2]}, {"vectors": FIXTURE[2:4]}]))  # two pages
    memory = FakeAgentCoreClient()

    class _Session:
        def client(self, name: str) -> Any:
            assert name == "bedrock-agentcore"
            return memory

    code = mig.main(["--from-file", str(dump), "--apply", "--memory-id", MEMORY_ID, "--yes"], session=_Session())
    assert code == 0 and len(memory.records) == 4


def test_reading_the_index_pages_and_asks_for_metadata():
    calls: list[dict] = []

    class _S3Vectors:
        def list_vectors(self, **params: Any) -> dict:
            calls.append(params)
            if "nextToken" not in params:
                return {"vectors": FIXTURE[:2], "nextToken": "t2"}
            return {"vectors": FIXTURE[2:3]}

    got = list(mig.vectors_from_index(_S3Vectors(), "bucket", "index"))
    assert len(got) == 3 and calls[0]["returnMetadata"] is True and calls[1]["nextToken"] == "t2"


@pytest.mark.parametrize("shape", ["page", "pages", "list", "jsonl"])
def test_every_dump_shape_loads(tmp_path, shape):
    path = tmp_path / "d.json"
    if shape == "page":
        path.write_text(json.dumps({"vectors": FIXTURE}))
    elif shape == "pages":
        path.write_text(json.dumps([{"vectors": FIXTURE[:5]}, {"vectors": FIXTURE[5:]}]))
    elif shape == "list":
        path.write_text(json.dumps([v for v in FIXTURE if "metadata" in v] + [FIXTURE[9]]))
    else:
        path.write_text("\n".join(json.dumps({"vectors": [v]}) for v in FIXTURE))
    assert len(list(mig.vectors_from_file(path))) == len(FIXTURE)
