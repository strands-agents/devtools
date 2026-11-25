"""``use_slack`` and ``notify_team``: the agent's Slack tools.

``use_slack`` is the Slack Web API tool, the agent's reply path for Slack ingress. ``notify_team``
posts a home-channel notification as a title plus a threaded details reply and records the thread
link (``strandly.ops.slack``) that gives later replies in that thread the notification's context.

One general tool whose Slack method is a parameter, the same pattern as ``use_github``. It carries
the bot token from :class:`~strandly.core.config.SlackSettings` and is injected by ``build_tools``
for Slack-triggered runs, or for non-personal notifications when the home-channel switch, a valid
destination and the token are all present. Every DM run is confined to its own DM thread, and
only a fixed set of methods is allowed. Runs are fire-and-forget, so the agent posts its own reply;
the prompt names the channel.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from strands import ToolContext  # noqa: TC002 — tool decorator resolves the annotation at runtime

from strandly.core.config import SlackSettings
from strandly.core.context import RUN_CONTEXT_STATE_KEY, TRIGGER_SOURCE_KEY, RuntimeContext
from strandly.core.session_ids import sanitize_session_id
from strandly.ops.slack import (
    THREAD_LINK_TITLE_MAX,
    clip,
    is_slack_ts,
    slack_error_message,
    thread_link_item,
)

logger = logging.getLogger(__name__)

SLACK_API_URL = "https://slack.com/api"
_USER_AGENT = "strandly-slack-tool/1.0"

# (method, Slack error) pairs meaning "already done": treated as success, not a tool error.
_IDEMPOTENT_NOOPS = frozenset({("reactions.add", "already_reacted"), ("reactions.remove", "no_reaction")})

# A Slack Web API method name: dot-separated lowercase groups (``chat.postMessage``,
# ``reactions.add``). Anchored so a doctored ``action`` can never traverse the URL path or smuggle
# a query string — the method is interpolated into the request URL.
_METHOD_RE = re.compile(r"^[a-z][a-zA-Z]*(\.[a-z][a-zA-Z]*)+$")

# The only Slack methods the agent may call. Fail closed: the bot token's ``chat:write`` reaches
# every DM and every channel the bot is in, so the method set and the destination checks below,
# not the token's scopes, decide what a run can touch.
ALLOWED_METHODS = frozenset({"chat.postMessage", "reactions.add", "reactions.remove"})
# Home-channel and shared-channel runs get a narrower set (no reaction removal).
_HOME_METHODS = frozenset({"chat.postMessage", "reactions.add"})


def _slack_api(method: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
    """POST one Slack Web API call. The only network seam (tests monkeypatch this)."""
    req = urllib.request.Request(
        f"{SLACK_API_URL}/{method}",
        data=json.dumps(payload).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("User-Agent", _USER_AGENT)
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed slack.com host
        return json.loads(resp.read().decode())


def _result(status: str, text: str) -> dict[str, Any]:
    return {"status": status, "content": [{"text": text}]}


def make_use_slack(
    settings: SlackSettings,
    *,
    default_channel_id: str | None = None,
    default_thread_ts: str | None = None,
    pin_destination: bool = False,
    home_channel: str | None = None,
    private_dm: bool = False,
) -> Any:
    """Return a ``use_slack`` tool bound to the harness's Slack settings (the bot token).

    Built and injected by ``build_tools`` when ``config.slack_enabled`` *and* the run came from
    Slack (``ctx.slack_channel_id``) — like ``make_use_github``,
    not a plain builtin: the token is carried in the closure from the loaded ``Config`` (Secrets
    Manager / ``.env``), so the deployed runtime authenticates even when the token never reaches
    ``os.environ``.

    ``default_thread_ts`` (the run's ``slackThreadTs``) is applied to a ``chat.postMessage`` into
    ``default_channel_id`` that omits ``thread_ts``: every Slack DM conversation is a thread, so a
    reply the model forgot to thread would otherwise land at the DM root, outside the session.
    """
    from strands import tool

    # Keep fail-closed destination confinement even when an enabled installation lacks a valid
    # channel key. Missing configuration must not restore unrestricted shared destinations.
    home_mode = bool(home_channel or settings.home_channel_enabled)
    blocked_methods: dict[str, str] = {}

    @tool(name="use_slack", context=True)
    def use_slack(
        action: str,
        channel_id: str,
        text: str = "",
        timestamp: str = "",
        name: str = "",
        thread_ts: str = "",
        tool_context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Call a Slack Web API method — post a message or react in the Slack DM you serve.

        Universal Slack access in one tool (like ``use_github``): ``action`` is the Web API method
        name and the remaining arguments fill its payload. The common calls:

        - ``chat.postMessage`` — post ``text`` into ``channel_id`` (optionally under ``thread_ts``).
          This is how you deliver your reply to a Slack DM: post it to the channel the dispatch
          prompt named. Use Slack ``mrkdwn`` (``*bold*``, `` `code` ``, `````blocks``` ``), not
          GitHub Markdown.
        - ``reactions.add`` / ``reactions.remove`` — react on the message at ``timestamp`` with the
          emoji ``name`` (no colons, e.g. ``white_check_mark``).

        The tool accepts only ``chat.postMessage``, ``reactions.add`` and ``reactions.remove``, and
        only in the conversation this run serves: a DM run replies in its own DM thread. In the
        team's home channel this tool posts only inside a thread; a new notification there goes
        through ``notify_team``.

        Args:
            action: The Slack Web API method, e.g. "chat.postMessage" or "reactions.add".
            channel_id: The channel to act on (the DM channel id from the dispatch prompt).
            text: chat.postMessage: the message text (mrkdwn).
            timestamp: reactions.*: the ``ts`` of the message to react on.
            name: reactions.*: the reaction (emoji) name, without colons.
            thread_ts: chat.postMessage: thread timestamp to reply under (defaults to the thread
                this Slack conversation is in).

        Returns:
            ``{"status": "success"|"error", "content": [{"text": <JSON of the response's ok,
            channel and ts, or the error>}]}``.
        """
        if not settings.bot_token:
            return _result("error", "No Slack bot token configured (STRANDLY_SLACK_BOT_TOKEN).")
        method = (action or "").strip()
        if not _METHOD_RE.match(method):
            return _result(
                "error",
                f"Invalid Slack API method {method!r} — expected a dotted Web API method name "
                "like 'chat.postMessage' or 'reactions.add'.",
            )
        if method not in ALLOWED_METHODS:
            return _result(
                "error",
                f"Slack method {method!r} is not allowed; use_slack supports only "
                f"{', '.join(sorted(ALLOWED_METHODS))}.",
            )
        if not channel_id:
            return _result("error", "channel_id is required.")

        current = (tool_context.invocation_state or {}).get(RUN_CONTEXT_STATE_KEY) if tool_context else None
        current_scheduled = isinstance(current, RuntimeContext) and (
            (current.session_id or "").startswith("sched-")
            or str((current.event or {}).get("triggerSource", "")).startswith("scheduled:")
        )
        channel, thread = default_channel_id, default_thread_ts
        if current_scheduled and isinstance(current, RuntimeContext):
            channel, thread = current.slack_channel_id, current.slack_thread_ts
        # A cached tool must not trust a newly supplied shared-channel context as an
        # additional destination. Only the configured home is permitted, even when scheduled.
        if channel and not channel.startswith("D") and channel != home_channel:
            if home_mode or pin_destination or current_scheduled:
                channel, thread = None, None
        # A scheduled run never posts to a Slack DM, even one its payload names (a legacy job's
        # stored DM): its only Slack destination is the enabled home channel.
        if (pin_destination or current_scheduled) and channel and channel.startswith("D"):
            channel, thread = None, None
        # Scheduled runs keep only a stored home-channel thread; the explicitly enabled home is the
        # only other destination. Shared-channel tools cannot read history or send DMs.
        personal_dm = private_dm or (isinstance(current, RuntimeContext) and current.is_slack_dm)
        # Without the home channel the run's own DM is the only destination. Fail closed when
        # there is none: a missing channel must not fall through to an unrestricted call.
        if not home_mode and (not channel or not channel.startswith("D")):
            return _result("error", "This run has no Slack DM to reply in; use_slack is unavailable.")
        # Confine to the run's own DM and thread whatever the home-channel switch says: the bot
        # token's chat:write reaches every DM, so this check, not the token, is the boundary.
        if (not home_mode or personal_dm) and channel_id != channel:
            return _result("error", "DM runs may reply only in their own DM.")
        # Inside its own DM a run keeps to its own thread, home switch or not; with no recorded
        # thread (``thread`` is None) any explicit ``thread_ts`` is foreign, so it is refused too.
        own_dm = bool(channel and channel.startswith("D") and channel_id == channel)
        if own_dm and thread_ts and thread_ts != thread:
            return _result("error", "DM runs may reply only in their own thread.")
        if home_mode and channel_id not in {home_channel, channel}:
            return _result("error", "Home-channel runs may only post/react in the enabled home channel or their own thread.")
        is_home = bool(home_channel and channel_id == home_channel)
        shared_run = bool(home_mode and (not channel or not channel.startswith("D")))
        if (is_home or shared_run) and (
            method not in _HOME_METHODS
            or channel_id not in {home_channel, channel}
            or (channel_id == channel and thread_ts and thread_ts != thread)
        ):
            return _result("error", "Home-channel runs may only post/react in the enabled home channel or their own thread.")
        if (pin_destination or current_scheduled) and not is_home and (
            not channel or channel_id != channel
            or (thread_ts and thread_ts != thread)
        ):
            return _result("error", "Scheduled runs may use only their stored Slack destination.")
        payload: dict[str, Any] = {"channel": channel_id}
        if text:
            payload["text"] = text
        if timestamp:
            payload["timestamp"] = timestamp
        if name:
            payload["name"] = name
        if (
            not thread_ts
            and thread
            and method == "chat.postMessage"
            and channel_id == channel
        ):
            thread_ts = thread
        if thread_ts:
            payload["thread_ts"] = thread_ts
        # Every top-level home post is a notify_team notification, so its thread has a link.
        if home_channel and channel_id == home_channel and method == "chat.postMessage" and not thread_ts:
            return _result(
                "error",
                "A new home-channel post goes through notify_team (a title plus details in its "
                "thread). use_slack posts there only inside a thread: pass thread_ts.",
            )

        if method in blocked_methods:
            return _result("error", blocked_methods[method])
        try:
            response = _slack_api(method, payload, settings.bot_token)
        except urllib.error.HTTPError as e:
            return _result("error", f"Slack API HTTP {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            return _result("error", f"Slack request error: {e.reason}")
        except Exception as e:  # noqa: BLE001 — a tool must return an error result, never raise
            return _result("error", f"Slack call failed: {e}")

        # Slack signals failure as HTTP 200 + {"ok": false, "error": "..."} — surface it as one.
        if not response.get("ok", False):
            detail = response.get("error") or "unknown_error"
            # Idempotent no-ops, not failures: the reaction is already in the requested state
            # (~half of all use_slack "errors" in prod were a repeat 👀). Report success so the model
            # doesn't retry and the tool-error metric only counts real failures.
            if (method, detail) in _IDEMPOTENT_NOOPS:
                return _result(
                    "success",
                    f"{method}: no change needed ({detail}) — the reaction is already in that state.",
                )
            message = slack_error_message(response)
            if detail == "missing_scope":
                blocked_methods[method] = message
            return _result("error", message)
        # Only what the model needs for a follow-up (the ts to thread under or react on); the full
        # response can carry message bodies and workspace metadata.
        trimmed = {key: response[key] for key in ("ok", "channel", "ts") if key in response}
        return _result("success", json.dumps(trimmed, indent=2))

    return use_slack


NOTIFY_TOOL_NAME = "notify_team"
NOTIFY_DETAILS_MAX = 3500


def _post(settings: SlackSettings, payload: dict[str, Any]) -> tuple[str | None, str]:
    """``chat.postMessage``; the posted ``ts`` or ``None`` and an error text. Never raises."""
    try:
        response = _slack_api("chat.postMessage", payload, settings.bot_token)
    except urllib.error.HTTPError as e:
        return None, f"Slack API HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return None, f"Slack request error: {e.reason}"
    except Exception as e:  # noqa: BLE001 - a tool must return an error result, never raise
        return None, f"Slack call failed: {e}"
    if not response.get("ok", False):
        return None, slack_error_message(response)
    ts = response.get("ts")
    return (ts if is_slack_ts(ts) else None), "Slack returned no message ts."


def make_notify_team(
    settings: SlackSettings,
    *,
    home_channel: str,
    ledger_table: Callable[[], Any] | None = None,
) -> Any:
    """Return the ``notify_team`` tool for the enabled ``home_channel``.

    ``ledger_table`` yields the run-ledger ``Table`` the thread link is written to; without it the
    notification still posts and later replies fall back to the thread root's text.
    """
    from strands import tool

    def record_link(current: RuntimeContext | None, thread_ts: str, title: str, details: str, *, new: bool) -> None:
        if ledger_table is None:
            return
        item = thread_link_item(
            channel_id=home_channel,
            thread_ts=thread_ts,
            title=title,
            details=details,
            origin_session_id=sanitize_session_id(current.session_id or "") if current else "",
            origin=str((current.event or {}).get(TRIGGER_SOURCE_KEY) or "") if current else "",
        )
        kwargs: dict[str, Any] = {} if new else {"ConditionExpression": "attribute_not_exists(task_id)"}
        try:
            ledger_table().put_item(Item=item, **kwargs)
        except Exception as e:  # noqa: BLE001 - the link is reply context; the post already landed
            response = getattr(e, "response", None)
            code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
            if code != "ConditionalCheckFailedException":
                logger.warning("notify_team: thread link write failed (thread_ts=%s)", thread_ts, exc_info=True)

    @tool(name=NOTIFY_TOOL_NAME, context=True)
    def notify_team(
        title: str,
        details: str,
        thread_ts: str = "",
        tool_context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Notify the maintainers in the team's Slack home channel: a title line, details in its thread.

        Posts ``title`` as one short top-level message and ``details`` as the first reply in its
        thread. When someone tags you in a reply in that thread you are invoked there with this
        notification as context, and the conversation continues in the thread like a DM.

        Notify only when maintainers need to know or act: a failure or risk that needs a human, a
        decision you need, or a notable result (link the PR, issue or run). Do not post progress
        updates, routine success or chatter. Batch minor updates into one notification. Keep it to
        one notification per topic: for more news on a topic you already posted, pass that
        notification's ``thread_ts`` so it goes into the existing thread instead of a new post.

        Args:
            title: One plain line saying what happened and whether action is needed, e.g.
                "Nightly eval failed on main: needs a look". At most 150 characters.
            details: The body, in Slack mrkdwn: what happened, evidence and links, and what you
                need from the maintainers.
            thread_ts: The ``thread_ts`` of an earlier notification to follow up in, instead of
                starting a new one.

        Returns:
            ``{"status": "success"|"error", "content": [{"text": ...}]}`` with the thread's
            ``thread_ts`` on success.
        """
        if not settings.bot_token:
            return _result("error", "No Slack bot token configured (STRANDLY_SLACK_BOT_TOKEN).")
        current = (tool_context.invocation_state or {}).get(RUN_CONTEXT_STATE_KEY) if tool_context else None
        current = current if isinstance(current, RuntimeContext) else None
        # A DM run carries one person's private conversation and memory; it never posts to the team.
        if current is not None and (current.slack_channel_id or "").startswith("D"):
            return _result("error", "Slack DM runs cannot post to the team's home channel.")
        heading = " ".join(str(title or "").split())
        body = str(details or "").strip()
        if not heading or not body:
            return _result("error", "Both title and details are required.")
        heading = clip(heading, THREAD_LINK_TITLE_MAX)
        body = clip(body, NOTIFY_DETAILS_MAX)
        follow_up = (thread_ts or "").strip()
        if follow_up:
            if not is_slack_ts(follow_up):
                return _result("error", f"thread_ts {follow_up!r} is not a Slack message ts.")
            ts, error = _post(settings, {
                "channel": home_channel, "thread_ts": follow_up, "text": f"*{heading}*\n{body}",
                "unfurl_links": False,
            })
            if ts is None:
                return _result("error", error)
            record_link(current, follow_up, heading, body, new=False)
            logger.info("notify_team: follow-up posted (thread_ts=%s)", follow_up)
            return _result("success", f"Posted in the existing thread thread_ts={follow_up}.")
        root, error = _post(settings, {"channel": home_channel, "text": heading, "unfurl_links": False})
        if root is None:
            return _result("error", error)
        record_link(current, root, heading, body, new=True)
        reply, error = _post(settings, {
            "channel": home_channel, "thread_ts": root, "text": body, "unfurl_links": False,
        })
        logger.info("notify_team: posted (thread_ts=%s details=%s)", root, "ok" if reply else "failed")
        if reply is None:
            return _result(
                "error",
                f"The title posted (thread_ts={root}) but the details did not: {error} "
                f"Retry only the details with notify_team(title=..., details=..., thread_ts=\"{root}\").",
            )
        return _result(
            "success",
            f"Notification posted, thread_ts={root}. Replies that tag you in that thread reach "
            f"you with this notification as context. For more on this topic, pass thread_ts=\"{root}\".",
        )

    return notify_team
