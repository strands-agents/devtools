"""Tests for the dynamic allowlist model in classify.py.

These cover the security-critical piece: the classification schema must only
accept labels from the configured allowlist. No Bedrock call is made.
"""

import pytest
from pydantic import ValidationError

from classify import build_classification_model, build_system_prompt, parse_field_config, parse_label_type_map, sanitize


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
