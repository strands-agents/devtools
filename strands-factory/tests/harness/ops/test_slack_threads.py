"""Home-channel thread links: the shared run-ledger row shape and its fail-open reads and writes."""

from __future__ import annotations

from typing import Any

from strandly.ops import slack


class _Client:
    def __init__(self, item: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.item, self.error, self.calls = item, error, []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"Item": self.item} if self.item else {}

    def put_item(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.error:
            raise self.error


def test_item_is_clipped_has_no_gsi_key_and_drops_empty_fields():
    item = slack.thread_link_item(
        channel_id="C0HOME", thread_ts="1.2", title="t" * 500, details="d" * 9000
    )
    assert item["task_id"] == "slack-thread#C0HOME#1.2"
    assert len(item["title"]) == slack.THREAD_LINK_TITLE_MAX
    assert len(item["details"]) == slack.THREAD_LINK_DETAILS_MAX
    assert "gsi_pk" not in item and "origin" not in item


def test_read_round_trips_a_written_link():
    item = slack.thread_link_item(channel_id="C0HOME", thread_ts="1.2", title="t", details="d")
    client = _Client(slack.to_attribute_values(item))
    assert slack.read_thread_link(client, "ledger", "C0HOME", "1.2") == item
    assert client.calls[0]["Key"] == {"task_id": {"S": "slack-thread#C0HOME#1.2"}}


def test_read_is_none_when_unconfigured_malformed_missing_or_failing():
    assert slack.read_thread_link(None, "ledger", "C0HOME", "1.2") is None
    assert slack.read_thread_link(_Client(), "", "C0HOME", "1.2") is None
    client = _Client()
    assert slack.read_thread_link(client, "ledger", "C0HOME", "../x") is None and client.calls == []
    assert slack.read_thread_link(_Client(), "ledger", "C0HOME", "1.2") is None
    assert slack.read_thread_link(_Client(error=RuntimeError("denied")), "ledger", "C0HOME", "1.2") is None


def test_client_write_keeps_an_existing_link_and_never_raises():
    client = _Client()
    item = slack.thread_link_item(channel_id="C0HOME", thread_ts="1.2", title="t", details="d")
    assert slack.put_thread_link_client(client, "ledger", item) is True
    assert client.calls[0]["ConditionExpression"] == "attribute_not_exists(task_id)"
    assert slack.put_thread_link_client(_Client(error=RuntimeError("x")), "ledger", item) is False
    assert slack.put_thread_link_client(None, "ledger", item) is False
