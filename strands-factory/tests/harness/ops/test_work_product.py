"""Work-product store tests — hermetic (no AWS): a fake DynamoDB Table captures the writes.

The fake understands just enough of the resource ``Table`` surface the store uses: ``put_item``,
``get_item``, and ``query`` over the two GSIs (parsing the boto3 KeyCondition to find the partition
value, then sorting newest-first on the index's sort key). boto3 is imported lazily by the store's
query methods; it is a normal (installed) dependency here.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from strandly.core.config import Config
from strandly.ops.work_product import (
    KIND_REVIEW,
    KIND_TRIAGE,
    STATUS_CURATED,
    STATUS_DRAFT,
    Finding,
    WorkProduct,
    WorkProductStore,
)

# GSI name → its sort-key attribute, so the fake can order a query newest-first like DynamoDB would.
_SORT_KEY = {"by_status": "updated_at", "by_session": "created_at"}


class FakeTable:
    """In-memory stand-in for a boto3 DynamoDB ``Table`` resource."""

    def __init__(self, raise_on_put: bool = False):
        self.items: dict[str, dict[str, Any]] = {}
        self.raise_on_put = raise_on_put

    def put_item(self, *, Item: dict[str, Any]) -> None:  # noqa: N803 - boto3 kwarg name
        if self.raise_on_put:
            raise RuntimeError("dynamo unavailable")
        self.items[Item["work_product_id"]] = Item

    def get_item(self, *, Key: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        item = self.items.get(Key["work_product_id"])
        return {"Item": item} if item else {}

    def query(self, *, IndexName: str, KeyConditionExpression: Any,  # noqa: N803
              ScanIndexForward: bool = True, Limit: int = 100) -> dict[str, Any]:  # noqa: N803
        expr = KeyConditionExpression.get_expression()
        key, value = expr["values"]
        rows = [i for i in self.items.values() if i.get(key.name) == value]
        sort_attr = _SORT_KEY[IndexName]
        rows.sort(key=lambda i: i.get(sort_attr, ""), reverse=not ScanIndexForward)
        return {"Items": rows[:Limit]}


def _store(**kw: Any) -> tuple[WorkProductStore, FakeTable]:
    table = FakeTable(**kw)
    return WorkProductStore("strandly-wp", region="us-west-2", table=table), table


def _review(session_id: str = "gh-strands-agents-harness-sdk-pr-42", **kw: Any) -> WorkProduct:
    return WorkProduct(
        session_id=session_id, kind=KIND_REVIEW, verdict="changes-requested",
        repo="strands-agents/harness-sdk", number=42, tldr="one real bug",
        findings=[
            Finding(id="f1", file="a.py", line=10, tier="block", category="correctness",
                    confidence=0.9, text="off-by-one", suggestion="use <="),
            Finding(id="f2", file="b.py", line=3, tier="nit", category="style",
                    confidence=0.4, text="rename", suppressed=True, suppress_reason="linter"),
        ],
        evidence_ledger={"branch_sha": "abc123", "tests_passed": 12},
        **kw,
    )


def test_from_config_disabled_returns_none():
    assert WorkProductStore.from_config(Config(values={})) is None


def test_from_config_enabled_returns_store():
    cfg = Config(values={"STRANDLY_WORK_PRODUCT_TABLE": "t", "AWS_REGION": "us-west-2"})
    store = WorkProductStore.from_config(cfg)
    assert isinstance(store, WorkProductStore)
    assert store.table_name == "t" and store.region == "us-west-2"


def test_put_get_round_trip_preserves_findings_and_confidence():
    store, _ = _store()
    wp = _review()
    store.put(wp)
    got = store.get(wp.id)
    assert got is not None
    assert got.kind == KIND_REVIEW and got.verdict == "changes-requested"
    assert got.number == 42 and got.tldr == "one real bug"
    assert [f.id for f in got.findings] == ["f1", "f2"]
    f1, f2 = got.findings
    assert f1.confidence == 0.9 and f1.line == 10 and f1.included is True
    assert f2.suppressed is True and f2.suppress_reason == "linter" and f2.included is False
    assert got.evidence_ledger == {"branch_sha": "abc123", "tests_passed": 12}


def test_confidence_stored_as_decimal_not_float():
    store, table = _store()
    wp = _review()
    store.put(wp)
    stored = table.items[wp.id]
    assert isinstance(stored["findings"][0]["confidence"], Decimal)


def test_get_missing_returns_none():
    store, _ = _store()
    assert store.get("nope") is None


def test_query_by_session_returns_newest_first():
    store, _ = _store()
    older = _review(created_at="2026-01-01T00:00:00+00:00")
    newer = _review(created_at="2026-02-01T00:00:00+00:00")
    store.put(older)
    store.put(newer)
    ids = [wp.id for wp in store.query_by_session(older.session_id)]
    assert ids == [newer.id, older.id]


def test_query_drafts_only_returns_drafts_and_filters_by_kind():
    store, _ = _store()
    review = _review()
    triage = WorkProduct(session_id="gh-o-r-issue-9", kind=KIND_TRIAGE, verdict="accept")
    posted = _review(session_id="gh-o-r-pr-7", status="posted")
    store.put(review)
    store.put(triage)
    store.put(posted)
    all_drafts = {wp.id for wp in store.query_drafts()}
    assert all_drafts == {review.id, triage.id}  # posted excluded
    review_only = [wp.id for wp in store.query_drafts(kind=KIND_REVIEW)]
    assert review_only == [review.id]


def test_mark_curated_sets_included_flags_and_status():
    store, _ = _store()
    wp = _review()
    store.put(wp)
    updated = store.mark_curated(wp.id, included_finding_ids=["f2"], curated_by="example-contributor")
    assert updated is not None
    assert updated.status == STATUS_CURATED and updated.curated_by == "example-contributor"
    by_id = {f.id: f.included for f in updated.findings}
    assert by_id == {"f1": False, "f2": True}  # only f2 kept
    # persisted
    reread = store.get(wp.id)
    assert reread is not None and reread.status == STATUS_CURATED


def test_mark_curated_missing_returns_none():
    store, _ = _store()
    assert store.mark_curated("nope", included_finding_ids=[]) is None


def test_mark_curated_rejects_invalid_status():
    import pytest

    store, table = _store()
    wp = _review()
    store.put(wp)
    # An arbitrary status would drop the row from every by_status view — the store rejects it.
    with pytest.raises(ValueError):
        store.mark_curated(wp.id, status="banana")
    # The stored row is untouched (still a draft).
    reread = store.get(wp.id)
    assert reread is not None and reread.status == STATUS_DRAFT


def test_mark_curated_without_finding_ids_leaves_flags():
    store, _ = _store()
    wp = _review()
    store.put(wp)
    updated = store.mark_curated(wp.id)  # verdict-only curation
    assert updated is not None
    by_id = {f.id: f.included for f in updated.findings}
    assert by_id == {"f1": True, "f2": False}  # untouched defaults


def test_mark_curated_persists_edited_tldr_verdict_and_finding_text():
    store, _ = _store()
    wp = _review()
    store.put(wp)
    updated = store.mark_curated(
        wp.id,
        included_finding_ids=["f1"],
        curated_by="example-contributor",
        tldr="sharpened headline",
        verdict="approve",
        finding_text_overrides={"f1": "edited body"},
    )
    assert updated is not None
    assert updated.tldr == "sharpened headline" and updated.verdict == "approve"
    f1 = next(f for f in updated.findings if f.id == "f1")
    assert f1.text == "edited body" and f1.included is True
    # persisted through to_item / from_item
    reread = store.get(wp.id)
    assert reread is not None
    assert reread.tldr == "sharpened headline" and reread.verdict == "approve"
    assert next(f for f in reread.findings if f.id == "f1").text == "edited body"


def test_suspected_area_round_trips():
    store, _ = _store()
    wp = WorkProduct(session_id="gh-o-r-issue-9", kind="first-response", verdict="confirmed-bug",
                     suspected_area="src/x.py:212")
    store.put(wp)
    reread = store.get(wp.id)
    assert reread is not None and reread.suspected_area == "src/x.py:212"


def test_put_is_fail_open():
    """A DynamoDB error on write must be swallowed — telemetry can't break a run."""
    store, _ = _store(raise_on_put=True)
    store.put(_review())  # must not raise


def test_status_defaults_to_draft():
    wp = WorkProduct(session_id="gh-o-r-pr-1", kind=KIND_REVIEW)
    assert wp.status == STATUS_DRAFT
    assert wp.to_item()["status"] == STATUS_DRAFT


def test_lazy_table_not_built_on_construction():
    store = WorkProductStore("t", region=None)  # no injected table
    assert store._table is None


def test_from_item_ignores_a_legacy_artifacts_attribute():
    """Rows written while work products carried an ``artifacts`` list still load."""
    item = WorkProduct(session_id="s", kind="review", verdict="approve").to_item()
    item["artifacts"] = ["o/r/pr/42/pytest.log"]
    back = WorkProduct.from_item(item)
    assert back.verdict == "approve"
    assert "artifacts" not in back.to_item()


# --- model-output tolerance (prod 2026-09-25: "confidence": "high" failed a completed run) ------


def test_finding_from_item_maps_word_confidence():
    assert Finding.from_item({"confidence": "high"}).confidence == 0.9
    assert Finding.from_item({"confidence": "Medium"}).confidence == 0.6
    assert Finding.from_item({"confidence": " low "}).confidence == 0.3


def test_finding_from_item_numeric_and_percent_confidence():
    assert Finding.from_item({"confidence": 0.75}).confidence == 0.75
    assert Finding.from_item({"confidence": Decimal("0.4")}).confidence == 0.4
    assert Finding.from_item({"confidence": "0.8"}).confidence == 0.8
    assert Finding.from_item({"confidence": "85%"}).confidence == 0.85
    assert Finding.from_item({"confidence": 85}).confidence == 0.85
    assert Finding.from_item({"confidence": 1}).confidence == 1.0
    assert Finding.from_item({"confidence": -2}).confidence == 0.0


def test_finding_from_item_unreadable_confidence_is_none_not_an_error():
    for bad in ("dunno", "", [], {}, True, float("nan")):
        assert Finding.from_item({"confidence": bad}).confidence is None
    assert Finding.from_item({}).confidence is None


def test_finding_from_item_tolerant_line():
    assert Finding.from_item({"line": 88}).line == 88
    assert Finding.from_item({"line": "88"}).line == 88
    assert Finding.from_item({"line": "88-90"}).line == 88
    assert Finding.from_item({"line": "L12"}).line == 12
    assert Finding.from_item({"line": Decimal("7")}).line == 7
    assert Finding.from_item({"line": "n/a"}).line is None
    assert Finding.from_item({}).line is None


def test_word_confidence_round_trips_through_dynamo_item():
    f = Finding.from_item({"id": "f1", "confidence": "high"})
    assert Finding.from_item(f.to_item()).confidence == 0.9
