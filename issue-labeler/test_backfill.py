import sys

import backfill
import pytest

from backfill import decide_label


def test_decide_label_reuses_existing_without_classifying():
    def classify_fn():
        raise AssertionError("must not classify when a label already exists")
    assert decide_label(["bug", "area-tool"], {"bug", "enhancement"}, classify_fn) == "bug"


def test_decide_label_classifies_when_missing():
    assert decide_label(["area-tool"], {"bug", "enhancement"}, lambda: ["enhancement"]) == "enhancement"


def test_decide_label_none_when_classify_empty():
    assert decide_label([], {"bug"}, lambda: []) is None


def test_dry_run_does_not_mutate(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "type.yml"
    cfg.write_text('labels:\n  bug:\n    description: "broken"\n    type: Bug\n')
    monkeypatch.setattr(sys, "argv", ["backfill.py", "--repo", "o/r", "--type-config", str(cfg), "--dry-run"])
    monkeypatch.setattr(backfill, "list_issues", lambda repo, state: [
        {"number": 1, "title": "t", "body": "b", "label_names": ["bug"]},
    ])
    monkeypatch.setattr(backfill, "resolve_native_ids", lambda repo: {"types": {}, "fields": {}})
    monkeypatch.setattr(backfill, "apply_native_targets", lambda *a, **k: pytest.fail("must not mutate in dry-run"))
    backfill.main()
    out = capsys.readouterr().out
    assert "would apply" in out
    assert "#1" in out
