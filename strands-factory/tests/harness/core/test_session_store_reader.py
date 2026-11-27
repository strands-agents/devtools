"""The strands-free session-store reader over S3 (fake client) and the local store dir."""

from __future__ import annotations

import io
import json

import pytest

from strandly.core.session_store_reader import (
    SessionStoreReader,
    conversation_from_messages,
    message_text,
    settled,
    snapshot_key,
)


class _NoSuchKey(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class _Denied(Exception):
    response = {"Error": {"Code": "AccessDenied"}}


class FakeS3:
    def __init__(self, objects: dict[str, bytes], *, page: int = 2) -> None:
        self.objects = objects
        self.page = page
        self.list_calls = 0

    def get_object(self, Bucket, Key):  # noqa: N803 — boto3's casing
        if Key == "denied":
            raise _Denied()
        if Key not in self.objects:
            raise _NoSuchKey()
        return {"Body": io.BytesIO(self.objects[Key])}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):  # noqa: N803
        self.list_calls += 1
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        chunk = keys[start : start + self.page]
        resp = {"Contents": [{"Key": k} for k in chunk]}
        if start + self.page < len(keys):
            resp["NextContinuationToken"] = str(start + self.page)
        return resp


def _snapshot(messages):
    return json.dumps({"scope": "agent", "schema_version": "1.0", "data": {"messages": messages}}).encode()


def test_snapshot_messages_from_s3_and_missing_is_none():
    msgs = [{"role": "user", "content": [{"text": "hi"}]}]
    s3 = FakeS3({snapshot_key("gh/o/r/1"): _snapshot(msgs)})
    reader = SessionStoreReader("bucket", client=s3)
    assert reader.snapshot_messages("gh/o/r/1") == msgs
    assert reader.snapshot_messages("nope") is None


def test_errors_other_than_missing_propagate():
    reader = SessionStoreReader("bucket", client=FakeS3({}))
    with pytest.raises(_Denied):
        reader.read("denied")


def test_local_store_dir_reads_what_local_file_storage_writes(tmp_path):
    path = tmp_path / snapshot_key("s-1")
    path.parent.mkdir(parents=True)
    path.write_bytes(_snapshot([{"role": "assistant", "content": [{"text": "yo"}]}]))
    reader = SessionStoreReader(local_dir=str(tmp_path))
    assert reader.conversation("s-1") == [("assistant", "yo", False)]
    assert reader.conversation("s-2") is None


def test_message_text_and_settle():
    narration = {"role": "assistant", "content": [{"text": "cloning"}, {"toolUse": {"name": "x"}}]}
    answer = {"role": "assistant", "content": [{"text": "final"}]}
    tool_result = {"role": "user", "content": [{"toolResult": {"toolUseId": "t", "content": []}}]}
    assert message_text(narration) == ("assistant", "cloning", True)
    assert message_text("junk") == ("", "", False)
    assert settled(conversation_from_messages([narration])) is False
    assert settled(conversation_from_messages([narration, tool_result])) is False
    assert settled(conversation_from_messages([narration, tool_result, answer])) is True
