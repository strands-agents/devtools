"""Slack user identity for the ingress: user ids into names and local time.

Sources, best first and both fail-soft: ``users.info`` (needs the optional ``users:read`` scope) and
the Slack secret's ``user_names`` map. Lookups sit inside Slack's 3-second ack budget, so they are
capped in count, per-call timeout and total wall clock, and cached per container including misses;
``resolve`` never raises. Names are untrusted (anyone the sender mentions can choose one), so every
label is flattened through :func:`render.one_line` before it reaches the prompt.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from strandly.ops.lambdas.slack_ingress.render import one_line

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"
_USER_AGENT = "strandly-slack-ingress/1.0"

# Deliberately tighter than the ack path's 10s: an enrichment call happens BEFORE the dispatch, so
# its timeout eats into Slack's 3-second ack budget. Two seconds is plenty for users.info; a slower
# Slack simply costs us the name, not the dispatch.
LOOKUP_TIMEOUT_SECONDS = 2.0
# Max users.info calls per request (sender first, then whoever they mentioned).
MAX_LOOKUPS = 3
# Hard wall-clock ceiling for ALL lookups in one request. The per-call timeout alone would allow
# MAX_LOOKUPS x LOOKUP_TIMEOUT_SECONDS (6s) against a hanging Slack, blowing the 3-second ack window
# and making Slack retry the event. Past this, remaining ids stay unresolved (names lost, dispatch
# kept) — which is the whole trade this module is built around.
#
# Enforced as a real ceiling, not just a gate: each call's socket timeout is the *smaller* of
# LOOKUP_TIMEOUT_SECONDS and the budget still unspent, so N calls cannot each burn the full
# per-call timeout after passing a check made before any of them started.
TOTAL_LOOKUP_BUDGET_SECONDS = 2.5
# Don't start a lookup that can't plausibly finish: below this much remaining budget the call would
# only add latency to a name we're about to abandon anyway.
MIN_LOOKUP_SECONDS = 0.3
# Profiles barely change and a wrong-for-15-minutes display name is harmless.
CACHE_TTL_SECONDS = 900.0
# Bound on the per-container cache. A warm container only ever sees the allow-list plus whoever they
# mention, so this is never hit in practice — it exists so a pathological workspace can't grow the
# dict without limit. Cheapest correct policy: drop everything and re-warm.
MAX_CACHE_ENTRIES = 256

# user_id -> (expires_at, profile). Misses are cached as an empty profile (see resolve).
_CACHE: dict[str, tuple[float, UserProfile]] = {}
# "This install has no users:read" memo. Time-boxed rather than permanent: if the scope is granted,
# a warm container picks it up at the next expiry instead of degrading until it's recycled.
_SCOPE_MISSING = {"until": 0.0}

# Slack timezone offsets are seconds east of UTC; anything outside ±14h is garbage, not a timezone.
_MAX_TZ_OFFSET_SECONDS = 14 * 3600


@dataclass(frozen=True)
class UserProfile:
    """What we know about one Slack user. Everything optional — an id is always enough."""

    user_id: str
    name: str = ""  # human name ("Example Maintainer")
    handle: str = ""  # @handle ("example-handle")
    tz: str = ""  # IANA-ish label Slack reports ("America/Los_Angeles")
    tz_offset: int | None = None  # seconds east of UTC

    @property
    def known(self) -> bool:
        """True when we have *some* human label — the trigger for the secret-map fallback."""
        return bool(self.name or self.handle)

    def label(self) -> str:
        """How the sender is introduced in the prompt. Always ends with the id (tools need it)."""
        if self.name and self.handle and self.handle.lower() != self.name.lower():
            return f"{self.name} (@{self.handle}, {self.user_id})"
        display = self.name or self.handle
        return f"{display} ({self.user_id})" if display else f"user {self.user_id}"

    def mention_label(self) -> str:
        """What a ``<@id>`` inside the message text is rewritten to — id kept so it stays usable."""
        display = self.handle or self.name
        return f"@{display} ({self.user_id})" if display else f"<@{self.user_id}>"

    def local_time(self, now: datetime) -> str:
        """``"21:04 America/Los_Angeles"`` — or "" when Slack told us no offset."""
        if self.tz_offset is None:
            return ""
        stamp = (now + timedelta(seconds=self.tz_offset)).strftime("%H:%M")
        return f"{stamp} {self.tz}" if self.tz else stamp


def parse_name_overrides(raw: str | None) -> dict[str, str]:
    """``"U0123:Example Maintainer,U0456:Bob"`` -> ``{"U0123": "Example Maintainer", "U0456": "Bob"}``.

    The secret's ``user_names`` key: the no-extra-scope way to give the agent real names. Malformed
    entries are skipped rather than failing the request (a typo in the secret must not stop DMs).
    """
    overrides: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        pair = chunk.strip()
        if not pair or ":" not in pair:
            continue
        user_id, _, name = pair.partition(":")
        if user_id.strip() and name.strip():
            overrides[user_id.strip()] = name.strip()
    return overrides


def _slack_get(
    method: str, params: dict[str, str], token: str, *, timeout: float = LOOKUP_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """GET one Slack Web API method. The network seam tests monkeypatch.

    GET (not the JSON POST the ack path uses) because ``users.info`` is a query-param method —
    Slack does not accept a JSON body for it. Fixed ``slack.com`` host; ``method``/``params`` are
    never attacker-controlled (we pass a literal method and a Slack-issued user id).

    ``timeout`` is passed in per call (not read from the constant) so :func:`resolve` can hand each
    lookup only the wall-clock budget that is actually left.
    """
    url = f"{SLACK_API}/{method}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", _USER_AGENT)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def _first_str(*values: Any) -> str:
    """The first usable string, flattened to one capped line (see :func:`render.one_line`).

    The flattening is the security-relevant part: these values are display names and timezone
    labels chosen by Slack users — including users nobody allow-listed — and they land in a prompt
    whose structure is newline-delimited.
    """
    for value in values:
        cleaned = one_line(value)
        if cleaned:
            return cleaned
    return ""


def _tz_offset(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if abs(value) <= _MAX_TZ_OFFSET_SECONDS else None


def profile_from_payload(user_id: str, payload: Mapping[str, Any]) -> UserProfile:
    """Map a ``users.info`` response onto :class:`UserProfile` (pure; tolerant of missing keys)."""
    # Narrow via locals rather than inline conditionals: ``x.get(k) if isinstance(x.get(k), dict)``
    # doesn't narrow for a type checker (the two ``get`` calls are unrelated expressions to it).
    raw_user = payload.get("user")
    user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
    raw_profile = user.get("profile")
    profile: dict[str, Any] = raw_profile if isinstance(raw_profile, dict) else {}
    return UserProfile(
        user_id=user_id,
        name=_first_str(
            profile.get("real_name_normalized"), profile.get("real_name"), user.get("real_name")
        ),
        handle=_first_str(
            profile.get("display_name_normalized"), profile.get("display_name"), user.get("name")
        ),
        tz=_first_str(user.get("tz")),
        tz_offset=_tz_offset(user.get("tz_offset")),
    )


def _lookup(token: str, user_id: str, now: float, timeout: float) -> UserProfile | None:
    """One ``users.info`` call. Never raises.

    Returns the profile (possibly empty — an unknown user is just an id), or ``None`` for a
    **transient** refusal (``ratelimited`` / HTTP 429). ``None`` means "don't remember this as a
    miss": caching a rate-limited id would cost the name for the whole TTL over a condition that
    clears in seconds, and it tells :func:`resolve` to stop asking for the rest of *this* request
    (Slack rate-limits per app, so the next id would be refused too).
    """
    try:
        payload = _slack_get("users.info", {"user": user_id}, token, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — enrichment is best-effort, never control flow
        logger.warning("slack ingress: users.info failed for %s: %s", user_id, e)
        # A 429 raises HTTPError rather than answering ``ok: false`` — same transient meaning.
        return None if getattr(e, "code", None) == 429 else UserProfile(user_id=user_id)
    if not isinstance(payload, dict) or not payload.get("ok"):
        error = (payload or {}).get("error") if isinstance(payload, dict) else "bad_response"
        if error == "missing_scope":
            # No users:read on this install. Stop asking for a while: every further call would fail
            # the same way and each one costs the request path real milliseconds.
            _SCOPE_MISSING["until"] = now + CACHE_TTL_SECONDS
            logger.info(
                "slack ingress: users.info needs the users:read scope — falling back to ids/"
                "user_names, and not retrying for %.0fs",
                CACHE_TTL_SECONDS,
            )
        elif error == "ratelimited":
            logger.info("slack ingress: users.info rate-limited — skipping names for this request")
            return None
        else:
            logger.info("slack ingress: users.info %s -> %s", user_id, error)
        return UserProfile(user_id=user_id)
    return profile_from_payload(user_id, payload)


def _monotonic() -> float:
    """Indirection over ``time.monotonic`` so tests can drive the wall-clock budget."""
    return time.monotonic()


def _scope_missing(now: float) -> bool:
    return now < float(_SCOPE_MISSING["until"])


def _remember(user_id: str, profile: UserProfile, now: float) -> None:
    if len(_CACHE) >= MAX_CACHE_ENTRIES:
        _CACHE.clear()
    _CACHE[user_id] = (now + CACHE_TTL_SECONDS, profile)


def _cached(user_id: str, now: float) -> UserProfile | None:
    entry = _CACHE.get(user_id)
    if entry is None:
        return None
    expires, profile = entry
    if now >= expires:
        _CACHE.pop(user_id, None)
        return None
    return profile


def resolve(
    token: str,
    user_ids: Iterable[str],
    fallback_names: Mapping[str, str] | None = None,
    *,
    now: float | None = None,
    limit: int = MAX_LOOKUPS,
) -> dict[str, UserProfile]:
    """Best-known profile per id, in one bounded, never-raising pass.

    Order per id: warm cache -> ``users.info`` (while both budgets last and the scope is not
    known-missing) -> the secret's ``user_names`` label. Only the API's answer is cached — including
    an empty one, which is what makes a missing scope or a deleted user cost a single call. The
    fallback label is applied *after* the cache read, so editing the secret takes effect at once.

    ``user_ids`` order is significant: the caller puts the *sender* first, so the one name that
    always matters wins the lookup budget over anyone they merely mentioned.
    """
    current = now if now is not None else time.time()
    started = _monotonic()
    fallbacks = fallback_names or {}
    resolved: dict[str, UserProfile] = {}
    budget = limit
    for user_id in user_ids:
        if not user_id or user_id in resolved:
            continue
        profile = _cached(user_id, current)
        if profile is None:
            # What's left of the wall clock also becomes this call's socket timeout, so the ceiling
            # holds even when every lookup hangs (a check-only gate allowed 2 x 2s + a late check).
            remaining = TOTAL_LOOKUP_BUDGET_SECONDS - (_monotonic() - started)
            timeout = min(LOOKUP_TIMEOUT_SECONDS, remaining)
            if (
                token
                and budget > 0
                and timeout >= MIN_LOOKUP_SECONDS
                and not _scope_missing(current)
            ):
                budget -= 1
                looked_up = _lookup(token, user_id, current, timeout)
                if looked_up is None:
                    # Transient (rate-limited): not a miss to cache, and no point trying the next id
                    # in this request either — Slack rate-limits the app, not the id.
                    budget = 0
                    profile = UserProfile(user_id=user_id)
                else:
                    profile = looked_up
                    _remember(user_id, profile, current)
            else:
                profile = UserProfile(user_id=user_id)
        if not profile.known:
            fallback = one_line(fallbacks.get(user_id, ""))
            if fallback:
                profile = UserProfile(
                    user_id=user_id, name=fallback, tz=profile.tz, tz_offset=profile.tz_offset
                )
        resolved[user_id] = profile
    return resolved


def reset_cache() -> None:
    """Drop the per-container profile cache and the ``missing_scope`` memo (tests use this)."""
    _CACHE.clear()
    _SCOPE_MISSING["until"] = 0.0
