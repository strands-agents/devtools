"""Hermetic unit tests for the AWS mention poller (ingress). No live AWS / GitHub — everything is
mocked at the ``_request`` (urllib) seam, the ``launch_run`` dispatch seam, and a fake DynamoDB
client. The suite is hermetic w.r.t. ``GITHUB_CONTEXT`` (popped by the autouse conftest fixture)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import pytest

from strandly.core.config import Config, MentionPollerSettings
from strandly.ops.lambdas.mention_poller import dedup, mention_log
from strandly.ops.lambdas.mention_poller import handler as mentions

NOW = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)


def _settings(**kw: Any) -> MentionPollerSettings:
    base = dict(
        handle="example-user",
        allowed_authors=("example-maintainer", "alice"),
        skip_repo="example-user/example-operator-repo",
        runtime_arn="arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/strandly-abc",
        region="us-west-2",
        dedup_table=None,
        managed_owners=("ext", "example-user"),
    )
    base.update(kw)
    return MentionPollerSettings(**base)


@pytest.fixture(autouse=True)
def _clear_org_member_cache():
    """The org-membership cache is module-global; clear it around every test for isolation."""
    mentions._ORG_MEMBER_CACHE.clear()
    yield
    mentions._ORG_MEMBER_CACHE.clear()


# ---------------------------------------------------------------------------
# Step 2: notification filtering
# ---------------------------------------------------------------------------
def test_mention_notifications_filters_reason():
    notifs = [
        {"id": "1", "reason": "mention"},
        {"id": "2", "reason": "team_mention"},
        {"id": "3", "reason": "subscribed"},
        {"id": "4", "reason": "author"},
        {},
    ]
    kept = mentions.mention_notifications(notifs)
    assert [n["id"] for n in kept] == ["1", "2"]


# ---------------------------------------------------------------------------
# Step 3: multi-location mention search + author/timestamp extraction
# ---------------------------------------------------------------------------
def test_select_mention_in_body():
    content = {
        "body": "hey @example-user please review",
        "user": {"login": "example-maintainer"},
        "updated_at": "2026-06-26T10:00:00Z",
        "created_at": "2026-06-26T09:00:00Z",
    }
    m = mentions.select_mention(
        content=content,
        comments=[],
        reviews=[],
        review_comments=[],
        handle="example-user",
        is_pull_request=False,
    )
    assert m is not None
    assert m.author == "example-maintainer" and m.source == "body"
    # created_at, NOT updated_at: a PR/issue's updated_at is bumped by ANY activity (comments,
    # labels), which would make the body perpetually "fresh" and shadow real follow-up comments.
    assert m.timestamp == "2026-06-26T09:00:00Z"


def test_select_mention_picks_latest_comment_and_is_case_insensitive():
    comments = [
        {
            "body": "@example-user old",
            "user": {"login": "alice"},
            "updated_at": "2026-06-26T08:00:00Z",
        },
        {"body": "no mention here", "user": {"login": "bob"}, "updated_at": "2026-06-26T11:00:00Z"},
        {
            "body": "@example-user new",
            "user": {"login": "example-maintainer"},
            "updated_at": "2026-06-26T10:00:00Z",
        },
    ]
    m = mentions.select_mention(
        content={"body": ""},
        comments=comments,
        reviews=[],
        review_comments=[],
        handle="example-user",
        is_pull_request=False,
    )
    assert m is not None and m.source == "comment"
    assert m.author == "example-maintainer" and m.timestamp == "2026-06-26T10:00:00Z"  # latest matching


def test_select_mention_newest_wins_across_locations():
    # Newest-wins (not precedence-wins): a newer follow-up comment must beat an older body mention,
    # otherwise the body would shadow the follow-up and the follow-up would test as stale → dropped.
    content = {
        "body": "@example-user in body",
        "user": {"login": "example-maintainer"},
        "updated_at": "2026-06-26T09:00:00Z",
    }
    comments = [
        {
            "body": "@example-user follow-up please",
            "user": {"login": "alice"},
            "updated_at": "2026-06-26T11:00:00Z",
        }
    ]
    m = mentions.select_mention(
        content=content,
        comments=comments,
        reviews=[],
        review_comments=[],
        handle="example-user",
        is_pull_request=True,
    )
    assert m is not None and m.source == "comment"
    assert m.author == "alice" and m.timestamp == "2026-06-26T11:00:00Z"


def test_select_mention_body_wins_when_newest():
    # When the body mention is genuinely the newest (by created_at), it still wins.
    content = {
        "body": "@example-user in body",
        "user": {"login": "example-maintainer"},
        "created_at": "2026-06-26T12:00:00Z",
    }
    comments = [
        {
            "body": "@example-user earlier",
            "user": {"login": "alice"},
            "updated_at": "2026-06-26T11:00:00Z",
        }
    ]
    m = mentions.select_mention(
        content=content,
        comments=comments,
        reviews=[],
        review_comments=[],
        handle="example-user",
        is_pull_request=True,
    )
    assert m is not None and m.source == "body" and m.timestamp == "2026-06-26T12:00:00Z"


def test_select_mention_body_updated_at_does_not_shadow_comment():
    # Regression (prod, devtools#75): a PR whose BODY merely links @handle, plus a real follow-up
    # COMMENT asking for a review. GitHub bumps the PR's updated_at to the comment's time on every
    # comment, so keying the body off updated_at made it tie the comment and win by precedence —
    # attributing the mention to the PR *opener* (unauthorized) instead of the commenter. The body
    # must be keyed off created_at so the genuine comment wins.
    content = {
        "body": "imports the harness. it currently powers @example-user.",
        "user": {"login": "example-user"},  # the PR opener (a bot, not on the allow-list)
        "created_at": "2026-06-26T09:00:00Z",
        "updated_at": "2026-06-26T11:00:00Z",  # bumped by the comment below
    }
    comments = [
        {
            "body": "@example-user can you review this?",
            "user": {"login": "example-maintainer"},
            "created_at": "2026-06-26T11:00:00Z",
            "updated_at": "2026-06-26T11:00:00Z",
        }
    ]
    m = mentions.select_mention(
        content=content,
        comments=comments,
        reviews=[],
        review_comments=[],
        handle="example-user",
        is_pull_request=True,
    )
    assert m is not None and m.source == "comment"
    assert m.author == "example-maintainer"  # the commenter who actually invoked the bot, not the PR opener


def test_select_mention_tie_breaks_to_precedence():
    # Equal/unparseable timestamps fall back to location precedence (body over comment).
    content = {"body": "@example-user b", "user": {"login": "example-maintainer"}, "updated_at": "t"}
    comments = [{"body": "@example-user c", "user": {"login": "alice"}, "updated_at": "t"}]
    m = mentions.select_mention(
        content=content,
        comments=comments,
        reviews=[],
        review_comments=[],
        handle="example-user",
        is_pull_request=True,
    )
    assert m is not None and m.source == "body"


def test_select_mention_in_pr_review_body():
    reviews = [
        {
            "body": "lgtm",
            "user": {"login": "x"},
            "state": "APPROVED",
            "submitted_at": "2026-06-26T08:00:00Z",
        },
        {
            "body": "@example-user fix this",
            "user": {"login": "alice"},
            "state": "CHANGES_REQUESTED",
            "submitted_at": "2026-06-26T09:00:00Z",
        },
    ]
    m = mentions.select_mention(
        content={"body": ""},
        comments=[],
        reviews=reviews,
        review_comments=[],
        handle="example-user",
        is_pull_request=True,
    )
    assert m is not None
    assert m.source == "review (CHANGES_REQUESTED)" and m.author == "alice"
    assert m.timestamp == "2026-06-26T09:00:00Z"


def test_select_mention_in_pr_line_comment():
    rc = [
        {
            "body": "@example-user here",
            "user": {"login": "example-maintainer"},
            "path": "src/app.py",
            "updated_at": "2026-06-26T09:00:00Z",
        }
    ]
    m = mentions.select_mention(
        content={"body": ""},
        comments=[],
        reviews=[],
        review_comments=rc,
        handle="example-user",
        is_pull_request=True,
    )
    assert m is not None
    assert m.source == "line comment on src/app.py" and m.author == "example-maintainer"


def test_select_mention_ignores_pr_only_locations_for_issues():
    reviews = [
        {
            "body": "@example-user",
            "user": {"login": "alice"},
            "state": "COMMENTED",
            "submitted_at": "t",
        }
    ]
    m = mentions.select_mention(
        content={"body": ""},
        comments=[],
        reviews=reviews,
        review_comments=[],
        handle="example-user",
        is_pull_request=False,
    )
    assert m is None  # an Issue never inspects review bodies


def test_select_mention_none_when_absent():
    m = mentions.select_mention(
        content={"body": "nothing"},
        comments=[{"body": "hi", "user": {"login": "x"}}],
        reviews=[],
        review_comments=[],
        handle="example-user",
        is_pull_request=True,
    )
    assert m is None


# ---------------------------------------------------------------------------
# Step 4: authorization allow-list
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "author,expected",
    [
        ("example-maintainer", True),
        ("example-maintainer", True),
        ("alice", True),
        ("eve", False),
        ("", False),
        (None, False),
    ],
)
def test_is_authorized(author, expected):
    assert _settings().is_authorized(author) is expected


# ---------------------------------------------------------------------------
# Step 4b: org-membership invoke gate (is_org_member) — all network monkeypatched
# ---------------------------------------------------------------------------
def test_is_org_member_true_on_204_first_org():
    calls: list[tuple[str, str, str]] = []

    def fake(org, login, token):
        calls.append((org, login, token))
        return 204  # member

    assert (
        mentions.is_org_member("carol", ("strands-agents", "strands-labs"), "tok", request=fake)
        is True
    )
    # First 204 short-circuits → only the first org is queried, and the token is passed through.
    assert calls == [("strands-agents", "carol", "tok")]


def test_is_org_member_checks_each_org_until_match():
    def fake(org, login, token):
        return 204 if org == "strands-labs" else 404

    assert (
        mentions.is_org_member("carol", ("strands-agents", "strands-labs"), "tok", request=fake)
        is True
    )


def test_is_org_member_false_when_404_everywhere():
    assert (
        mentions.is_org_member(
            "eve", ("strands-agents", "strands-labs"), "tok", request=lambda *a: 404
        )
        is False
    )


@pytest.mark.parametrize("status", [200, 201, 202, 301, 302, 401, 403, 500])
def test_is_org_member_fail_closed_on_non_204_status(status):
    # ADVERSARIAL: a redirect (302) or any non-204 2xx must NOT be misread as membership.
    assert (
        mentions.is_org_member("eve", ("strands-agents",), "tok", request=lambda *a: status)
        is False
    )


def test_is_org_member_fail_closed_on_request_raising():
    def boom(org, login, token):
        raise RuntimeError("network down / 403 / unparseable")

    assert mentions.is_org_member("eve", ("strands-agents",), "tok", request=boom) is False


def test_is_org_member_empty_orgs_is_false_without_request():
    # An empty allowed_orgs cleanly means "no org gating" — no network call, no raise.
    assert (
        mentions.is_org_member("eve", (), "tok", request=lambda *a: pytest.fail("no call")) is False
    )


def test_is_org_member_no_login_or_token_is_false():
    assert (
        mentions.is_org_member(
            "", ("strands-agents",), "tok", request=lambda *a: pytest.fail("no call")
        )
        is False
    )
    assert (
        mentions.is_org_member(
            None, ("strands-agents",), "tok", request=lambda *a: pytest.fail("no call")
        )
        is False
    )
    assert (
        mentions.is_org_member(
            "eve", ("strands-agents",), None, request=lambda *a: pytest.fail("no call")
        )
        is False
    )
    assert (
        mentions.is_org_member(
            "eve", ("strands-agents",), "", request=lambda *a: pytest.fail("no call")
        )
        is False
    )


def test_is_org_member_caches_within_ttl():
    calls = {"n": 0}

    def fake(org, login, token):
        calls["n"] += 1
        return 404

    orgs = ("strands-agents",)
    assert mentions.is_org_member("eve", orgs, "tok", request=fake) is False
    assert mentions.is_org_member("eve", orgs, "tok", request=fake) is False
    assert calls["n"] == 1  # second lookup served from the short-TTL cache


def test_membership_request_only_204_is_member(monkeypatch):
    # Exercise the real _membership_request seam: it returns the raw status, mapping HTTPError →
    # its code, and only an exact 204 means "member" (verified via is_org_member).
    import urllib.error

    class FakeResp:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeOpener:
        def open(self, req, timeout=None):
            # Assert the seam sends the token + targets the members endpoint.
            assert req.get_header("Authorization") == "Bearer tok"
            assert req.full_url == "https://api.github.com/orgs/strands-agents/members/carol"
            return FakeResp()

    monkeypatch.setattr(mentions.urllib.request, "build_opener", lambda *a: FakeOpener())
    assert mentions._membership_request("strands-agents", "carol", "tok") == 204

    # And a 404 (not a member) is surfaced as its code, not raised.
    class NotFoundOpener:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(mentions.urllib.request, "build_opener", lambda *a: NotFoundOpener())
    assert mentions._membership_request("strands-agents", "eve", "tok") == 404


def test_no_redirect_handler_refuses_to_follow():
    # ADVERSARIAL (seam-level): the redirect handler must REFUSE every redirect by returning None,
    # which makes urllib surface the raw 3xx as an HTTPError instead of following a server-supplied
    # Location; the public-members fallback is a separate request to a URL we build ourselves.
    assert (
        mentions._NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://x/public")
        is None
    )


def _scripted_opener(monkeypatch, statuses):
    """Patch build_opener so successive requests get ``statuses`` in order; returns the URLs hit."""
    import urllib.error

    seen: list[str] = []
    queue = list(statuses)

    class FakeResp:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class ScriptedOpener:
        def open(self, req, timeout=None):
            assert req.get_header("Authorization") == "Bearer tok"
            seen.append(req.full_url)
            status = queue.pop(0)
            if status >= 300:  # mirrors urllib once _NoRedirect refuses to follow a 3xx
                raise urllib.error.HTTPError(
                    req.full_url, status, "x", {"Location": "/elsewhere"}, None
                )
            return FakeResp(status)

    monkeypatch.setattr(mentions.urllib.request, "build_opener", lambda *a: ScriptedOpener())
    return seen


_MEMBERS = "https://api.github.com/orgs/strands-agents/members/eve"
_PUBLIC = "https://api.github.com/orgs/strands-agents/public_members/eve"


def test_membership_request_302_falls_back_to_public_members_204(monkeypatch):
    # The real-world case: the bot account isn't an org member, so /members/{login} answers 302 for
    # EVERY login. A PUBLIC member must still be recognised via /public_members/{login} → 204.
    seen = _scripted_opener(monkeypatch, [302, 204])
    assert mentions._membership_request("strands-agents", "eve", "tok") == 204
    # The fallback URL is one WE build (never the server-supplied Location), on api.github.com.
    assert seen == [_MEMBERS, _PUBLIC]


def test_is_org_member_grants_public_member_via_fallback(monkeypatch):
    _scripted_opener(monkeypatch, [302, 204])
    assert mentions.is_org_member("eve", ("strands-agents",), "tok") is True


@pytest.mark.parametrize("public_status", [404, 302, 301, 200, 403, 500])
def test_membership_request_302_then_non_204_is_not_member(monkeypatch, public_status):
    # ADVERSARIAL: a 302 followed by anything but an exact 204 (incl. another redirect, or a 200)
    # must deny — the fallback can only ever grant on 204, and is asked exactly once.
    seen = _scripted_opener(monkeypatch, [302, public_status])
    assert mentions.is_org_member("eve", ("strands-agents",), "tok") is False
    assert seen == [_MEMBERS, _PUBLIC]


@pytest.mark.parametrize("first", [204, 404, 403, 301, 500])
def test_membership_request_no_fallback_unless_302(monkeypatch, first):
    # Only a 302 ("can't see private membership") triggers the public lookup; a definitive answer
    # (204 member / 404 not a member) or an error is returned as-is with a single request.
    seen = _scripted_opener(monkeypatch, [first])
    assert mentions._membership_request("strands-agents", "eve", "tok") == first
    assert seen == [_MEMBERS]


def test_is_stale_old_mention_is_stale():
    assert mentions.is_stale("2026-06-26T09:00:00Z", "2026-06-26T10:00:00Z") is True


def test_is_stale_fresh_mention_is_not_stale():
    assert mentions.is_stale("2026-06-26T11:00:00Z", "2026-06-26T10:00:00Z") is False


def test_is_stale_equal_timestamps_is_stale():
    assert mentions.is_stale("2026-06-26T10:00:00Z", "2026-06-26T10:00:00Z") is True


def test_is_stale_fail_open_on_missing_timestamps():
    assert mentions.is_stale(None, "2026-06-26T10:00:00Z") is False
    assert mentions.is_stale("2026-06-26T10:00:00Z", None) is False
    assert mentions.is_stale("garbage", "also-garbage") is False


def test_is_stale_handles_naive_and_aware_without_error():
    # A timestamp without a 'Z'/offset (naive) must not raise when compared to an aware one.
    assert mentions.is_stale("2026-06-26T09:00:00", "2026-06-26T10:00:00Z") is True
    assert mentions.is_stale("2026-06-26T11:00:00", "2026-06-26T10:00:00Z") is False


class FakeDynamo:
    """Minimal in-memory DynamoDB stand-in supporting get_item/put_item, or raising on demand."""

    def __init__(self, raise_on_get: bool = False, raise_on_put: bool = False):
        self.items: dict[str, dict[str, Any]] = {}
        self.raise_on_get = raise_on_get
        self.raise_on_put = raise_on_put
        self.puts: list[dict[str, Any]] = []
        self.deletes: list[str] = []

    def get_item(self, TableName, Key, **kw):  # noqa: N803 — boto kwarg names
        if self.raise_on_get:
            raise RuntimeError("boom")
        tid = Key["thread_id"]["S"]
        item = self.items.get(tid)
        return {"Item": item} if item else {}

    def put_item(self, TableName, Item, **kw):  # noqa: N803
        self.puts.append(Item)
        if self.raise_on_put:
            raise RuntimeError("boom")
        # Dedup rows key on thread_id; mention-log rows on mention_id — store either.
        key = Item.get("thread_id") or Item.get("mention_id")
        self.items[key["S"]] = Item

    def delete_item(self, TableName, Key, **kw):  # noqa: N803
        if getattr(self, "raise_on_delete", False):
            raise RuntimeError("boom")
        self.deletes.append(Key["thread_id"]["S"])
        self.items.pop(Key["thread_id"]["S"], None)


def test_backstop_already_dispatched_true_when_recorded_newer_or_equal():
    client = FakeDynamo()
    dedup.record_dispatch(client, "T", "thread-1", "2026-06-26T10:00:00Z")
    assert dedup.already_dispatched(client, "T", "thread-1", "2026-06-26T10:00:00Z") is True
    assert dedup.already_dispatched(client, "T", "thread-1", "2026-06-26T09:00:00Z") is True


def test_backstop_not_dispatched_when_recorded_older():
    client = FakeDynamo()
    dedup.record_dispatch(client, "T", "thread-1", "2026-06-26T09:00:00Z")
    assert dedup.already_dispatched(client, "T", "thread-1", "2026-06-26T10:00:00Z") is False


def test_backstop_fail_open_on_read_error():
    client = FakeDynamo(raise_on_get=True)
    assert dedup.already_dispatched(client, "T", "thread-1", "2026-06-26T10:00:00Z") is False


def test_backstop_no_table_or_no_client_is_fail_open():
    assert dedup.already_dispatched(None, None, "t", "2026-06-26T10:00:00Z") is False
    assert dedup.already_dispatched(FakeDynamo(), None, "t", "2026-06-26T10:00:00Z") is False


def test_backstop_record_swallows_write_error():
    client = FakeDynamo(raise_on_put=True)
    dedup.record_dispatch(client, "T", "thread-1", "2026-06-26T10:00:00Z")  # must not raise
    assert client.puts  # it tried


def test_backstop_record_includes_ttl():
    client = FakeDynamo()
    dedup.record_dispatch(client, "T", "thread-1", "2026-06-26T10:00:00Z")
    assert "ttl" in client.puts[0] and client.puts[0]["ttl"]["N"].isdigit()


def test_backstop_clear_dispatch_removes_row():
    client = FakeDynamo()
    dedup.record_dispatch(client, "T", "thread-1", "2026-06-26T10:00:00Z")
    assert "thread-1" in client.items
    dedup.clear_dispatch(client, "T", "thread-1")
    assert "thread-1" not in client.items
    assert client.deletes == ["thread-1"]
    # After rollback the backstop no longer suppresses a retry.
    assert dedup.already_dispatched(client, "T", "thread-1", "2026-06-26T10:00:00Z") is False


def test_backstop_clear_dispatch_no_table_or_client_is_noop():
    dedup.clear_dispatch(FakeDynamo(), None, "thread-1")  # no table → no-op
    dedup.clear_dispatch(None, "T", "thread-1")  # no client → no-op


def test_backstop_clear_dispatch_swallows_delete_error():
    client = FakeDynamo()
    client.raise_on_delete = True
    dedup.clear_dispatch(client, "T", "thread-1")  # must not raise


# ---------------------------------------------------------------------------
# Step 6: prompt + session-id building
# ---------------------------------------------------------------------------
def test_build_session_id_pr_and_issue():
    assert mentions.build_session_id("o/r", True, 42) == "gh-o-r-96458419-pr-42"
    assert mentions.build_session_id("o/r", False, 7) == "gh-o-r-96458419-issue-7"


def test_build_prompt_contains_author_source_url_and_body():
    m = mentions.Mention(
        author="example-maintainer", source="comment", timestamp="t", body="please review @example-user"
    )
    prompt = mentions.build_prompt(m, "o/r", True, 42, NOW)
    assert "@example-maintainer" in prompt and "in comment of o/r#42" in prompt
    assert "https://github.com/o/r/pull/42" in prompt
    assert "please review" in prompt
    assert "Do NOT dismiss as duplicate" in prompt
    assert "2026-06-26T12:00:00Z" in prompt


def test_build_prompt_truncates_long_body():
    m = mentions.Mention(author="example-maintainer", source="body", timestamp="t", body="x" * 5000)
    prompt = mentions.build_prompt(m, "o/r", False, 1, NOW)
    assert "... (truncated)" in prompt
    assert len(prompt) < 5000


# ---------------------------------------------------------------------------
# Step 7: dispatch — fire-and-forget + correct session id (launch_run mocked)
# ---------------------------------------------------------------------------
def test_dispatch_uses_launch_run_fire_and_forget(monkeypatch):
    captured = {}

    def fake_launch_run(arn, region, session_id, prompt, github_context):
        captured.update(
            arn=arn,
            region=region,
            session_id=session_id,
            prompt=prompt,
            github_context=github_context,
        )
        return {"status": "accepted", "taskId": "task-123"}

    from strandly.ops import runtime_client

    monkeypatch.setattr(runtime_client, "launch_run", fake_launch_run)
    out = mentions.dispatch(_settings(), "gh-o-r-96458419-pr-42", "the prompt")

    assert out == {"status": "accepted", "taskId": "task-123"}
    assert captured["session_id"] == "gh-o-r-96458419-pr-42"
    assert captured["arn"].endswith("runtime/strandly-abc")
    assert captured["region"] == "us-west-2"
    # Fire-and-forget: NO GitHub context is passed.
    assert captured["github_context"] == {}


def test_dispatch_forwards_ack_url_for_terminal_reactions(monkeypatch):
    """The mention's reactions endpoint rides along as ackReactionsUrl — the runtime posts the
    terminal 🚀/😕 there. It is a plain payload key, NOT a GitHub event context (no event_name/
    event keys), so the fire-and-forget path is unchanged."""
    captured = {}

    def fake_launch_run(arn, region, session_id, prompt, github_context):
        captured.update(github_context=github_context)
        return {"status": "accepted", "taskId": "task-123"}

    from strandly.ops import runtime_client

    monkeypatch.setattr(runtime_client, "launch_run", fake_launch_run)
    ack = "https://api.github.com/repos/o/r/issues/comments/9/reactions"
    out = mentions.dispatch(_settings(), "gh-o-r-96458419-pr-42", "the prompt", ack)

    assert out == {"status": "accepted", "taskId": "task-123"}
    assert captured["github_context"] == {"ackReactionsUrl": ack}


def test_dispatch_forwards_queue_provenance(monkeypatch):
    """``triggerId`` (the comment's reactions endpoint), ``triggerHash`` and ``triggerSource`` ride
    the payload.

    The runtime treats a re-dispatch of the running trigger with unchanged text as a no-op.
    """
    captured = {}

    def fake_launch_run(arn, region, session_id, prompt, github_context):
        captured.update(github_context=github_context)
        return {"status": "accepted", "taskId": "task-123", "duplicate": True}

    from strandly.ops import runtime_client

    monkeypatch.setattr(runtime_client, "launch_run", fake_launch_run)
    ack = "https://api.github.com/repos/o/r/issues/comments/9/reactions"
    out = mentions.dispatch(
        _settings(),
        "gh-o-r-96458419-pr-42",
        "the prompt",
        ack,
        trigger_id=ack,
        source="github:@octocat",
        trigger_hash="h1",
        github_url="https://github.com/o/r/pull/42",
        github_user_id="12345",
    )

    assert mentions._dispatch_accepted(out)
    assert captured["github_context"] == {
        "ackReactionsUrl": ack,
        "triggerId": ack,
        "triggerHash": "h1",
        "triggerSource": "github:@octocat",
        "githubUrl": "https://github.com/o/r/pull/42",
        "githubUserId": "12345",
        "githubIssuer": "https://github.com",
    }


def test_dispatch_raises_without_runtime_arn(monkeypatch):
    monkeypatch.delenv("STRANDLY_RUNTIME_ARN", raising=False)
    from strandly.ops import runtime_client

    # Isolate the on-disk fallback too: resolve_runtime_arn also reads ~/.strandly/runtime.json, so
    # on a dev machine that has deployed before it would leak a real ARN and mask the "no arn ->
    # raise" path (green in CI, red locally). Force it empty so the test exercises the
    # genuinely-unresolvable case regardless of machine state.
    monkeypatch.setattr(runtime_client, "_recorded", lambda: {})
    monkeypatch.setattr(
        runtime_client, "launch_run", lambda *a, **k: pytest.fail("should not dispatch")
    )
    with pytest.raises(RuntimeError):
        mentions.dispatch(_settings(runtime_arn=None), "s", "p")


@pytest.mark.parametrize(
    "result,accepted",
    [
        ({"status": "accepted", "taskId": "t"}, True),
        ({"status": "accepted"}, True),
        # A re-dispatch of the running trigger: the runtime already owns it.
        ({"status": "accepted", "taskId": "t", "duplicate": True}, True),
        # A rejection (the scheduled surface's policy) must still leave the trigger unconsumed.
        ({"status": "rejected", "reason": "session busy"}, False),
        ({"status": "error", "error": "needs a GitHub context"}, False),  # HTTP-200 error body
        ({"raw": "not json"}, False),
        ({}, False),
        (None, False),
        ("accepted", False),
    ],
)
def test_dispatch_accepted_only_on_explicit_acceptance(result, accepted):
    assert mentions._dispatch_accepted(result) is accepted


# ---------------------------------------------------------------------------
# 👀 ack reaction (started-working signal on a dispatched mention)
# ---------------------------------------------------------------------------
def test_select_mention_carries_comment_reactions_url():
    m = mentions.select_mention(
        content={
            "body": "",
            "reactions": {"url": "https://api.github.com/repos/o/r/issues/7/reactions"},
        },
        comments=[
            {
                "body": "@example-user go",
                "user": {"login": "example-maintainer"},
                "updated_at": "2026-06-26T11:00:00Z",
                "reactions": {
                    "url": "https://api.github.com/repos/o/r/issues/comments/1/reactions"
                },
            }
        ],
        reviews=[],
        review_comments=[],
        handle="example-user",
        is_pull_request=False,
    )
    assert m and m.reactions_url == "https://api.github.com/repos/o/r/issues/comments/1/reactions"


def test_select_mention_review_body_acks_on_pr_itself():
    # Review *bodies* have no reactions API → the candidate carries the PR's own reactions URL.
    m = mentions.select_mention(
        content={
            "body": "",
            "reactions": {"url": "https://api.github.com/repos/o/r/issues/7/reactions"},
        },
        comments=[],
        reviews=[
            {
                "body": "@example-user thoughts?",
                "user": {"login": "example-maintainer"},
                "state": "COMMENTED",
                "submitted_at": "2026-06-26T11:00:00Z",
            }
        ],
        review_comments=[],
        handle="example-user",
        is_pull_request=True,
    )
    assert m and m.source == "review (COMMENTED)"
    assert m.reactions_url == "https://api.github.com/repos/o/r/issues/7/reactions"


def test_select_mention_reactions_url_empty_when_payload_lacks_it():
    m = mentions.select_mention(
        content={"body": ""},
        comments=[
            {
                "body": "@example-user go",
                "user": {"login": "example-maintainer"},
                "updated_at": "2026-06-26T11:00:00Z",
            }
        ],
        reviews=[],
        review_comments=[],
        handle="example-user",
        is_pull_request=False,
    )
    assert m and m.reactions_url == ""  # process_notification falls back to the issue/PR endpoint


def test_add_ack_reaction_posts_eyes(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(mentions, "_request", lambda m, u, t, **k: calls.append((m, u, t, k)))
    mentions.add_ack_reaction("https://api.github.com/repos/o/r/issues/comments/1/reactions", "tok")
    assert calls == [
        (
            "POST",
            "https://api.github.com/repos/o/r/issues/comments/1/reactions",
            "tok",
            {"parse": False, "data": {"content": "eyes"}},
        )
    ]


def test_add_ack_reaction_noop_on_empty_url(monkeypatch):
    monkeypatch.setattr(mentions, "_request", lambda *a, **k: pytest.fail("must not call the API"))
    mentions.add_ack_reaction("", "tok")  # no target → silent no-op


def test_add_ack_reaction_fail_open_on_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(mentions, "_request", boom)
    mentions.add_ack_reaction(
        "https://api.github.com/repos/o/r/issues/1/reactions", "tok"
    )  # no raise


def test_add_ack_reaction_fail_open_on_url_guard():
    # A doctored reactions.url pointing off-github: the guard raises ValueError inside _request and
    # add_ack_reaction swallows it — the token never leaves, the poll never breaks.
    mentions.add_ack_reaction("https://evil.example.com/reactions", "tok")  # no raise


def test_request_posts_json_body(monkeypatch):
    """_request(data=...) JSON-encodes the body and sets Content-Type (the reaction POST plumbing)."""
    seen = {}

    class FakeResp:
        status = 201

        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["data"] = req.data
        seen["content_type"] = req.get_header("Content-type")
        seen["method"] = req.get_method()
        return FakeResp()

    from strandly.ops import github

    monkeypatch.setattr(github._OPENER, "open", fake_urlopen)
    out = mentions._request(
        "POST",
        "https://api.github.com/repos/o/r/issues/1/reactions",
        "tok",
        parse=False,
        data={"content": "eyes"},
    )
    assert out is None
    assert seen["method"] == "POST"
    assert seen["data"] == b'{"content": "eyes"}'
    assert seen["content_type"] == "application/json"


def test_process_dispatched_acks_on_the_mention_comment(monkeypatch, wired):
    """The ack lands on the mention item itself when the payload carries reactions.url."""
    gathered = _gather()
    gathered["comments"][0]["reactions"] = {
        "url": "https://api.github.com/repos/ext/repo/issues/comments/9/reactions"
    }
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: gathered)
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(),
        token="tok",
        ddb_client=None,
        now=NOW,
    )
    assert outcome == "dispatched"
    assert wired.reactions == ["https://api.github.com/repos/ext/repo/issues/comments/9/reactions"]


def test_process_no_ack_when_dispatch_rejected(monkeypatch):
    """A rejected dispatch retries later — the 👀 must not lie about work having started."""
    reactions: list[str] = []
    monkeypatch.setattr(mentions, "add_ack_reaction", lambda url, token: reactions.append(url))
    monkeypatch.setattr(mentions, "mark_read", lambda *a, **k: None)
    monkeypatch.setattr(mentions, "dispatch", lambda *a, **k: {"status": "error"})
    monkeypatch.setattr(
        mentions, "gather_subject", lambda *a, **k: _gather(ts="2026-06-26T11:00:00Z")
    )
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(),
        token="tok",
        ddb_client=None,
        now=NOW,
    )
    assert outcome == "dispatch-error"
    assert reactions == []


# ---------------------------------------------------------------------------
# process_notification end-to-end (network + dispatch mocked)
# ---------------------------------------------------------------------------
class Recorder:
    def __init__(self):
        self.dispatched: list[tuple[str, str]] = []
        self.dispatch_ack_urls: list[str] = []
        self.dispatch_trigger_ids: list[str] = []
        self.dispatch_sources: list[str] = []
        self.dispatch_trigger_hashes: list[str] = []
        self.dispatch_github_urls: list[str] = []
        self.marked_read: list[str] = []
        self.reactions: list[str] = []


@pytest.fixture
def wired(monkeypatch):
    """Wire all I/O seams to in-memory fakes; return a Recorder of side effects."""
    rec = Recorder()
    monkeypatch.setattr(mentions, "mark_read", lambda tid, token: rec.marked_read.append(tid))
    monkeypatch.setattr(mentions, "add_ack_reaction", lambda url, token: rec.reactions.append(url))

    def fake_dispatch(
        settings, session_id, prompt, ack_url="", trigger_id="", source="", trigger_hash="",
        github_url="", github_user_id="",
    ):
        rec.dispatched.append((session_id, prompt))
        rec.dispatch_github_urls.append(github_url)
        rec.github_user_id = github_user_id
        rec.dispatch_ack_urls.append(ack_url)
        rec.dispatch_trigger_ids.append(trigger_id)
        rec.dispatch_sources.append(source)
        rec.dispatch_trigger_hashes.append(trigger_hash)
        return {"status": "accepted", "taskId": "t"}

    monkeypatch.setattr(mentions, "dispatch", fake_dispatch)
    return rec


def _pr_notification(thread_id="thread-1", last_read_at=None):
    return {
        "id": thread_id,
        "reason": "mention",
        "repository": {"full_name": "ext/repo"},
        "subject": {"type": "PullRequest", "url": "https://api.github.com/repos/ext/repo/pulls/42"},
        "last_read_at": last_read_at,
    }


def _gather(
    comment_body="@example-user please look", author="example-maintainer", ts="2026-06-26T11:00:00Z"
):
    return {
        "content": {"number": 42, "body": "", "comments_url": "x"},
        "comments": [{"body": comment_body, "user": {"login": author, "id": 12345}, "updated_at": ts}],
        "reviews": [],
        "review_comments": [],
    }


def test_process_dispatches_fresh_authorized_mention(monkeypatch, wired):
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather())
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(),
        token="tok",
        ddb_client=None,
        now=NOW,
    )
    assert outcome == "dispatched"
    assert wired.dispatched == [("gh-ext-repo-11e2c1a1-pr-42", wired.dispatched[0][1])]
    assert wired.marked_read == ["thread-1"]
    # The 👀 ack landed; _gather's comment carries no reactions.url → issue/PR-level fallback.
    assert wired.reactions == ["https://api.github.com/repos/ext/repo/issues/42/reactions"]
    # The SAME endpoint was forwarded to the runtime for the terminal 🚀/😕 reaction.
    assert wired.dispatch_ack_urls == wired.reactions
    # The issue/PR the run is about: the default notify target of a job scheduled from it.
    assert wired.dispatch_github_urls == ["https://github.com/ext/repo/pull/42"]
    assert wired.github_user_id == "12345"


def test_trigger_id_is_the_items_own_endpoint_never_the_thread_level_fallback(monkeypatch, wired):
    """A review-body mention must NOT carry a thread-level trigger id.

    ``ack_url`` falls back to the issue/PR-level reactions endpoint whenever the item has no
    ``reactions.url`` — which is every PR review *body* (no reactions API for reviews). Sending that
    as ``triggerId`` makes every review-body mention on one PR share a key, so a second one with the
    same text would read as a re-dispatch of the first and be dropped as a duplicate.
    """
    monkeypatch.setattr(
        mentions,
        "gather_subject",
        lambda *a, **k: {
            "content": {"number": 42, "body": "", "comments_url": "x"},
            "comments": [],
            # Review bodies: no reactions.url anywhere in the payload.
            "reviews": [
                {
                    "body": "@example-user thoughts?",
                    "user": {"login": "example-maintainer"},
                    "state": "COMMENTED",
                    "submitted_at": "2026-06-26T11:00:00Z",
                }
            ],
            "review_comments": [],
        },
    )
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(),
        token="tok",
        ddb_client=None,
        now=NOW,
    )
    assert outcome == "dispatched"
    # The ack/terminal-reaction URL still falls back (a 👀 on the PR is better than none)…
    assert wired.dispatch_ack_urls == ["https://api.github.com/repos/ext/repo/issues/42/reactions"]
    # …but NOTHING is sent as the trigger id, so each review-body mention is its own trigger.
    assert wired.dispatch_trigger_ids == [""]


def test_trigger_id_is_sent_for_an_item_that_has_its_own_reactions_endpoint(monkeypatch, wired):
    """The flip side: a comment DOES have an item-level id, so a re-dispatch of it is recognized."""
    comment_url = "https://api.github.com/repos/ext/repo/issues/comments/77/reactions"
    monkeypatch.setattr(
        mentions,
        "gather_subject",
        lambda *a, **k: {
            "content": {"number": 42, "body": "", "comments_url": "x"},
            "comments": [
                {
                    "body": "@example-user please look",
                    "user": {"login": "example-maintainer"},
                    "updated_at": "2026-06-26T11:00:00Z",
                    "reactions": {"url": comment_url},
                }
            ],
            "reviews": [],
            "review_comments": [],
        },
    )
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(),
        token="tok",
        ddb_client=None,
        now=NOW,
    )
    assert outcome == "dispatched"
    assert wired.dispatch_trigger_ids == [comment_url]
    assert wired.dispatch_ack_urls == [comment_url]
    # The fingerprint is of the mention text, not the rendered prompt (which carries the poll time).
    assert wired.dispatch_trigger_hashes == [
        hashlib.sha256(b"@example-user please look").hexdigest()
    ]


def test_process_skips_stale_mention_but_marks_read(monkeypatch, wired):
    monkeypatch.setattr(
        mentions, "gather_subject", lambda *a, **k: _gather(ts="2026-06-26T08:00:00Z")
    )
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(),
        token="tok",
        ddb_client=None,
        now=NOW,
    )
    assert outcome == "stale"
    assert wired.dispatched == [] and wired.marked_read == ["thread-1"]
    assert wired.reactions == []  # no ack for a suppressed mention


def test_process_skips_unauthorized_author(monkeypatch, wired):
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather(author="eve"))
    outcome = mentions.process_notification(
        _pr_notification(),
        settings=_settings(),
        token="tok",
        ddb_client=None,
        now=NOW,
    )
    assert outcome == "unauthorized"
    assert wired.dispatched == [] and wired.marked_read == ["thread-1"]
    assert wired.reactions == []  # no ack for an unauthorized author


def test_process_authorizes_via_org_membership(monkeypatch, wired):
    # Author NOT in the static allow-list but IS a member of an allowed org → dispatched.
    seen: list[tuple[str, str, str]] = []

    def fake_membership(org, login, token):
        seen.append((org, login, token))
        return 204  # member

    monkeypatch.setattr(mentions, "_membership_request", fake_membership)
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather(author="carol"))
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(allowed_orgs=("strands-agents", "strands-labs")),
        token="tok",
        ddb_client=None,
        now=NOW,
    )
    assert outcome == "dispatched"
    assert wired.dispatched and wired.dispatched[0][0] == "gh-ext-repo-11e2c1a1-pr-42"
    assert wired.marked_read == ["thread-1"]
    # The org check actually ran with the poller's token against the configured org.
    assert seen and seen[0] == ("strands-agents", "carol", "tok")


def test_process_unauthorized_when_not_static_and_not_member(monkeypatch, wired):
    # Author NOT in static list and NOT an org member (404 everywhere) → unauthorized.
    monkeypatch.setattr(mentions, "_membership_request", lambda *a: 404)
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather(author="eve"))
    outcome = mentions.process_notification(
        _pr_notification(),
        settings=_settings(allowed_orgs=("strands-agents",)),
        token="tok",
        ddb_client=None,
        now=NOW,
    )
    assert outcome == "unauthorized"
    assert wired.dispatched == [] and wired.marked_read == ["thread-1"]


def test_process_static_allowlist_authorizes_without_any_org_call(monkeypatch, wired):
    # The static path is unchanged: an allow-listed author dispatches WITHOUT any org-membership call.
    monkeypatch.setattr(
        mentions,
        "_membership_request",
        lambda *a: pytest.fail("org check must not run for a static author"),
    )
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather(author="example-maintainer"))
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(allowed_orgs=("strands-agents",)),
        token="tok",
        ddb_client=None,
        now=NOW,
    )
    assert outcome == "dispatched"
    assert wired.marked_read == ["thread-1"]


def test_process_fail_closed_when_org_api_errors(monkeypatch, wired):
    # FAIL-CLOSED: the org-membership API raising (network error / 403) → unauthorized, not dispatch.
    def boom(*a):
        raise RuntimeError("403 / network down")

    monkeypatch.setattr(mentions, "_membership_request", boom)
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather(author="carol"))
    outcome = mentions.process_notification(
        _pr_notification(),
        settings=_settings(allowed_orgs=("strands-agents",)),
        token="tok",
        ddb_client=None,
        now=NOW,
    )
    assert outcome == "unauthorized"
    assert wired.dispatched == [] and wired.marked_read == ["thread-1"]


def test_process_skips_own_repo_without_fetching(monkeypatch, wired):
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: pytest.fail("should not fetch"))
    notif = _pr_notification()
    notif["repository"]["full_name"] = "example-user/example-operator-repo"
    outcome = mentions.process_notification(
        notif,
        settings=_settings(),
        token="tok",
        ddb_client=None,
        now=NOW,
    )
    assert outcome == "skipped-own-repo"
    assert wired.dispatched == [] and wired.marked_read == ["thread-1"]


@pytest.mark.parametrize(
    ("repo", "managed"),
    [
        ("strands-agents-sdk/python", ("strands-agents", "strands-labs")),
        ("acme-labs/tool", ("acme",)),
        ("acme/other", ("acme/labs-*",)),
        ("", ("ext",)),
        ("ext/repo", ()),
    ],
)
def test_process_skips_unmanaged_repositories_without_fetching(monkeypatch, wired, repo, managed):
    """A mention outside the managed owners never picks or seeds a session: skipped and read."""
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: pytest.fail("should not fetch"))
    notif = _pr_notification()
    notif["repository"]["full_name"] = repo
    outcome = mentions.process_notification(
        notif, settings=_settings(managed_owners=managed), token="tok", ddb_client=None, now=NOW
    )
    assert outcome == "skipped-unmanaged-repo"
    assert wired.dispatched == [] and wired.marked_read == ["thread-1"]


@pytest.mark.parametrize(
    ("repo", "managed"),
    [("strands-agents/sdk-python", ("strands-agents",)), ("Acme/Labs-Tool", ("acme/labs-*",))],
)
def test_process_dispatches_in_managed_repositories(monkeypatch, wired, repo, managed):
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather())
    notif = _pr_notification(last_read_at="2026-06-26T10:00:00Z")
    notif["repository"]["full_name"] = repo
    outcome = mentions.process_notification(
        notif, settings=_settings(managed_owners=managed), token="tok", ddb_client=None, now=NOW
    )
    assert outcome == "dispatched"
    assert wired.dispatched[0][0] == mentions.build_session_id(repo, True, 42)


def test_config_manages_the_use_github_owners():
    cfg = Config(values={"STRANDLY_ALLOWED_OWNERS": "example-org, example-team/approved-*"})
    assert cfg.mention_poller.managed_owners == ("example-org", "example-team/approved-*")
    assert Config(values={}).mention_poller.managed_owners == Config(values={}).github.allowed_owners


def test_process_no_mention_author_skipped_for_security(monkeypatch, wired):
    monkeypatch.setattr(
        mentions, "gather_subject", lambda *a, **k: _gather(comment_body="nothing here")
    )
    outcome = mentions.process_notification(
        _pr_notification(),
        settings=_settings(),
        token="tok",
        ddb_client=None,
        now=NOW,
    )
    assert outcome == "no-mention"
    assert wired.dispatched == [] and wired.marked_read == ["thread-1"]


def test_process_backstop_suppresses_when_last_read_at_missing(monkeypatch, wired):
    monkeypatch.setattr(
        mentions, "gather_subject", lambda *a, **k: _gather(ts="2026-06-26T11:00:00Z")
    )
    client = FakeDynamo()
    dedup.record_dispatch(client, "T", "thread-1", "2026-06-26T11:00:00Z")  # already dispatched
    outcome = mentions.process_notification(
        _pr_notification(last_read_at=None),  # primary signal absent → backstop must catch it
        settings=_settings(dedup_table="T"),
        token="tok",
        ddb_client=client,
        now=NOW,
    )
    assert outcome == "stale"
    assert wired.dispatched == []


def test_process_records_backstop_on_dispatch(monkeypatch, wired):
    monkeypatch.setattr(
        mentions, "gather_subject", lambda *a, **k: _gather(ts="2026-06-26T11:00:00Z")
    )
    client = FakeDynamo()
    outcome = mentions.process_notification(
        _pr_notification(last_read_at=None),
        settings=_settings(dedup_table="T"),
        token="tok",
        ddb_client=client,
        now=NOW,
    )
    assert outcome == "dispatched"
    assert client.items["thread-1"]["last_dispatched_ts"]["S"] == "2026-06-26T11:00:00Z"


def test_process_dispatch_rejected_fails_closed(monkeypatch):
    """HIGH-1: an HTTP-200 error body must NOT mark-read or leave a dedup row (so it retries)."""
    marked: list[str] = []
    monkeypatch.setattr(mentions, "mark_read", lambda tid, token: marked.append(tid))
    monkeypatch.setattr(
        mentions, "dispatch", lambda *a, **k: {"status": "error", "error": "needs a GitHub context"}
    )
    monkeypatch.setattr(
        mentions, "gather_subject", lambda *a, **k: _gather(ts="2026-06-26T11:00:00Z")
    )
    client = FakeDynamo()
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(dedup_table="T"),
        token="tok",
        ddb_client=client,
        now=NOW,
    )
    assert outcome == "dispatch-error"
    assert marked == []  # NOT marked read → next poll retries
    assert "thread-1" not in client.items  # backstop intent rolled back → won't suppress retry
    assert client.deletes == ["thread-1"]  # rollback actually happened


def test_process_dispatch_rejected_without_table_still_fails_closed(monkeypatch):
    """HIGH-1 with no dedup table: still must not mark read on a rejected invoke."""
    marked: list[str] = []
    monkeypatch.setattr(mentions, "mark_read", lambda tid, token: marked.append(tid))
    monkeypatch.setattr(mentions, "dispatch", lambda *a, **k: {"status": "error"})
    monkeypatch.setattr(
        mentions, "gather_subject", lambda *a, **k: _gather(ts="2026-06-26T11:00:00Z")
    )
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(),
        token="tok",
        ddb_client=None,
        now=NOW,
    )
    assert outcome == "dispatch-error"
    assert marked == []


def test_process_dispatch_exception_rolls_back_intent(monkeypatch):
    """HIGH-A: a *raised* dispatch (e.g. boto throttle/timeout, unresolved ARN) must roll back the
    pre-written backstop intent and re-raise — otherwise the orphaned row would make the next poll
    suppress the mention as 'stale' and mark it read (silent drop)."""
    marked: list[str] = []
    monkeypatch.setattr(mentions, "mark_read", lambda tid, token: marked.append(tid))

    def boom(*a, **k):
        raise RuntimeError("throttled")

    monkeypatch.setattr(mentions, "dispatch", boom)
    monkeypatch.setattr(
        mentions, "gather_subject", lambda *a, **k: _gather(ts="2026-06-26T11:00:00Z")
    )
    client = FakeDynamo()
    with pytest.raises(RuntimeError):
        mentions.process_notification(
            _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
            settings=_settings(dedup_table="T"),
            token="tok",
            ddb_client=client,
            now=NOW,
        )
    assert "thread-1" not in client.items  # intent rolled back on the exception path
    assert client.deletes == ["thread-1"]
    assert marked == []  # not marked read on this tick
    # The next poll is NOT suppressed: with the intent rolled back, the backstop reports "not yet".
    assert dedup.already_dispatched(client, "T", "thread-1", "2026-06-26T11:00:00Z") is False


def _read_timeout() -> Exception:
    from botocore.exceptions import ReadTimeoutError

    return ReadTimeoutError(endpoint_url="https://bedrock-agentcore.us-west-2.amazonaws.com")


@pytest.mark.parametrize("kind", ["read-timeout", "connection-closed"])
def test_process_dispatch_maybe_accepted_keeps_intent_and_marks_read(monkeypatch, kind):
    """A launch is ONE attempt with a 300 s read timeout. An error AFTER the request was sent (read
    timeout, connection dropped mid-response) means the run may already be live — rolling the intent
    back would make the next poll dispatch the same mention again (a duplicate run). Keep the intent,
    mark read, no 👀 ack, outcome ``dispatch-maybe``."""
    from botocore.exceptions import ConnectionClosedError

    marked: list[str] = []
    acked: list[str] = []
    monkeypatch.setattr(mentions, "mark_read", lambda tid, token: marked.append(tid))
    monkeypatch.setattr(mentions, "add_ack_reaction", lambda url, token: acked.append(url))
    err = (
        _read_timeout()
        if kind == "read-timeout"
        else ConnectionClosedError(endpoint_url="https://bedrock-agentcore.us-west-2.amazonaws.com")
    )

    def sent_then_lost(*a, **k):
        raise err

    monkeypatch.setattr(mentions, "dispatch", sent_then_lost)
    monkeypatch.setattr(
        mentions, "gather_subject", lambda *a, **k: _gather(ts="2026-06-26T11:00:00Z")
    )
    client = FakeDynamo()
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(dedup_table="T"),
        token="tok",
        ddb_client=client,
        now=NOW,
    )
    assert outcome == "dispatch-maybe"
    assert client.deletes == []  # intent KEPT
    assert dedup.already_dispatched(client, "T", "thread-1", "2026-06-26T11:00:00Z") is True
    assert marked == ["thread-1"]
    assert acked == []  # we do not know it started


def test_process_dispatch_pre_send_errors_still_roll_back(monkeypatch):
    """Errors that provably never reached the runtime keep the fail-closed rollback: a connect
    error, a connect timeout, and a service error response (throttling)."""
    from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError

    errors = [
        EndpointConnectionError(endpoint_url="https://x"),
        ConnectTimeoutError(endpoint_url="https://x"),
        ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "InvokeAgentRuntime"),
    ]
    for err in errors:
        monkeypatch.setattr(mentions, "mark_read", lambda tid, token: None)

        def boom(*a, _err=err, **k):
            raise _err

        monkeypatch.setattr(mentions, "dispatch", boom)
        monkeypatch.setattr(
            mentions, "gather_subject", lambda *a, **k: _gather(ts="2026-06-26T11:00:00Z")
        )
        client = FakeDynamo()
        with pytest.raises(type(err)):
            mentions.process_notification(
                _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
                settings=_settings(dedup_table="T"),
                token="tok",
                ddb_client=client,
                now=NOW,
            )
        assert client.deletes == ["thread-1"], err


def test_poll_metrics_count_dispatch_maybe(monkeypatch):
    emitted: list[dict] = []
    monkeypatch.setattr(mentions.metrics, "emit", lambda doc, **kw: emitted.append(doc))
    mentions._emit_poll_metrics({"processed": 2, "counts": {"dispatch-maybe": 1, "dispatched": 1}})
    (doc,) = emitted
    assert doc[mentions.metrics.DISPATCH_MAYBE] == 1
    assert doc[mentions.metrics.DISPATCHED] == 1
    assert doc[mentions.metrics.DISPATCH_FAILED] == 0


def test_poll_once_dispatch_exception_then_success_redispatches(monkeypatch):
    """HIGH-A end-to-end: a raised dispatch on poll 1 must not silently consume the mention — a
    later poll re-dispatches it (the orphaned-intent silent-drop the reviewer reproduced is gone)."""
    cfg = Config(
        values={
            "STRANDLY_NOTIFICATIONS_TOKEN": "tok",
            "STRANDLY_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-west-2:1:runtime/x",
            "STRANDLY_MENTION_HANDLE": "example-user",
            "STRANDLY_ALLOWED_OWNERS": "ext",
            "STRANDLY_MENTION_ALLOWED_AUTHORS": "example-maintainer",
            "STRANDLY_DEDUP_TABLE": "T",
            "AWS_REGION": "us-west-2",
        }
    )
    client = FakeDynamo()
    monkeypatch.setattr(mentions, "_dynamodb_client", lambda config: client)
    monkeypatch.setattr(
        mentions,
        "fetch_notifications",
        lambda token: [_pr_notification("thread-1", last_read_at=None)],
    )
    monkeypatch.setattr(
        mentions, "gather_subject", lambda *a, **k: _gather(ts="2026-06-26T11:00:00Z")
    )
    marked: list[str] = []
    monkeypatch.setattr(mentions, "mark_read", lambda tid, token: marked.append(tid))

    calls = {"n": 0}

    def flaky_dispatch(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("throttled")
        return {"status": "accepted", "taskId": "t"}

    monkeypatch.setattr(mentions, "dispatch", flaky_dispatch)
    monkeypatch.setattr(mentions, "add_ack_reaction", lambda *a, **k: None)

    out1 = mentions.poll_once(cfg, now=NOW)
    assert out1["counts"].get("error") == 1  # caught, counted as error
    assert marked == []  # NOT marked read → will retry
    assert "thread-1" not in client.items  # intent rolled back

    out2 = mentions.poll_once(cfg, now=NOW)
    assert out2["counts"].get("dispatched") == 1  # re-dispatched, not suppressed
    assert marked == ["thread-1"]


def test_process_dispatches_followup_comment_not_shadowed_by_body(monkeypatch, wired):
    """HIGH-3: an old body mention must not shadow a newer follow-up comment (newest-wins)."""

    def gathered(*a, **k):
        return {
            "content": {
                "number": 42,
                "body": "@example-user please review",
                "user": {"login": "example-maintainer"},
                "updated_at": "2026-06-26T09:00:00Z",  # body is OLD (≤ last_read_at)
                "comments_url": "x",
            },
            "comments": [
                {
                    "body": "@example-user any update?",
                    "user": {"login": "example-maintainer"},
                    "updated_at": "2026-06-26T11:00:00Z",
                }  # follow-up is NEW (> last_read_at)
            ],
            "reviews": [],
            "review_comments": [],
        }

    monkeypatch.setattr(mentions, "gather_subject", gathered)
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(),
        token="tok",
        ddb_client=None,
        now=NOW,
    )
    # The body (09:00) is not newer than last_read_at (10:00); newest-wins selects the 11:00
    # follow-up comment → fresh → dispatched.
    assert outcome == "dispatched"
    assert wired.dispatched and wired.dispatched[0][0] == "gh-ext-repo-11e2c1a1-pr-42"
    assert wired.marked_read == ["thread-1"]


# ---------------------------------------------------------------------------
# poll_once orchestration
# ---------------------------------------------------------------------------
def test_poll_once_disabled_when_not_configured():
    out = mentions.poll_once(Config(values={}))
    assert out["status"] == "disabled"


def test_poll_once_errors_without_handle():
    cfg = Config(
        values={
            "STRANDLY_NOTIFICATIONS_TOKEN": "tok",
            "STRANDLY_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-west-2:1:runtime/x",
        }
    )
    out = mentions.poll_once(cfg)
    assert out["status"] == "error"


def test_poll_once_counts_outcomes(monkeypatch):
    cfg = Config(
        values={
            "STRANDLY_NOTIFICATIONS_TOKEN": "tok",
            "STRANDLY_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-west-2:1:runtime/x",
            "STRANDLY_MENTION_HANDLE": "example-user",
            "STRANDLY_ALLOWED_OWNERS": "ext",
            "STRANDLY_MENTION_ALLOWED_AUTHORS": "example-maintainer",
            "AWS_REGION": "us-west-2",
        }
    )
    monkeypatch.setattr(
        mentions,
        "fetch_notifications",
        lambda token: [
            _pr_notification("t1"),
            {"id": "t2", "reason": "subscribed"},
        ],
    )
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather())
    monkeypatch.setattr(mentions, "dispatch", lambda *a, **k: {"status": "accepted"})
    monkeypatch.setattr(mentions, "mark_read", lambda *a, **k: None)
    monkeypatch.setattr(mentions, "add_ack_reaction", lambda *a, **k: None)
    out = mentions.poll_once(cfg, now=NOW)
    assert out["status"] == "ok"
    assert out["processed"] == 1  # the subscribed one is filtered out
    assert out["counts"] == {"dispatched": 1}


def test_poll_once_one_bad_notification_does_not_sink_poll(monkeypatch):
    cfg = Config(
        values={
            "STRANDLY_NOTIFICATIONS_TOKEN": "tok",
            "STRANDLY_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-west-2:1:runtime/x",
            "STRANDLY_MENTION_HANDLE": "example-user",
            "STRANDLY_ALLOWED_OWNERS": "ext",
            "STRANDLY_MENTION_ALLOWED_AUTHORS": "example-maintainer",
            "AWS_REGION": "us-west-2",
        }
    )
    monkeypatch.setattr(
        mentions,
        "fetch_notifications",
        lambda token: [_pr_notification("t1"), _pr_notification("t2")],
    )

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(mentions, "gather_subject", boom)
    monkeypatch.setattr(mentions, "mark_read", lambda *a, **k: None)
    out = mentions.poll_once(cfg, now=NOW)
    assert out["counts"].get("error") == 2  # both errored, but poll completed


# ---------------------------------------------------------------------------
# HTTP plumbing: fetch_notifications + mark_read at the _request seam
# ---------------------------------------------------------------------------
def test_fetch_notifications_parses_list(monkeypatch):
    monkeypatch.setattr(
        mentions, "_request", lambda m, u, t, **k: [{"id": "1", "reason": "mention"}]
    )
    assert mentions.fetch_notifications("tok") == [{"id": "1", "reason": "mention"}]


def test_fetch_notifications_fail_soft_on_error(monkeypatch):
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(mentions, "_request", boom)
    assert mentions.fetch_notifications("tok") == []


def test_mark_read_calls_patch(monkeypatch):
    calls = []
    monkeypatch.setattr(mentions, "_request", lambda m, u, t, **k: calls.append((m, u)))
    mentions.mark_read("thread-9", "tok")
    assert calls == [("PATCH", "https://api.github.com/notifications/threads/thread-9")]


def test_request_refuses_non_github_host():
    # Defense-in-depth: a doctored subject.url must never receive the PAT.
    with pytest.raises(ValueError, match="non-GitHub-API URL"):
        mentions._request("GET", "https://evil.example.com/steal", "tok")
    assert mentions._is_github_api_url("https://api.github.com/notifications") is True
    assert mentions._is_github_api_url("https://api.github.com.evil.com/x") is False


def test_get_fails_soft_on_url_guard(monkeypatch):
    # _get swallows the guard's ValueError and returns None (fail soft, no token leak).
    assert mentions._get("https://evil.example.com/x", "tok") is None


def test_lambda_handler_returns_summary(monkeypatch):
    monkeypatch.setattr(mentions, "poll_once", lambda: {"status": "disabled", "counts": {}})
    assert mentions.lambda_handler({}, None)["status"] == "disabled"


def test_poll_once_emits_poll_metrics(monkeypatch, capsys):
    """A completed poll emits PollSuccess (+ the dispatched/processed counts) as one EMF line."""
    import json as _json

    from strandly.ops import metrics

    monkeypatch.setenv(metrics.NAMESPACE_ENV, "Strandly-dev")
    cfg = Config(
        values={
            "STRANDLY_NOTIFICATIONS_TOKEN": "tok",
            "STRANDLY_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-west-2:1:runtime/x",
            "STRANDLY_MENTION_HANDLE": "example-user",
            "STRANDLY_ALLOWED_OWNERS": "ext",
            "STRANDLY_MENTION_ALLOWED_AUTHORS": "example-maintainer",
            "AWS_REGION": "us-west-2",
        }
    )
    monkeypatch.setattr(mentions, "fetch_notifications", lambda token: [_pr_notification("t1")])
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather())
    monkeypatch.setattr(mentions, "dispatch", lambda *a, **k: {"status": "accepted"})
    monkeypatch.setattr(mentions, "mark_read", lambda *a, **k: None)
    monkeypatch.setattr(mentions, "add_ack_reaction", lambda *a, **k: None)

    mentions.poll_once(cfg, now=NOW)

    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip().startswith("{")]
    docs = [_json.loads(ln) for ln in lines]
    poll_docs = [d for d in docs if d.get("surface") == metrics.SURFACE_POLLER]
    assert poll_docs, "expected a poller EMF line"
    doc = poll_docs[-1]
    assert doc[metrics.POLL_SUCCESS] == 1
    assert doc[metrics.DISPATCHED] == 1
    assert doc[metrics.NOTIFICATIONS_FETCHED] == 1


class ConditionalFakeDynamo(FakeDynamo):
    """FakeDynamo that honors record_dispatch's conditional put (attribute_not_exists OR ts < :ts)."""

    class ConditionalCheckFailedException(Exception):
        pass

    def put_item(self, TableName, Item, **kw):  # noqa: N803
        self.puts.append(Item)
        if self.raise_on_put:
            raise RuntimeError("boom")
        if kw.get("ConditionExpression"):
            tid = Item["thread_id"]["S"]
            existing = self.items.get(tid)
            new_ts = kw["ExpressionAttributeValues"][":ts"]["S"]
            if existing is not None and existing["last_dispatched_ts"]["S"] >= new_ts:
                raise self.ConditionalCheckFailedException(
                    "The conditional request failed (ConditionalCheckFailedException)"
                )
        self.items[Item["thread_id"]["S"]] = Item


def test_backstop_record_conditional_write_wins_when_no_row():
    client = ConditionalFakeDynamo()
    assert dedup.record_dispatch(client, "T", "thread-1", "2026-06-26T10:00:00Z") is True
    assert client.items["thread-1"]["last_dispatched_ts"]["S"] == "2026-06-26T10:00:00Z"


def test_backstop_record_conditional_write_wins_when_row_older():
    client = ConditionalFakeDynamo()
    dedup.record_dispatch(client, "T", "thread-1", "2026-06-26T09:00:00Z")
    # A newer mention on the same thread advances the row (a follow-up mention, not a race).
    assert dedup.record_dispatch(client, "T", "thread-1", "2026-06-26T10:00:00Z") is True
    assert client.items["thread-1"]["last_dispatched_ts"]["S"] == "2026-06-26T10:00:00Z"


def test_backstop_record_loses_race_on_equal_or_newer_row():
    """RC-1 (TOCTOU): the loser of two overlapping polls must get False and skip its dispatch."""
    client = ConditionalFakeDynamo()
    dedup.record_dispatch(client, "T", "thread-1", "2026-06-26T10:00:00Z")  # winner's intent
    # Same mention (equal ts): the concurrent poll already owns it.
    assert dedup.record_dispatch(client, "T", "thread-1", "2026-06-26T10:00:00Z") is False
    # An older mention can never displace a newer recorded dispatch either.
    assert dedup.record_dispatch(client, "T", "thread-1", "2026-06-26T09:00:00Z") is False
    # The winner's row is untouched.
    assert client.items["thread-1"]["last_dispatched_ts"]["S"] == "2026-06-26T10:00:00Z"


def test_backstop_record_fail_open_on_infra_error_still_true():
    # A non-conditional failure (throttle, table missing) stays best-effort:
    # swallow, warn, and return True so the mention still dispatches (fail-open).
    client = ConditionalFakeDynamo(raise_on_put=True)
    assert dedup.record_dispatch(client, "T", "thread-1", "2026-06-26T10:00:00Z") is True


def test_backstop_record_unconfigured_returns_true():
    assert dedup.record_dispatch(None, None, "thread-1", "2026-06-26T10:00:00Z") is True
    assert dedup.record_dispatch(ConditionalFakeDynamo(), None, "t", "2026-06-26T10:00:00Z") is True
    assert dedup.record_dispatch(ConditionalFakeDynamo(), "T", "t", None) is True


def test_process_duplicate_when_losing_intent_race(monkeypatch):
    """RC-1 end-to-end: a poll that passes the already_dispatched read but loses the conditional
    intent write must return 'duplicate' WITHOUT dispatching and WITHOUT mark_read — the winner
    owns both (its success marks read; its failure rolls back and leaves the mention to retry)."""
    marked: list[str] = []
    dispatched: list[str] = []
    monkeypatch.setattr(mentions, "mark_read", lambda tid, token: marked.append(tid))
    monkeypatch.setattr(
        mentions, "dispatch", lambda *a, **k: dispatched.append("x") or {"status": "accepted"}
    )
    monkeypatch.setattr(
        mentions, "gather_subject", lambda *a, **k: _gather(ts="2026-06-26T11:00:00Z")
    )

    class RacyDynamo(ConditionalFakeDynamo):
        """Simulates the TOCTOU window: the read sees nothing, but by write time another poll
        has already recorded the intent row for the same mention."""

        def get_item(self, TableName, Key, **kw):  # noqa: N803
            return {}  # the check passes — row not visible yet

    client = RacyDynamo()
    client.items["thread-1"] = {  # ...but the winner's row lands before our conditional write
        "thread_id": {"S": "thread-1"},
        "last_dispatched_ts": {"S": "2026-06-26T11:00:00Z"},
    }
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(dedup_table="T"),
        token="tok",
        ddb_client=client,
        now=NOW,
    )
    assert outcome == "duplicate"
    assert dispatched == []  # no double-fire into the live session
    assert marked == []  # the winner owns mark_read


# ---- mention log (dashboard Mentions tab) ------------------------------------------------


def _log_rows(client: FakeDynamo) -> list[dict[str, Any]]:
    """The mention-log puts a FakeDynamo saw (dedup rows key on thread_id, log rows on mention_id)."""
    return [p for p in client.puts if "mention_id" in p]


def test_process_logs_dispatched_mention(monkeypatch, wired):
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather())
    client = FakeDynamo()
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(mention_log_table="ML"),
        token="tok",
        ddb_client=client,
        now=NOW,
    )
    assert outcome == "dispatched"
    rows = _log_rows(client)
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"]["S"] == "dispatched"
    assert row["authorized"]["BOOL"] is True
    assert row["author"]["S"] == "example-maintainer"
    assert row["repo"]["S"] == "ext/repo"
    assert row["number"]["N"] == "42"
    assert row["is_pull_request"]["BOOL"] is True
    assert row["gsi_pk"]["S"] == "MENTION"  # constant partition for the "recent" GSI
    assert row["session_id"]["S"] == "gh-ext-repo-11e2c1a1-pr-42"
    assert row["url"]["S"] == "https://github.com/ext/repo/pull/42"
    assert row["mention_ts"]["S"] == "2026-06-26T11:00:00Z"
    assert "ttl" in row and row["ttl"]["N"].isdigit()


def test_process_logs_unauthorized_mention(monkeypatch, wired):
    # The whole point of the tab: an unauthorized mention is visible with authorized=False.
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather(author="eve"))
    client = FakeDynamo()
    outcome = mentions.process_notification(
        _pr_notification(),
        settings=_settings(mention_log_table="ML"),
        token="tok",
        ddb_client=client,
        now=NOW,
    )
    assert outcome == "unauthorized"
    rows = _log_rows(client)
    assert len(rows) == 1
    assert rows[0]["outcome"]["S"] == "unauthorized"
    assert rows[0]["authorized"]["BOOL"] is False
    assert rows[0]["author"]["S"] == "eve"
    assert "session_id" not in rows[0]  # nothing was dispatched


def test_process_logs_stale_mention(monkeypatch, wired):
    monkeypatch.setattr(
        mentions, "gather_subject", lambda *a, **k: _gather(ts="2026-06-26T08:00:00Z")
    )
    client = FakeDynamo()
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(mention_log_table="ML"),
        token="tok",
        ddb_client=client,
        now=NOW,
    )
    assert outcome == "stale"
    rows = _log_rows(client)
    assert len(rows) == 1 and rows[0]["outcome"]["S"] == "stale"


def test_process_logs_dispatch_error(monkeypatch):
    monkeypatch.setattr(mentions, "mark_read", lambda tid, token: None)
    monkeypatch.setattr(mentions, "dispatch", lambda *a, **k: {"status": "error"})
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather())
    client = FakeDynamo()
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(mention_log_table="ML"),
        token="tok",
        ddb_client=client,
        now=NOW,
    )
    assert outcome == "dispatch-error"
    rows = _log_rows(client)
    assert len(rows) == 1 and rows[0]["outcome"]["S"] == "dispatch-error"
    assert rows[0]["session_id"]["S"] == "gh-ext-repo-11e2c1a1-pr-42"


def test_process_no_log_rows_without_table(monkeypatch, wired):
    # Feature off (no table configured) → zero mention-log writes, dispatch unaffected.
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather())
    client = FakeDynamo()
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(dedup_table="T"),
        token="tok",
        ddb_client=client,
        now=NOW,
    )
    assert outcome == "dispatched"
    assert _log_rows(client) == []


def test_mention_log_write_failure_never_blocks_dispatch(monkeypatch, wired):
    # FAIL-OPEN: a broken mention-log table must not change the outcome or suppress mark-read.
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather())
    client = FakeDynamo(raise_on_put=True)  # no dedup table → only the log write hits put_item
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(mention_log_table="ML"),
        token="tok",
        ddb_client=client,
        now=NOW,
    )
    assert outcome == "dispatched"
    assert wired.dispatched and wired.marked_read == ["thread-1"]
    assert client.puts  # it tried


def test_mention_log_record_noop_without_table_or_client():
    client = FakeDynamo()
    mention_log.record(client, None, thread_id="t", outcome="x", authorized=True)
    mention_log.record(None, "ML", thread_id="t", outcome="x", authorized=True)
    assert client.puts == []


def test_mention_log_record_clips_body_and_skips_empty_optionals():
    client = FakeDynamo()
    mention_log.record(
        client,
        "ML",
        thread_id="t",
        outcome="dispatched",
        authorized=True,
        body="x" * 5000,
        author="",
        repo="",
        now=NOW,
    )
    row = client.puts[0]
    assert len(row["body"]["S"]) == 1000  # clipped — the log is an index, not a transcript
    assert "author" not in row and "repo" not in row  # empty strings are omitted, not stored


# ---------------------------------------------------------------------------
# Pagination (regression: mentions past the first page were silently dropped)
# ---------------------------------------------------------------------------
def _paged_seam(pages_by_path):
    """A fake ``_request`` seam: dispatches on URL path, serves ``pages[page_no]`` per path.

    ``pages_by_path`` maps a path substring -> {page_number: payload}. Records every URL so tests
    can assert which pages were requested.
    """
    from urllib.parse import parse_qs, urlparse

    calls: list[str] = []

    def fake(method, url, token, **kw):
        calls.append(url)
        parsed = urlparse(url)
        page = int(parse_qs(parsed.query).get("page", ["1"])[0])
        for fragment, pages in pages_by_path.items():
            if parsed.path.endswith(fragment):
                return pages.get(page, [])
        raise AssertionError(f"unexpected URL in test: {url}")

    return fake, calls


def _comment(i, body="nothing here"):
    return {
        "body": body,
        "user": {"login": f"user{i}"},
        "updated_at": f"2026-06-26T10:{i % 60:02d}:00Z",
    }


def test_get_all_pages_walks_until_short_page(monkeypatch):
    full = [_comment(i) for i in range(100)]
    short = [_comment(i) for i in range(3)]
    fake, calls = _paged_seam({"/items": {1: full, 2: short}})
    monkeypatch.setattr(mentions, "_request", fake)

    items = mentions._get_all_pages("https://api.github.com/items", "tok")

    assert len(items) == 103
    # NB: endswith, not `in` — "per_page=100" itself contains the substring "page=1".
    assert calls[0].endswith("&page=1") and calls[1].endswith("&page=2")
    assert all("per_page=100" in c for c in calls)


def test_get_all_pages_short_first_page_makes_one_call(monkeypatch):
    fake, calls = _paged_seam({"/items": {1: [_comment(1)]}})
    monkeypatch.setattr(mentions, "_request", fake)
    assert len(mentions._get_all_pages("https://api.github.com/items", "tok")) == 1
    assert len(calls) == 1


def test_get_all_pages_preserves_existing_query_params(monkeypatch):
    fake, calls = _paged_seam({"/items": {1: []}})
    monkeypatch.setattr(mentions, "_request", fake)
    mentions._get_all_pages("https://api.github.com/items?participating=true", "tok")
    assert calls == ["https://api.github.com/items?participating=true&per_page=100&page=1"]


def test_get_all_pages_fail_soft_returns_partial_on_midwalk_error(monkeypatch):
    import urllib.error

    full = [_comment(i) for i in range(100)]

    def fake(method, url, token, **kw):
        if url.endswith("&page=1"):
            return full
        raise urllib.error.URLError("flaky")

    monkeypatch.setattr(mentions, "_request", fake)
    items = mentions._get_all_pages("https://api.github.com/items", "tok")
    assert len(items) == 100  # page 1 kept; the flaky page 2 degrades to a partial search


def test_get_all_pages_bounded_by_page_cap(monkeypatch, caplog):
    full = [_comment(i) for i in range(100)]
    fake, calls = _paged_seam({"/items": {p: full for p in range(1, 100)}})
    monkeypatch.setattr(mentions, "_request", fake)

    with caplog.at_level("WARNING"):
        items = mentions._get_all_pages("https://api.github.com/items", "tok")

    assert len(items) == mentions._MAX_PAGES * 100
    assert len(calls) == mentions._MAX_PAGES
    assert any("pagination cap" in r.message for r in caplog.records)


def test_gather_subject_finds_mention_past_first_page(monkeypatch):
    # The @mention lives beyond the first page of comments. The per-issue comments endpoint
    # ignores sort/direction, so a single-page fetch would miss it, mark the thread read and
    # silently drop the mention. Full pagination must surface it.
    subject = "https://api.github.com/repos/o/r/pulls/5"
    content = {
        "number": 5,
        "body": "no handle here",
        "user": {"login": "someone"},
        "comments_url": "https://api.github.com/repos/o/r/issues/5/comments",
    }
    page1 = [_comment(i) for i in range(100)]  # a full page of noise
    page2 = [
        _comment(0),
        {
            "body": "@example-user please review",
            "user": {"login": "example-maintainer"},
            "updated_at": "2026-06-26T11:59:00Z",
        },
    ]

    def fake(method, url, token, **kw):
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(url)
        page = int(parse_qs(parsed.query).get("page", ["1"])[0])
        if parsed.path.endswith("/pulls/5"):
            return content
        if parsed.path.endswith("/issues/5/comments"):
            return {1: page1, 2: page2}.get(page, [])
        if parsed.path.endswith("/pulls/5/reviews") or parsed.path.endswith("/pulls/5/comments"):
            return []
        raise AssertionError(f"unexpected URL in test: {url}")

    monkeypatch.setattr(mentions, "_request", fake)
    gathered = mentions.gather_subject(subject, True, "tok")
    assert len(gathered["comments"]) == 102

    m = mentions.select_mention(
        content=gathered["content"],
        comments=gathered["comments"],
        reviews=gathered["reviews"],
        review_comments=gathered["review_comments"],
        handle="example-user",
        is_pull_request=True,
    )
    assert m is not None and m.author == "example-maintainer" and m.source == "comment"


def test_gather_subject_paginates_reviews_and_line_comments(monkeypatch):
    subject = "https://api.github.com/repos/o/r/pulls/7"
    content = {"number": 7, "comments_url": "https://api.github.com/repos/o/r/issues/7/comments"}
    full = [_comment(i) for i in range(100)]
    fake, calls = _paged_seam(
        {
            "/pulls/7": {1: content},
            "/issues/7/comments": {1: []},
            "/pulls/7/reviews": {1: full, 2: [_comment(1)]},
            "/pulls/7/comments": {1: full, 2: full, 3: []},
        }
    )

    # the subject itself is a dict, not a list — serve it directly
    def fake2(method, url, token, **kw):
        if url == subject:
            calls.append(url)
            return content
        return fake(method, url, token, **kw)

    monkeypatch.setattr(mentions, "_request", fake2)
    gathered = mentions.gather_subject(subject, True, "tok")
    assert len(gathered["reviews"]) == 101
    assert len(gathered["review_comments"]) == 200


def test_fetch_notifications_paginates_at_api_max_50(monkeypatch):
    # The notifications API caps per_page at 50 — the walk must use 50, not 100.
    page1 = [{"id": str(i), "reason": "mention"} for i in range(50)]
    page2 = [{"id": "51", "reason": "mention"}]
    fake, calls = _paged_seam({"/notifications": {1: page1, 2: page2}})
    monkeypatch.setattr(mentions, "_request", fake)

    notifs = mentions.fetch_notifications("tok")

    assert len(notifs) == 51
    assert all("per_page=50" in c for c in calls)
    assert all("participating=true" in c for c in calls)


# ---------------------------------------------------------------------------
# Step 4c: the people directory (ops/people.py) as an additional authorizer + identity context
# ---------------------------------------------------------------------------
class _PeopleDdb:
    """Fake DynamoDB with a people table (GSI Query) — the dedup/mention-log paths stay unconfigured."""

    def __init__(self, *items: dict[str, Any], fail: Exception | None = None) -> None:
        self.items = list(items)
        self.fail = fail
        self.queries: list[dict[str, Any]] = []

    def query(self, **kw: Any) -> dict[str, Any]:
        self.queries.append(kw)
        if self.fail is not None:
            raise self.fail
        attr = kw["ExpressionAttributeNames"]["#k"]
        value = kw["ExpressionAttributeValues"][":v"]["S"]
        return {"Items": [i for i in self.items if (i.get(attr) or {}).get("S") == value]}


CAROL_ROW = {
    "person_id": {"S": "carol"},
    "name": {"S": "Carol Stone"},
    "role": {"S": "maintainer"},
    "github_login": {"S": "carol"},
    "slack_user_id": {"S": "U0CAROL"},
}


@pytest.fixture(autouse=True)
def _clear_people_cache():
    from strandly.ops import people

    people.reset_cache()
    yield
    people.reset_cache()


def test_process_authorizes_via_people_directory_and_introduces_the_author(monkeypatch, wired):
    # Author NOT in the static list, NOT an org member — but an enabled person in the directory.
    monkeypatch.setattr(mentions, "_membership_request", lambda *a: pytest.fail("org check must not run"))
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather(author="carol"))
    ddb = _PeopleDdb(CAROL_ROW)
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(allowed_orgs=("strands-agents",), people_table="strandly-dev-people"),
        token="tok",
        ddb_client=ddb,
        now=NOW,
    )
    assert outcome == "dispatched"
    (q,) = ddb.queries
    assert q["TableName"] == "strandly-dev-people" and q["IndexName"] == "by_github"
    # The prompt tells the agent WHO carol is, across surfaces — one line, right under the header.
    _, prompt = wired.dispatched[0]
    assert "Who @carol is: Carol Stone — maintainer (GitHub @carol, Slack U0CAROL)." in prompt
    assert prompt.index("Who @carol is") < prompt.index("What @carol said")


def test_process_disabled_person_is_unauthorized(monkeypatch, wired):
    monkeypatch.setattr(mentions, "_membership_request", lambda *a: 404)
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather(author="carol"))
    disabled = {**CAROL_ROW, "enabled": {"BOOL": False}}
    outcome = mentions.process_notification(
        _pr_notification(),
        settings=_settings(allowed_orgs=("strands-agents",), people_table="strandly-dev-people"),
        token="tok",
        ddb_client=_PeopleDdb(disabled),
        now=NOW,
    )
    assert outcome == "unauthorized"
    assert wired.dispatched == [] and wired.marked_read == ["thread-1"]


def test_process_disabled_person_authorized_elsewhere_is_not_introduced(monkeypatch, wired):
    # example-maintainer is in the static list but the operator DISABLED his directory row: still dispatched
    # (the secret is an explicit allow) but the prompt must not vouch for who he is.
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather(author="example-maintainer"))
    row = {
        "person_id": {"S": "example-maintainer"},
        "name": {"S": "Example Maintainer"},
        "role": {"S": "maintainer"},
        "github_login": {"S": "example-maintainer"},
        "enabled": {"BOOL": False},
    }
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(people_table="strandly-dev-people"),
        token="tok",
        ddb_client=_PeopleDdb(row),
        now=NOW,
    )
    assert outcome == "dispatched"
    assert "Who @" not in wired.dispatched[0][1]


def test_process_directory_error_fails_closed_then_org_gate_decides(monkeypatch, wired):
    # DynamoDB down: the directory grants nothing (fail-closed) and the org gate is still consulted.
    monkeypatch.setattr(mentions, "_membership_request", lambda *a: 404)
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather(author="carol"))
    outcome = mentions.process_notification(
        _pr_notification(),
        settings=_settings(allowed_orgs=("strands-agents",), people_table="strandly-dev-people"),
        token="tok",
        ddb_client=_PeopleDdb(CAROL_ROW, fail=RuntimeError("ProvisionedThroughputExceeded")),
        now=NOW,
    )
    assert outcome == "unauthorized"
    assert wired.dispatched == []


def test_process_static_author_still_gets_directory_context(monkeypatch, wired):
    # example-maintainer is in the static list; the directory is consulted anyway so the prompt can say who he is.
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather(author="example-maintainer"))
    row = {"person_id": {"S": "example-maintainer"}, "name": {"S": "Example Maintainer"}, "github_login": {"S": "example-maintainer"}}
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(people_table="strandly-dev-people"),
        token="tok",
        ddb_client=_PeopleDdb(row),
        now=NOW,
    )
    assert outcome == "dispatched"
    assert "Who @example-maintainer is: Example Maintainer (GitHub @example-maintainer)." in wired.dispatched[0][1]


def test_process_without_people_table_never_queries(monkeypatch, wired):
    monkeypatch.setattr(mentions, "gather_subject", lambda *a, **k: _gather(author="example-maintainer"))
    ddb = _PeopleDdb(CAROL_ROW)
    outcome = mentions.process_notification(
        _pr_notification(last_read_at="2026-06-26T10:00:00Z"),
        settings=_settings(),  # people_table=None
        token="tok",
        ddb_client=ddb,
        now=NOW,
    )
    assert outcome == "dispatched"
    assert ddb.queries == []
    assert "Who @" not in wired.dispatched[0][1]


def test_build_prompt_without_person_is_unchanged():
    mention = mentions.Mention(author="example-maintainer", source="comment", timestamp="2026-06-26T11:00:00Z", body="hi")
    prompt = mentions.build_prompt(mention, "ext/repo", True, 42, NOW)
    assert prompt.splitlines()[:3] == [
        "NEW mention at 2026-06-26T12:00:00Z by @example-maintainer in comment of ext/repo#42.",
        "URL: https://github.com/ext/repo/pull/42",
        "",
    ]


def test_dynamodb_client_is_built_when_only_the_people_table_is_configured(monkeypatch):
    import sys
    import types

    made: list[tuple[str, str | None]] = []
    fake_boto3 = types.SimpleNamespace(client=lambda svc, region_name=None: made.append((svc, region_name)) or "client")
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    config = Config(values={"STRANDLY_PEOPLE_TABLE": "strandly-dev-people"})
    assert mentions._dynamodb_client(config) == "client"
    assert made == [("dynamodb", None)]
    assert mentions._dynamodb_client(Config(values={})) is None


@pytest.mark.parametrize("location", ["body", "comment", "review", "line"])
def test_selected_mention_preserves_its_immutable_author_id(location):
    item = {"body": "@example-user check this", "user": {"login": "renameable", "id": 98765}, "created_at": "2026-06-26T11:00:00Z", "submitted_at": "2026-06-26T11:00:00Z"}
    mention = mentions.select_mention(
        content=item if location == "body" else {"user": {"login": "other", "id": 111}},
        comments=[item] if location == "comment" else [],
        reviews=[item] if location == "review" else [],
        review_comments=[item] if location == "line" else [],
        handle="example-user", is_pull_request=True,
    )
    assert mention and mention.author == "renameable" and mention.author_id == "98765"
