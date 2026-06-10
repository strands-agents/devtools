"""Tests for the dynamic allowlist model in classify.py.

These cover the security-critical piece: the classification schema must only
accept labels from the configured allowlist. No Bedrock call is made.
"""

import pytest
from pydantic import ValidationError

from classify import build_classification_model, build_system_prompt, sanitize


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
