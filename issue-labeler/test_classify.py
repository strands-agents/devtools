"""Tests for the dynamic allowlist model in classify.py.

These cover the security-critical piece: the classification schema must only
accept labels from the configured allowlist. No Bedrock call is made.
"""

import pytest
from pydantic import ValidationError

import classify
from classify import (
    build_classification_model,
    build_system_prompt,
    parse_field_config,
    parse_label_type_map,
    parse_native_ids,
    sanitize,
    select_option,
    select_type,
)


def test_accepts_allowlisted_labels():
    model = build_classification_model(frozenset({"bug", "enhancement"}))
    obj = model(labels=["bug", "enhancement"])
    assert [m.value for m in obj.labels] == ["bug", "enhancement"]


def test_rejects_out_of_allowlist_label():
    model = build_classification_model(frozenset({"bug"}))
    with pytest.raises(ValidationError):
        model(labels=["bug", "not-a-real-label"])


def test_defaults_to_empty_list():
    model = build_classification_model(frozenset({"bug"}))
    assert model().labels == []


def test_system_prompt_lists_labels_and_cap():
    config = {"labels": {"bug": "broken", "enhancement": {"description": "new"}}}
    prompt = build_system_prompt(config, max_labels=2)
    assert "- bug: broken" in prompt
    assert "- enhancement: new" in prompt
    assert "at most 2 labels" in prompt


def test_sanitize_strips_control_chars_and_truncates():
    assert sanitize("a\x00b\x1fc", 10) == "abc"
    assert sanitize("abcdef", 3) == "abc"


def test_parse_label_type_map_extracts_only_typed_labels():
    config = {
        "labels": {
            "bug": {"description": "broken", "type": "Bug"},
            "enhancement": {"description": "new", "type": "Feature"},
            "question": {"description": "asking"},      # no type -> omitted
            "area-tool": "shorthand string",            # shorthand -> omitted
        }
    }
    assert parse_label_type_map(config) == {"bug": "Bug", "enhancement": "Feature"}


def test_parse_label_type_map_empty_when_none_typed():
    config = {"labels": {"python": {"description": "py"}, "ts": "shorthand"}}
    assert parse_label_type_map(config) == {}


def test_parse_field_config_none_when_absent():
    config = {"labels": {"bug": {"description": "x"}}}
    assert parse_field_config(config) is None


def test_parse_field_config_extracts_name_and_option_map():
    config = {
        "field": {"name": "Language"},
        "labels": {
            "python": {"description": "py", "option": "Python"},
            "typescript": {"description": "ts", "option": "TypeScript"},
            "other": {"description": "no option"},      # omitted from option_map
        },
    }
    assert parse_field_config(config) == {
        "name": "Language",
        "option_map": {"python": "Python", "typescript": "TypeScript"},
    }


_SAMPLE_GRAPHQL_DATA = {
    "repository": {
        "issueTypes": {"nodes": [
            {"id": "IT_bug", "name": "Bug"},
            {"id": "IT_feature", "name": "Feature"},
        ]},
        "issueFields": {"nodes": [
            {"__typename": "IssueFieldSingleSelect", "id": "IFSS_lang", "name": "Language",
             "options": [
                 {"id": "OPT_py", "name": "Python"},
                 {"id": "OPT_ts", "name": "TypeScript"},
             ]},
            {"__typename": "IssueFieldDate", "id": "IFD_x", "name": "Target date"},
        ]},
    }
}


def test_parse_native_ids_indexes_types_and_single_select_fields():
    ids = parse_native_ids(_SAMPLE_GRAPHQL_DATA)
    assert ids["types"] == {"bug": "IT_bug", "feature": "IT_feature"}
    assert ids["fields"]["language"]["id"] == "IFSS_lang"
    assert ids["fields"]["language"]["options"] == {"python": "OPT_py", "typescript": "OPT_ts"}
    # non-single-select fields are skipped
    assert "target date" not in ids["fields"]


def test_select_type_returns_first_mapped_label():
    type_map = {"bug": "Bug", "enhancement": "Feature"}
    assert select_type(["enhancement"], type_map) == "Feature"
    assert select_type(["question"], type_map) is None
    assert select_type([], type_map) is None


def test_select_option_returns_first_mapped_label():
    option_map = {"python": "Python", "typescript": "TypeScript"}
    assert select_option(["typescript"], option_map) == "TypeScript"
    assert select_option(["rust"], option_map) is None


def test_apply_native_targets_sets_type_and_field(monkeypatch):
    calls = {}
    monkeypatch.setattr(classify, "resolve_native_ids", lambda repo: {
        "types": {"bug": "IT_bug", "feature": "IT_feature"},
        "fields": {"language": {"id": "IFSS_lang", "options": {"python": "OPT_py"}}},
    })
    monkeypatch.setattr(classify, "get_issue_node_id", lambda n, repo: "ISSUE_NODE")
    monkeypatch.setattr(classify, "set_issue_type", lambda node, tid: calls.__setitem__("type", (node, tid)))
    monkeypatch.setattr(classify, "set_issue_field", lambda node, fid, oid: calls.__setitem__("field", (node, fid, oid)))

    classify.apply_native_targets(
        "12", "o/r",
        labels=["bug", "python"],
        type_map={"bug": "Bug", "enhancement": "Feature"},
        field_config={"name": "Language", "option_map": {"python": "Python"}},
    )
    assert calls["type"] == ("ISSUE_NODE", "IT_bug")
    assert calls["field"] == ("ISSUE_NODE", "IFSS_lang", "OPT_py")


def test_apply_native_targets_warns_and_skips_unknown_name(monkeypatch, capsys):
    monkeypatch.setattr(classify, "resolve_native_ids", lambda repo: {"types": {}, "fields": {}})
    monkeypatch.setattr(classify, "get_issue_node_id", lambda n, repo: "ISSUE_NODE")
    monkeypatch.setattr(classify, "set_issue_type", lambda node, tid: pytest.fail("should not set"))
    monkeypatch.setattr(classify, "set_issue_field", lambda node, fid, oid: pytest.fail("should not set"))

    classify.apply_native_targets(
        "12", "o/r", labels=["bug"],
        type_map={"bug": "Bug"}, field_config=None,
    )
    assert "::warning::" in capsys.readouterr().out


def test_apply_native_targets_noops_when_no_mapping_matches(monkeypatch):
    monkeypatch.setattr(classify, "resolve_native_ids", lambda repo: pytest.fail("should not resolve"))
    # No type_map and no field_config -> nothing to do, must not even resolve.
    classify.apply_native_targets("12", "o/r", labels=["bug"], type_map={}, field_config=None)


def test_apply_native_targets_uses_provided_native_without_resolving(monkeypatch):
    calls = {}
    monkeypatch.setattr(classify, "resolve_native_ids", lambda repo: pytest.fail("should not resolve when native provided"))
    monkeypatch.setattr(classify, "get_issue_node_id", lambda n, repo: "ISSUE_NODE")
    monkeypatch.setattr(classify, "set_issue_type", lambda node, tid: calls.__setitem__("type", (node, tid)))
    monkeypatch.setattr(classify, "set_issue_field", lambda node, fid, oid: pytest.fail("no field expected"))
    classify.apply_native_targets(
        "12", "o/r", labels=["bug"], type_map={"bug": "Bug"}, field_config=None,
        native={"types": {"bug": "IT_bug"}, "fields": {}},
    )
    assert calls["type"] == ("ISSUE_NODE", "IT_bug")


def test_main_native_block_failure_does_not_fail_workflow(monkeypatch, tmp_path, capsys):
    """The additive native-targets block must never fail the run: a raising
    apply_native_targets is downgraded to a warning, and main() still exits 0
    after writing its label output."""
    config = tmp_path / "type.yml"
    config.write_text('labels:\n  bug:\n    description: "broken"\n    type: Bug\n')
    output = tmp_path / "gh_output"

    monkeypatch.setenv("CONFIG_PATH", str(config))
    monkeypatch.setenv("GH_REPO", "o/r")
    monkeypatch.setenv("ISSUE_TITLE", "something broke")
    monkeypatch.setenv("ISSUE_BODY", "details")
    monkeypatch.setenv("ISSUE_NUMBER", "7")
    monkeypatch.setenv("EVENT_NAME", "issues")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    monkeypatch.setattr(classify, "classify_issue", lambda *a, **k: ["bug"])
    monkeypatch.setattr(classify, "apply_labels", lambda n, labels: None)

    def boom(*a, **k):
        raise RuntimeError("simulated API/permission failure")

    monkeypatch.setattr(classify, "apply_native_targets", boom)

    # Must return normally (no exception, no non-zero SystemExit).
    classify.main()

    out = capsys.readouterr().out
    assert "::warning::Native type/field update skipped" in out
    assert "labels=bug" in output.read_text()
