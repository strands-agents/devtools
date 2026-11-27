"""Slack ingress: the push-based Slack trigger for DMs and the team's home channel.

``POST /slack/events`` reaches this Lambda, which dispatches the agent runtime fire-and-forget the
way the GitHub poller does. Per request: verify the Slack signature (HMAC over
``v0:{timestamp}:{body}``, five-minute replay window) before anything else, including the
``url_verification`` challenge; accept only fresh human messages; authorize the sender against the
secret's ``allowed_users`` or the people directory (fail-closed; an unknown sender gets a silent
200); deduplicate on ``event_id``; derive the thread session (``sessions``); render ids into names
(``profiles``, ``render``) within Slack's 3-second ack budget; launch the run; react with ``eyes``
once it is accepted. It always answers 200, and a rejected dispatch rolls back the dedup record so
the next message retries.

The home channel is handled only with the operator's enable flag, bot token and ``home_channel``
set, and only for messages that @-mention Strandly (thread replies included); messages starting
with ``//`` or addressed to someone else are ignored. Threads there are team-memory sessions, and a
tagged reply in a notification's thread gets that notification's thread link
(``strandly.ops.slack``) as context. The signing secret lives only in this Lambda's own secret
(``STRANDLY_SLACK_SECRETS_ARN``), never in the runtime's config. See docs/deploy.md.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.request
from base64 import b64decode
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from strandly.core.config import SLACK_HOME_CHANNEL_ENABLED, home_channel_enabled, slack_channel_id
from strandly.core.context import SLACK_CHANNEL_KEY
from strandly.ops import metrics, people, runtime_client
from strandly.ops.lambdas import configure_logging
from strandly.ops.lambdas.mention_poller import dedup
from strandly.ops.lambdas.slack_ingress import profiles, render
from strandly.ops.lambdas.slack_ingress.profiles import UserProfile
from strandly.ops.lambdas.slack_ingress.sessions import (
    session_id_for_channel,
    session_id_for_dm,
    thread_root_ts,
)
from strandly.ops.slack import read_thread_link, slack_error_message

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"
_USER_AGENT = "strandly-slack-ingress/1.0"

# Env vars wired by infra/stacks/slack_stack.py.
SECRETS_ARN_ENV = "STRANDLY_SLACK_SECRETS_ARN"
RUNTIME_ARN_ENV = "STRANDLY_RUNTIME_ARN"
DEDUP_TABLE_ENV = "STRANDLY_DEDUP_TABLE"
ALLOWED_USERS_ENV = "STRANDLY_SLACK_ALLOWED_USERS"
PEOPLE_TABLE_ENV = people.PEOPLE_TABLE_ENV
# The run ledger, set only with the home channel enabled: the ingress reads notification thread links.
RUN_LEDGER_TABLE_ENV = "STRANDLY_RUN_LEDGER_TABLE"

# The exact shape of a Slack v0 signature: "v0=" + 64 lowercase hex chars. Pre-validated so a
# doctored header can never reach ``hmac.compare_digest`` with a non-ASCII str — that raises
# TypeError (compare_digest only accepts ASCII strs), which would turn a garbage header into an
# unhandled 500 instead of a clean 401. The format is public, so rejecting on shape early is not a
# timing oracle; the digest itself is still compared constant-time.
_SIGNATURE_RE = re.compile(r"^v0=[0-9a-f]{64}$")

# Slack's documented replay window: reject a signed request whose timestamp is older (or further in
# the future) than this many seconds.
SIGNATURE_MAX_AGE_SECONDS = 60 * 5
# The "started working" ack — a Slack reaction name (mirrors the poller's ACK_REACTION "eyes").
ACK_REACTION = "eyes"
# Truncation cap for the DM text folded into the dispatch prompt (mirrors MENTION_BODY_MAX).
DM_BODY_MAX = 2000
# Dedup-key prefix: keeps slack rows disjoint from the poller's GitHub thread ids in the SAME table.
DEDUP_KEY_PREFIX = "slack-"
# Secret key holding the optional ``U0123ABC:Name`` label map (see ``profiles``) — the path to real
# names that needs no OAuth change at all.
USER_NAMES_SECRET_KEY = "user_names"
# Secret key holding the team's Slack home channel id (``C…``/``G…``); unset ⇒ no home channel.
HOME_CHANNEL_SECRET_KEY = "home_channel"
# ``channel_type`` of a public / private channel message (``im`` is a DM).
CHANNEL_TYPES = frozenset({"channel", "group"})
# A home-channel message starting with this is between humans: Strandly ignores it.
QUIET_PREFIX = "//"
# ``conversations.replies`` for the thread root: socket timeout and how much of the root to keep.
THREAD_ROOT_TIMEOUT_SECONDS = 1.5
THREAD_ROOT_MAX = 2000


# ---------------------------------------------------------------------------
# Step 1: signature verification (pure — fully unit-testable)
# ---------------------------------------------------------------------------
def verify_signature(
    signing_secret: str,
    timestamp: str | None,
    body: str,
    signature: str | None,
    *,
    now: float | None = None,
) -> bool:
    """True iff ``signature`` is Slack's valid HMAC for ``body`` at ``timestamp``.

    Implements Slack's v0 signing scheme: ``v0=hex(hmac_sha256(secret, "v0:{ts}:{body}"))``,
    compared constant-time (``hmac.compare_digest``). **Fail-closed on everything**: a missing
    secret, header, or signature, a signature not shaped ``v0=<64 hex>`` (incl. non-ASCII — which
    would otherwise TypeError out of ``compare_digest``), an unparseable/non-finite timestamp
    (``"inf"`` would otherwise OverflowError), or a timestamp outside the ±5-minute
    replay window all return ``False`` — an unauthenticated request must never proceed.
    """
    if not signing_secret or not timestamp or not signature:
        return False
    if not _SIGNATURE_RE.match(signature):
        return False
    try:
        ts = int(float(timestamp))
    except (TypeError, ValueError, OverflowError):
        # OverflowError: int(float("inf")) — a doctored timestamp must 401, not crash to a 500.
        return False
    current = now if now is not None else time.time()
    if abs(current - ts) > SIGNATURE_MAX_AGE_SECONDS:
        return False
    base = f"v0:{timestamp}:{body}".encode()
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Secrets (this Lambda's OWN secret — never the runtime config secret)
# ---------------------------------------------------------------------------
# Short-TTL cache so a hot Lambda container doesn't refetch per event, while an allow-list edit in
# the secret still propagates within minutes (no redeploy — same rationale as the poller keeping
# its allow-list in the secret rather than baked into the Lambda env).
_SECRET_CACHE: dict[str, Any] = {"value": None, "expires": 0.0}
_SECRET_CACHE_TTL_SECONDS = 300.0


def load_secrets(*, now: float | None = None) -> dict[str, str]:
    """The Slack secret (``bot_token`` / ``signing_secret`` / optional ``allowed_users``), cached.

    Fail-soft to an empty dict on any fetch/parse error — signature verification then fails closed
    on the missing ``signing_secret`` (a broken secret must yield 401s, not a crash-loop).
    """
    current = now if now is not None else time.time()
    if _SECRET_CACHE["value"] is not None and current < _SECRET_CACHE["expires"]:
        return _SECRET_CACHE["value"]
    arn = os.environ.get(SECRETS_ARN_ENV)
    data: dict[str, str] = {}
    if arn:
        try:
            import boto3

            client = boto3.client("secretsmanager")
            raw = client.get_secret_value(SecretId=arn).get("SecretString") or "{}"
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = {str(k): str(v) for k, v in parsed.items()}
        except Exception as e:  # noqa: BLE001 — fail soft; the signature check fails closed
            logger.warning("slack ingress: secret fetch failed: %s", e)
    _SECRET_CACHE["value"] = data
    _SECRET_CACHE["expires"] = current + _SECRET_CACHE_TTL_SECONDS
    return data


def allowed_users(secrets: dict[str, str]) -> frozenset[str]:
    """The DM allow-list: the secret's ``allowed_users`` key, else the env var. Case-preserved.

    Slack user ids are case-sensitive opaque ids (``U0123ABC``), so no lowercasing. An empty
    result means **nobody** is authorized (fail-closed — the endpoint must be explicitly opened).
    """
    raw = secrets.get("allowed_users") or os.environ.get(ALLOWED_USERS_ENV) or ""
    return frozenset(u.strip() for u in raw.split(",") if u.strip())


# ---------------------------------------------------------------------------
# Slack ack (the only outbound Slack call — the network seam tests monkeypatch)
# ---------------------------------------------------------------------------
def _slack_post(method: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
    """POST one Slack Web API method. Fixed ``slack.com`` host — nothing here is attacker-addressable."""
    req = urllib.request.Request(
        f"{SLACK_API}/{method}", data=json.dumps(payload).encode(), method="POST"
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("User-Agent", _USER_AGENT)
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 — fixed slack.com host
        return json.loads(resp.read().decode())


def add_ack_reaction(token: str, channel: str, message_ts: str) -> None:
    """👀 "started working" on the triggering DM. Best-effort — never blocks the 200 ack.

    Posted only after the runtime *accepted* the dispatch (truthful, like the poller's ack).
    Slack returns ``already_reacted`` on a repeat — harmless, so no idempotence handling needed.
    """
    if not token or not channel or not message_ts:
        return
    try:
        result = _slack_post(
            "reactions.add",
            {"channel": channel, "timestamp": message_ts, "name": ACK_REACTION},
            token,
        )
        if not result.get("ok") and result.get("error") != "already_reacted":
            logger.warning("slack ingress: ack reaction failed: %s", slack_error_message(result))
    except Exception:  # noqa: BLE001 — telemetry, not control flow
        logger.warning("slack ingress: ack reaction transport failure; no retry")


# ---------------------------------------------------------------------------
# Steps 5–8: dedup + enrichment + prompt + dispatch
# ---------------------------------------------------------------------------
def event_iso_ts(event: dict[str, Any], envelope: dict[str, Any]) -> str:
    """The message's instant as ISO-8601 — the format ``dedup``'s timestamp comparisons expect.

    Slack timestamps are epoch strings (``"1712345678.123456"``); the shared dedup module parses
    ISO-8601 only, so convert. Falls back to the envelope's integer ``event_time``, then to ""
    (which makes ``record_dispatch`` a no-op fail-open write — acceptable for a malformed event).
    """
    for raw in (event.get("ts"), envelope.get("event_time")):
        try:
            # A None candidate is handled by the except below (float(None) -> TypeError), which
            # is exactly the documented fall-through — silence the checker, keep the behavior.
            return datetime.fromtimestamp(float(raw), tz=timezone.utc).isoformat()  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError, OSError):
            continue
    return ""


@dataclass(frozen=True)
class DirectMessage:
    """Every field of a DM (or home-channel) message the dispatch needs — parsed once, in one place.

    ``origin`` is the thread link of the notification a home-channel reply answers (see
    ``strandly.ops.slack``); without one, ``thread_root`` is the thread root's text when it is
    Strandly's own post (filled by :func:`fetch_thread_root`).
    """

    user_id: str
    channel_id: str
    team_id: str
    text: str = ""
    message_ts: str = ""
    thread_ts: str = ""
    files: tuple[Any, ...] = ()
    bot_user_id: str = ""
    channel_type: str = "im"
    app_id: str = ""
    thread_root: str = ""
    origin: Mapping[str, str] | None = None

    @property
    def in_channel(self) -> bool:
        """A home-channel message rather than a DM."""
        return self.channel_type in CHANNEL_TYPES

    @property
    def is_thread_reply(self) -> bool:
        return bool(self.thread_ts) and self.thread_ts != self.message_ts

    @property
    def root_ts(self) -> str:
        """The thread this DM belongs to — and where the reply goes (``thread_ts`` or its own ``ts``)."""
        return thread_root_ts(self.message_ts, self.thread_ts or None)


def bot_user_id(envelope: Mapping[str, Any]) -> str:
    """The bot user id this app is authorized as — the agent's own Slack identity.

    Read from ``authorizations[0].user_id`` (or the legacy ``authed_users``), both of which Slack
    puts in the envelope for free. Handed to the agent so it recognizes ``<@U…>`` references *to
    itself* instead of reading them as some third party — the Slack twin of the prompt's "Your
    GitHub identity" section. "" when Slack omits it.
    """
    auth = envelope.get("authorizations")
    if isinstance(auth, list) and auth and isinstance(auth[0], dict):
        value = auth[0].get("user_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    legacy = envelope.get("authed_users")
    if isinstance(legacy, list) and legacy and isinstance(legacy[0], str):
        return legacy[0].strip()
    return ""


def parse_message(
    envelope: Mapping[str, Any], home_channel: str | None
) -> DirectMessage | None:
    """The event as a :class:`DirectMessage`, or ``None`` when it isn't one we answer (step 3).

    The filter: only ``message`` events, in a DM (``channel_type == "im"``) or in the
    ``home_channel`` (``channel``/``group``; any other channel is ignored, and with no home channel
    every channel is). ``subtype`` drops edits/deletes/joins, and ``bot_id`` drops bot posts,
    ours included — without which the bot's ``use_slack`` post would re-trigger this ingress in a
    loop. In the home channel, threads included, only a message that @-mentions Strandly (the
    envelope's bot user id) is handled; one starting with :data:`QUIET_PREFIX` or opening with an
    @-mention of someone else is between humans and ignored. A message with no
    author, channel or ``ts`` can't be authorized or answered, so it is ignored too.
    """
    event = envelope.get("event")
    if not isinstance(event, dict):
        return None
    if event.get("type") != "message" or event.get("subtype") or event.get("bot_id"):
        return None
    channel_type = str(event.get("channel_type") or "")
    channel_id = str(event.get("channel") or "")
    text = str(event.get("text") or "")
    bot_id = bot_user_id(envelope)
    if channel_type in CHANNEL_TYPES:
        if not home_channel or channel_id != home_channel:
            return None
        if text.lstrip().startswith(QUIET_PREFIX):
            return None
        # The channel is shared: only a message that tags Strandly is for Strandly.
        if not render.mentions(text, bot_id):
            return None
        addressee = render.leading_mention(text)
        if addressee and bot_id and addressee != bot_id:
            return None
    elif channel_type != "im":
        return None
    user_id = str(event.get("user") or "")
    message_ts = str(event.get("ts") or "")
    # No ``ts`` means no thread root: the session id would collapse to one never-ending
    # per-channel session and the reply would have nowhere to thread. Slack always
    # sends ``ts``; drop the event rather than guess.
    if not user_id or not channel_id or not message_ts or user_id == bot_id:
        return None
    files = event.get("files")
    return DirectMessage(
        user_id=user_id,
        channel_id=channel_id,
        team_id=str(envelope.get("team_id") or ""),
        text=text,
        message_ts=message_ts,
        thread_ts=str(event.get("thread_ts") or ""),
        files=tuple(files) if isinstance(files, list) else (),
        bot_user_id=bot_id,
        channel_type=channel_type,
        app_id=str(envelope.get("api_app_id") or ""),
    )


def fetch_thread_root(token: str, dm: DirectMessage) -> str:
    """The thread root's text when it is Strandly's own post (an alarm, a job result), else ``""``.

    One ``conversations.replies`` call (needs ``channels:history`` / ``groups:history``), bounded
    by :data:`THREAD_ROOT_TIMEOUT_SECONDS`. Never raises: without the root the run still has its
    own session history.
    """
    if not token or not dm.in_channel or not dm.is_thread_reply:
        return ""
    try:
        payload = profiles._slack_get(
            "conversations.replies",
            {"channel": dm.channel_id, "ts": dm.thread_ts, "limit": "1", "inclusive": "true"},
            token,
            timeout=THREAD_ROOT_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 — context, not control flow
        logger.warning("slack ingress: conversations.replies transport failure; no retry")
        return ""
    if not payload.get("ok"):
        logger.warning("slack ingress: conversations.replies: %s", slack_error_message(payload))
        return ""
    messages = payload.get("messages")
    root = messages[0] if isinstance(messages, list) and messages else None
    if not isinstance(root, dict) or root.get("ts") != dm.thread_ts:
        return ""
    ours = (dm.bot_user_id and root.get("user") == dm.bot_user_id) or (
        dm.app_id and root.get("app_id") == dm.app_id
    )
    if not ours:
        return ""
    text = str(root.get("text") or "").strip()
    if len(text) > THREAD_ROOT_MAX:
        text = text[:THREAD_ROOT_MAX] + "... (truncated)"
    return text


def resolve_people(
    dm: DirectMessage, secrets: Mapping[str, str], person: people.Person | None = None
) -> dict[str, UserProfile]:
    """Profiles for the sender and the (few) users they mentioned. Never raises.

    The sender's people-directory ``name`` (when known) is folded in as a label override ahead of
    the secret's ``user_names`` map (the directory is the curated source, the map the legacy
    fallback); ``users.info`` still wins over both when the scope is granted.

    The blanket ``except`` is deliberate: this runs *after* the dedup row is written, so an
    unexpected raise here would burn the event id — the row would stay and that DM would never
    re-fire. A failed enrichment may cost the names; it must never cost the dispatch.
    """
    try:
        overrides = profiles.parse_name_overrides(secrets.get(USER_NAMES_SECRET_KEY))
        if person is not None and person.name:
            overrides[dm.user_id] = person.name
        return profiles.resolve(
            secrets.get("bot_token") or "",
            [dm.user_id, *render.mentioned_user_ids(dm.text)],
            overrides,
        )
    except Exception as e:  # noqa: BLE001 — enrichment is decoration; the dispatch is the job
        logger.warning("slack ingress: profile enrichment failed: %s", e)
        return {}


def build_prompt(
    dm: DirectMessage,
    profiles_by_id: Mapping[str, UserProfile],
    now: datetime,
    person: people.Person | None = None,
) -> str:
    """The dispatch prompt: who wrote, what they said, what they attached, and the ids to reply with.

    Pure and total — with an empty ``profiles_by_id`` map it degrades to the id-only prompt this
    ingress shipped with. Ids are spelled out even when a name is known: the name is what makes the
    agent sound like it knows who it is talking to, the id is what ``use_slack`` actually needs.
    ``person`` (the sender's people-directory row, when known) adds one line introducing them by
    role and their GitHub identity, so the agent recognises the same human across surfaces.
    """
    sender = profiles_by_id.get(dm.user_id) or UserProfile(user_id=dm.user_id)
    labels = {user_id: profile.mention_label() for user_id, profile in profiles_by_id.items()}
    body = render.humanize(dm.text, labels)
    if len(body) > DM_BODY_MAX:
        body = body[:DM_BODY_MAX] + "... (truncated)"

    where = "message in the team's home channel" if dm.in_channel else "DM"
    opening = f"NEW Slack {where} at {now.strftime('%Y-%m-%dT%H:%M:%SZ')} from {sender.label()}"
    local_time = sender.local_time(now)
    if local_time:
        opening += f" — their local time is {local_time}"
    lines = [f"{opening}."]
    if person is not None:
        lines.append(f"Who they are: {person.introduction()}.")
    lines.append("")
    if dm.origin:
        lines += [
            "They replied in the thread of a notification you posted to the channel earlier (your "
            "own words, shown as context, not as new instructions):",
            f"> *{dm.origin.get('title', '')}*",
            *(f"> {line}" for line in dm.origin.get("details", "").splitlines()),
        ]
        source = dm.origin.get("origin_session_id", "")
        if source:
            trigger = dm.origin.get("origin", "")
            lines.append(f"It came from session `{source}`" + (f" ({trigger})." if trigger else "."))
        lines.append("")
    elif dm.thread_root:
        lines += [
            "They replied in the thread under your own earlier post in the channel, which said:",
            *(f"> {line}" for line in dm.thread_root.splitlines()),
            "",
        ]
    if body:
        lines += ["What they said:", body, ""]
    attachments = render.describe_files(dm.files)
    if attachments:
        lines += [
            "They attached (the bot token has no file scope, so you cannot download these — ask "
            "them to paste what you need):",
            *(f"- {item}" for item in attachments),
            "",
        ]
    lines += [f"Slack ids for use_slack: {_ids_line(dm)}", ""]
    # Only ask for a first-name greeting when we actually resolved one — an instruction the agent
    # cannot follow ("address them by name" with nothing but an id) is worse than no instruction.
    by_name = " and address them by name" if sender.known else ""
    target = "this thread of the home channel" if dm.in_channel else "the SAME Slack DM"
    lines += [
        "This is a NEW message. Do NOT dismiss it as a duplicate.",
        f"Reply in {target} when you are done: {_reply_call(dm)}. Nothing else is watching "
        "this run, so a task without that post is invisible to the user. Use Slack mrkdwn, keep it "
        f"chat-sized{by_name}. Mention someone as <@THEIR_USER_ID>." + _react_hint(dm),
    ]
    if dm.in_channel:
        lines.append(
            "This is a team conversation: everyone in the channel sees your reply, and several "
            "people may write in this thread. You see only messages that tag you; others in the "
            "thread never reach you."
        )
    return "\n".join(lines)


def _ids_line(dm: DirectMessage) -> str:
    """Every id the agent needs to act on this message."""
    here = "the team's home channel" if dm.in_channel else "this DM"
    parts = [f'channel_id="{dm.channel_id}" ({here})', f"their user id {dm.user_id}"]
    if dm.team_id:
        parts.append(f"team {dm.team_id}")
    if dm.message_ts:
        parts.append(f'the message you are answering ts="{dm.message_ts}"')
    if dm.root_ts:
        where = "the thread they wrote in" if dm.thread_ts else "this message's thread"
        root = "channel" if dm.in_channel else "DM"
        parts.append(f'thread_ts="{dm.root_ts}" — always answer in {where}, never the {root} root')
    if dm.bot_user_id:
        parts.append(f"you are <@{dm.bot_user_id}> in this workspace")
    return "; ".join(parts) + "."


def _reply_call(dm: DirectMessage) -> str:
    """The exact ``use_slack`` call that lands the reply where the human is looking."""
    call = f'use_slack(action="chat.postMessage", channel_id="{dm.channel_id}"'
    if dm.root_ts:
        call += f', thread_ts="{dm.root_ts}"'
    return call + ", text=...)"


def _react_hint(dm: DirectMessage) -> str:
    """The message ``ts`` the agent needs to react to it."""
    if not dm.message_ts:
        return ""
    return (
        ' To react on their message: use_slack(action="reactions.add", '
        f'channel_id="{dm.channel_id}", timestamp="{dm.message_ts}", name="white_check_mark").'
    )


def dispatch(
    runtime_arn: str | None,
    session_id: str,
    prompt: str,
    slack_context: dict[str, Any],
) -> dict[str, Any]:
    """Invoke the deployed runtime fire-and-forget via strandly's own ``launch_run``.

    ``slack_context`` rides along as plain extra payload keys (like the poller's
    ``ackReactionsUrl``) — NOT a GitHub event context — so the run stays fire-and-forget. Lazy
    serving imports, same as the poller: a request that dispatches nothing never imports them.
    """
    from strandly.ops import runtime_client

    arn = runtime_client.resolve_runtime_arn(runtime_arn)
    region = runtime_client.resolve_region(None)
    if not arn or not region:
        raise RuntimeError(
            f"cannot dispatch: runtime_arn={arn!r} region={region!r} "
            "(set STRANDLY_RUNTIME_ARN / AWS_REGION)"
        )
    return runtime_client.launch_run(arn, region, session_id, prompt, slack_context)


def _dispatch_accepted(result: Any) -> bool:
    """Only ``accepted`` counts — an HTTP-200 error body is a rejection.

    A DM on a session with a run in flight supersedes that run and is ``accepted`` too; a
    redelivery of the same Slack event while its run is going is ``accepted`` with
    ``duplicate: true``.
    """
    if not isinstance(result, dict):
        return False
    return result.get("status") == "accepted"


def _dynamodb_client() -> Any | None:
    if not any(os.environ.get(name) for name in (DEDUP_TABLE_ENV, PEOPLE_TABLE_ENV, RUN_LEDGER_TABLE_ENV)):
        return None
    import boto3

    return boto3.client("dynamodb")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def process_event(
    envelope: dict[str, Any],
    *,
    secrets: dict[str, str],
    ddb_client: Any | None,
    now: datetime,
) -> str:
    """Process one (already signature-verified) ``event_callback``; returns an outcome label.

    Outcomes: ``ignored`` (not a fresh user DM / home-channel message), ``unauthorized``, ``duplicate``, ``dispatched``,
    ``dispatch-error``, ``dispatch-maybe`` (sent but the answer was lost — the dedup row is kept so a
    redelivery cannot run it twice). Every outcome maps to a 200 for Slack — the label is for logs/metrics.
    """
    event = envelope.get("event") or {}

    # Step 3: only fresh, human DMs and home-channel messages — ``parse_message`` owns the filter and pulls out every
    # field the prompt is built from (ids, ts, thread, attachments, our own bot id).
    dm = parse_message(envelope, (slack_channel_id(secrets.get(HOME_CHANNEL_SECRET_KEY))
         if home_channel_enabled(os.environ.get(SLACK_HOME_CHANNEL_ENABLED)) and secrets.get("bot_token")
         else None))
    event_id = envelope.get("event_id") or ""
    if dm is None or not event_id:
        return "ignored"

    # Step 4: authorize — the static allow-list first (no I/O), then the people directory (one
    # DynamoDB query, fail-closed; also looked up for a static user because it is what tells the
    # prompt WHO they are). Silent no-op for strangers (never reveal the bot exists), fail-closed on
    # an empty allow-list with no directory.
    directory = people.PeopleDirectory(ddb_client, os.environ.get(PEOPLE_TABLE_ENV))
    person = directory.find_by_slack(dm.user_id)
    if dm.user_id not in allowed_users(secrets) and not directory.authorizes(person):
        logger.info(
            "slack ingress: unauthorized user %s in %s — ignoring", dm.user_id, dm.channel_id
        )
        metrics.emit({metrics.UNAUTHORIZED: 1}, surface=metrics.SURFACE_SLACK)
        return "unauthorized"

    # Step 5: dedup on the event id — absorbs Slack's automatic retries (same event_id). Same
    # intent-before-invoke + rollback-on-failure shape as the poller: the conditional
    # ``record_dispatch`` closes the race between a retry delivery and a slow first attempt.
    table = os.environ.get(DEDUP_TABLE_ENV)
    dedup_key = f"{DEDUP_KEY_PREFIX}{event_id}"
    iso_ts = event_iso_ts(event, envelope)
    if dedup.already_dispatched(ddb_client, table, dedup_key, iso_ts):
        logger.info("slack ingress: event %s already dispatched — skipping", event_id)
        return "duplicate"
    if not dedup.record_dispatch(ddb_client, table, dedup_key, iso_ts):
        logger.info("slack ingress: lost the intent race for event %s — skipping", event_id)
        return "duplicate"

    # Steps 6–8: session id + enriched prompt + fire-and-forget dispatch (fail-closed on
    # rejection). The enrichment sits here, after dedup and authorization, so a stranger's DM and a
    # Slack retry cost zero Slack API calls.
    if dm.in_channel:
        session_id = session_id_for_channel(dm.channel_id, dm.root_ts)
    else:
        session_id = session_id_for_dm(dm.team_id, dm.channel_id, dm.root_ts)
    # These ride along as extra payload keys. ``SLACK_CHANNEL_KEY`` ("slackChannelId") is imported
    # from core.context on purpose: it is the marker the agent side gates ``use_slack`` on
    # (``RuntimeContext.slack_channel_id``), so the producer and the consumer cannot drift apart.
    slack_context: dict[str, Any] = {
        "slackTeamId": dm.team_id,
        SLACK_CHANNEL_KEY: dm.channel_id,
        "slackUserId": dm.user_id,
        "slackEventTs": dm.message_ts,
        # The message ts identifies this DM, so a Slack redelivery of the same event while its
        # run is going is a no-op at the runtime instead of superseding that run. The hash stands
        # in for the prompt, which carries the delivery time.
        "triggerId": dm.message_ts,
        "triggerHash": hashlib.sha256(dm.text.encode("utf-8", "replace")).hexdigest(),
        "triggerSource": f"slack:{dm.user_id}",
    }
    if dm.root_ts:
        # Symmetric with ``slackEventTs``: the thread the reply belongs in — the DM's ``thread_ts``,
        # or its own ``ts`` for a top-level message (every conversation is a thread now).
        slack_context["slackThreadTs"] = dm.root_ts
    try:
        # Prompt building lives INSIDE the rollback guard: the dedup row is already written, so
        # anything that raises between here and the invoke would otherwise burn this event id and
        # the DM would never re-fire.
        introduced = directory.introduces(person)
        token = secrets.get("bot_token") or ""
        if dm.is_thread_reply and dm.in_channel:
            link = read_thread_link(
                ddb_client, os.environ.get(RUN_LEDGER_TABLE_ENV), dm.channel_id, dm.thread_ts
            )
            dm = replace(dm, origin=link) if link else replace(dm, thread_root=fetch_thread_root(token, dm))
        prompt = build_prompt(dm, resolve_people(dm, secrets, introduced), now, person=introduced)
        result = dispatch(os.environ.get(RUNTIME_ARN_ENV), session_id, prompt, slack_context)
    except Exception as e:  # noqa: BLE001 — must still 200 to Slack; roll back so the next DM re-fires
        if runtime_client.launch_maybe_accepted(e):
            # Sent, answer lost (read timeout / connection dropped): the run may be live. Keep the
            # dedup row so a Slack redelivery cannot start it a second time; report, don't retry.
            logger.warning(
                "slack ingress: dispatch MAYBE accepted for %s (%s: %s) — keeping the dedup row; "
                "not re-dispatching (a retry could run it twice)",
                session_id,
                type(e).__name__,
                e,
            )
            metrics.emit({metrics.DISPATCH_MAYBE: 1}, surface=metrics.SURFACE_SLACK)
            return "dispatch-maybe"
        logger.exception("slack ingress: dispatch raised for %s: %s", session_id, e)
        dedup.clear_dispatch(ddb_client, table, dedup_key)
        metrics.emit({metrics.DISPATCH_FAILED: 1}, surface=metrics.SURFACE_SLACK)
        return "dispatch-error"
    if not _dispatch_accepted(result):
        logger.error(
            "slack ingress: dispatch REJECTED for %s (result=%r); rolling back dedup",
            session_id,
            result,
        )
        dedup.clear_dispatch(ddb_client, table, dedup_key)
        metrics.emit({metrics.DISPATCH_FAILED: 1}, surface=metrics.SURFACE_SLACK)
        return "dispatch-error"

    logger.info(
        "slack ingress: dispatched %s for user %s (event %s)", session_id, dm.user_id, event_id
    )
    metrics.emit({metrics.DISPATCHED: 1}, surface=metrics.SURFACE_SLACK)
    # Step 9: best-effort 👀 ack on the triggering message (truthful — only after acceptance).
    add_ack_reaction(secrets.get("bot_token") or "", dm.channel_id, dm.message_ts)
    return "dispatched"


def _raw_body(request: dict[str, Any]) -> str:
    """The request body exactly as Slack sent it (base64-decoded if API Gateway encoded it).

    The signature is computed over the *verbatim* body bytes, so this must not re-serialize.
    """
    body = request.get("body") or ""
    if request.get("isBase64Encoded"):
        try:
            return b64decode(body).decode()
        except (ValueError, UnicodeDecodeError):
            return ""
    return body


def _headers(request: dict[str, Any]) -> dict[str, str]:
    """Lowercased header map (HTTP/2 lowercases on the wire; don't depend on Slack's casing)."""
    return {str(k).lower(): str(v) for k, v in (request.get("headers") or {}).items()}


def _response(status_code: int, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body or {}),
    }


def lambda_handler(event: dict[str, Any], context: Any | None = None) -> dict[str, Any]:
    """AWS Lambda entrypoint — API Gateway (HTTP API, payload v2.0) proxy handler."""
    configure_logging()
    body_raw = _raw_body(event)
    headers = _headers(event)
    secrets = load_secrets()

    # Step 1: signature — before ANY parsing/echoing (Slack signs every delivery, the
    # url_verification challenge included). Fail-closed → 401.
    if not verify_signature(
        secrets.get("signing_secret") or "",
        headers.get("x-slack-request-timestamp"),
        body_raw,
        headers.get("x-slack-signature"),
    ):
        logger.warning("slack ingress: signature verification failed — rejecting")
        return _response(401, {"error": "invalid signature"})

    try:
        payload = json.loads(body_raw)
    except (json.JSONDecodeError, TypeError):
        # Signed but unparseable — shouldn't happen; 200 so Slack doesn't retry garbage.
        return _response(200, {"ok": True, "outcome": "ignored"})
    if not isinstance(payload, dict):
        return _response(200, {"ok": True, "outcome": "ignored"})

    # Step 2: the one-time Event Subscriptions URL handshake.
    if payload.get("type") == "url_verification":
        return _response(200, {"challenge": payload.get("challenge") or ""})

    outcome = "ignored"
    if payload.get("type") == "event_callback":
        try:
            outcome = process_event(
                payload,
                secrets=secrets,
                ddb_client=_dynamodb_client(),
                now=datetime.now(timezone.utc),
            )
        except Exception as e:  # noqa: BLE001 — one bad event must not 5xx (Slack would retry it)
            logger.exception("slack ingress: error processing event: %s", e)
            outcome = "error"

    # Step 9: 200 regardless of dispatch outcome — dedup rollback (not an HTTP retry) is the
    # fail-closed retry path; a non-2xx would make Slack re-deliver into the same failure.
    return _response(200, {"ok": True, "outcome": outcome})
