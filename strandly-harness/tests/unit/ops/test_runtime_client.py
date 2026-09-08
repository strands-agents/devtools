from __future__ import annotations

from strandly_harness.ops.runtime_client import _parse_sse


def test_parse_sse_filters_and_decodes_json():
    lines = [
        b"data: {\"kind\": \"text\", \"text\": \"hi\"}",
        b"",  # blank line between SSE frames
        b": comment line ignored",
        b"data: {\"kind\": \"done\"}",
    ]
    events = list(_parse_sse(iter(lines)))
    assert events == [{"kind": "text", "text": "hi"}, {"kind": "done"}]


def test_parse_sse_non_json_data_becomes_text():
    events = list(_parse_sse(iter(["data: plain words"])))
    assert events == [{"kind": "text", "text": "plain words"}]
