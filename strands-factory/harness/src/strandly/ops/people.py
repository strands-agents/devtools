"""People directory: who may trigger Strandly, and who they are across GitHub and Slack.

One DynamoDB row per person with any of GitHub login (stored lowercase), Slack user id, name and
role, so an ingress authorizes and introduces a caller in one lookup. A row may also be bound to one
portal user (its Cognito ``sub``), which is how the dashboard API knows who a signed-in caller is.
The agent runtime has no grant on the table; the ingress Lambdas read it and operators write it.
Authorization is fail-closed: no table, an error, a malformed row or an ambiguous identity all mean
"unknown", and the directory only ever adds to the legacy secret allow-lists. Lookups are cached per
container for a short TTL, hits and misses but never errors. See docs/deploy.md.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from strandly.core.constants import (
    PEOPLE_COGNITO_GSI_NAME,
    PEOPLE_GITHUB_GSI_NAME,
    PEOPLE_SLACK_GSI_NAME,
)

logger = logging.getLogger(__name__)

# Env var naming the table (wired onto the ingress Lambdas by infra/stacks/ingress_stack.py and
# slack_stack.py; deliberately NOT folded into the runtime's config secret).
PEOPLE_TABLE_ENV = "STRANDLY_PEOPLE_TABLE"

# Per-container cache TTL for lookups (hits and misses). Matches the secret cache in the Slack
# ingress and the org-membership cache in the poller: an operator edit lands within minutes without
# a redeploy, and a repeat lookup inside one poll costs nothing.
CACHE_TTL_SECONDS = 300.0
# Bound on the cache so a flood of unknown ids cannot grow it without limit.
MAX_CACHE_ENTRIES = 512

# Every free-text field is flattened to one line and capped before it can reach a prompt. The table
# is operator-controlled, but a value that lands in the model's input still gets the same
# defense-in-depth the Slack profile labels get (``slack_ingress.render.one_line``).
MAX_FIELD_CHARS = 80
# Everything a model may read as a line/record boundary: whitespace, C0 controls (\n, \r, \t, NUL,
# ESC…), DEL and the Unicode line/paragraph separators — the same set ``slack_ingress.render``
# collapses (``\s`` alone would pass an ANSI escape or a NUL straight through).
_LINE_BREAKING = re.compile(r"[\s\x00-\x1f\x7f\u2028\u2029\u0085]+")
# A GitHub login: alphanumerics and single hyphens, ≤ 39 chars (GitHub's own rule). Validated on
# lookup so a junk mention author can never become a query key.
_GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
# A Slack user id (mirrors core.context._SLACK_USER_ID_RE): U/W (+ B for bots) + uppercase alnums.
_SLACK_USER_ID_RE = re.compile(r"^[UWB][A-Z0-9]{1,31}$")
# A person_id slug: what an operator types. Lowercase alnums, hyphens, underscores, dots.
_PERSON_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# A Cognito ``sub`` (mirrors the dashboard API's ``_PORTAL_USER_RE``): a lowercase UUID.
_COGNITO_SUB_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")

# (kind, key) -> (expires_at, person-or-None)
_CACHE: dict[tuple[str, str], tuple[float, Person | None]] = {}


def _one_line(value: Any, limit: int = MAX_FIELD_CHARS) -> str:
    """Flatten any value to a single, length-capped line ("" for None)."""
    if value is None:
        return ""
    text = _LINE_BREAKING.sub(" ", str(value)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass(frozen=True)
class Person:
    """One directory row. Every identity is optional — a row needs at least one to be useful."""

    person_id: str
    name: str = ""
    role: str = ""
    github_login: str = ""
    slack_user_id: str = ""
    enabled: bool = True
    # The portal user bound to this person. Authenticates the dashboard caller; never introduced.
    cognito_sub: str = ""

    @property
    def identities(self) -> tuple[str, ...]:
        """The cross-surface handles to introduce this person by, GitHub first."""
        parts = []
        if self.github_login:
            parts.append(f"GitHub @{self.github_login}")
        if self.slack_user_id:
            parts.append(f"Slack {self.slack_user_id}")
        return tuple(parts)

    def introduction(self) -> str:
        """A one-line prompt introduction: ``Example Maintainer — maintainer (GitHub @example-maintainer, Slack U0123)``.

        Pure and total; every field has already been flattened by :func:`person_from_item`, and the
        result is a single line by construction so it can never open a new heading in the prompt.
        """
        who = self.name or self.person_id
        if self.role:
            who += f" — {self.role}"
        ids = ", ".join(self.identities)
        return f"{who} ({ids})" if ids else who


def person_from_item(item: dict[str, Any]) -> Person | None:
    """Parse a low-level DynamoDB item into a :class:`Person`; ``None`` for a row with no id.

    Tolerant of a hand-written row (missing attributes, wrong types): every string field is
    flattened + capped — but a row without a usable ``person_id`` is not a person at all, and
    ``enabled`` is **fail-closed**: absent ⇒ enabled, present ⇒ enabled only if it is exactly
    ``{"BOOL": true}``. A console/UI edit that stores ``"false"`` as a string must revoke, not grant.
    """

    def _s(name: str) -> str:
        return _one_line((item.get(name) or {}).get("S"))

    person_id = _s("person_id")
    if not person_id:
        return None
    enabled_attr = item.get("enabled")
    enabled = enabled_attr is None or enabled_attr == {"BOOL": True}
    return Person(
        person_id=person_id,
        name=_s("name"),
        role=_s("role"),
        github_login=_s("github_login").lower(),
        slack_user_id=_s("slack_user_id"),
        enabled=enabled,
        cognito_sub=_s("cognito_sub").lower(),
    )


def person_to_item(person: Person, *, now: datetime | None = None) -> dict[str, Any]:
    """The low-level DynamoDB item for ``person``, normalized for the GSIs (see module docstring).

    Empty identities are **omitted** rather than written as ``""`` — a DynamoDB GSI rejects an
    empty string key, and omitting the attribute is what makes the index sparse.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    item: dict[str, Any] = {
        "person_id": {"S": person.person_id},
        "enabled": {"BOOL": person.enabled},
        "updated_at": {"S": stamp},
    }
    for attr, value in (
        ("name", person.name),
        ("role", person.role),
        ("github_login", person.github_login.lower()),
        ("slack_user_id", person.slack_user_id),
        ("cognito_sub", person.cognito_sub.lower()),
    ):
        cleaned = _one_line(value)
        if cleaned:
            item[attr] = {"S": cleaned}
    return item


class PeopleDirectory:
    """Read side: resolve a GitHub login or Slack user id to a :class:`Person`, fail-closed.

    ``client`` is a low-level boto3 DynamoDB client (injected, so tests stay AWS-free); ``table``
    is the table name or ``None``. With either missing the directory is **disabled**: every lookup
    is ``None`` and nothing is queried — the ingress then falls back to its legacy secret list.
    """

    def __init__(self, client: Any | None, table: str | None) -> None:
        self._client = client
        self._table = table or None

    @property
    def enabled(self) -> bool:
        return bool(self._client is not None and self._table)

    def find_by_github(self, login: str | None) -> Person | None:
        """The person owning GitHub ``login`` (case-insensitive), or ``None``."""
        if not isinstance(login, str) or not _GITHUB_LOGIN_RE.match(login.strip()):
            return None
        return self._lookup("github", PEOPLE_GITHUB_GSI_NAME, "github_login", login.strip().lower())

    def find_by_slack(self, user_id: str | None) -> Person | None:
        """The person owning Slack ``user_id`` (case-sensitive), or ``None``."""
        if not isinstance(user_id, str) or not _SLACK_USER_ID_RE.match(user_id.strip()):
            return None
        return self._lookup("slack", PEOPLE_SLACK_GSI_NAME, "slack_user_id", user_id.strip())

    def find_by_cognito(self, sub: str | None) -> Person | None:
        """The person bound to the verified Cognito ``sub``, or ``None``."""
        if not isinstance(sub, str) or not _COGNITO_SUB_RE.match(sub.strip()):
            return None
        return self._lookup("cognito", PEOPLE_COGNITO_GSI_NAME, "cognito_sub", sub.strip())

    def authorizes(self, person: Person | None) -> bool:
        """True iff ``person`` is a known, **enabled** row — the directory's grant decision."""
        return person is not None and person.enabled

    def introduces(self, person: Person | None) -> Person | None:
        """``person`` if the prompt may vouch for them, else ``None``.

        A disabled row is the operator saying "this person is no longer one of us": even when a
        legacy secret list still lets them in, the agent must not be told they are a maintainer.
        """
        return person if self.authorizes(person) else None

    # ---- internals -----------------------------------------------------------------

    def _lookup(self, kind: str, index: str, attr: str, key: str) -> Person | None:
        if not self.enabled:
            return None
        cache_key = (kind, key)
        now = time.monotonic()
        cached = _CACHE.get(cache_key)
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            person = self._query(index, attr, key)
        except Exception as e:  # noqa: BLE001 — an authorizer fails CLOSED on any uncertainty
            # Denies this call only — NOT cached. A throttle/timeout/IAM-propagation blip must not
            # be remembered as a miss, or one blip locks a person out for a whole TTL window.
            logger.warning("people: lookup %s=%s failed: %s; treating as unknown", attr, key, e)
            return None
        if len(_CACHE) >= MAX_CACHE_ENTRIES:
            _CACHE.clear()
        _CACHE[cache_key] = (now + CACHE_TTL_SECONDS, person)
        return person

    def _query(self, index: str, attr: str, key: str) -> Person | None:
        """One GSI Query; ``None`` on no match or more than one match. Raises on any error
        (DynamoDB, or a response that does not parse) — :meth:`_lookup` turns that into "unknown"."""
        resp = self._client.query(  # type: ignore[union-attr]  # enabled ⇒ client is set
            TableName=self._table,
            IndexName=index,
            KeyConditionExpression="#k = :v",
            ExpressionAttributeNames={"#k": attr},
            ExpressionAttributeValues={":v": {"S": key}},
            Limit=2,
        )
        items = [p for p in map(person_from_item, resp.get("Items") or []) if p is not None]
        if not items:
            logger.info("people: %s=%s is unknown to the directory", attr, key)
            return None
        if len(items) > 1:
            logger.warning(
                "people: %s=%s is claimed by %d persons (%s); refusing the ambiguous identity",
                attr,
                key,
                len(items),
                ", ".join(sorted(p.person_id for p in items)),
            )
            return None
        person = items[0]
        logger.info(
            "people: %s=%s resolved to %s (enabled=%s)", attr, key, person.person_id, person.enabled
        )
        return person


# ---------------------------------------------------------------------------
# Direct Lambda invokes — the dashboard Lambdas (ReadApiFn, SchedulesApiFn) called with
# ``lambda:InvokeFunction`` by an IAM-trusted principal instead of through API Gateway.
# ---------------------------------------------------------------------------

# ``X-Strandly-Person: github:<login>``: the person a direct invoke names. Through API Gateway it
# never selects anyone (the person comes from the verified Cognito user there).
PERSON_HEADER = "x-strandly-person"
_PERSON_HEADER_RE = re.compile(r"^github:([A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38})$")


def is_direct_invoke(event: Any) -> bool:
    """Whether ``event`` is a direct Lambda invoke: no API Gateway context, no authorizer context.

    API Gateway always sets ``requestContext.apiId``, so an internet caller can never land here;
    only a principal IAM lets invoke the function can. An event that carries an authorizer is
    judged by its claims, whoever sent it.
    """
    if not isinstance(event, dict):
        return False
    ctx = event.get("requestContext")
    if ctx is None:
        ctx = {}
    return isinstance(ctx, dict) and "apiId" not in ctx and "authorizer" not in ctx


def claimed_github(event: Any) -> str | None:
    """The lowercased login an ``X-Strandly-Person`` header names, or ``None`` (absent/malformed)."""
    headers = event.get("headers") if isinstance(event, dict) else None
    if not isinstance(headers, dict):
        return None
    raw = next((v for k, v in headers.items() if isinstance(k, str) and k.lower() == PERSON_HEADER), None)
    if not isinstance(raw, str) or not raw.strip():
        return None
    match = _PERSON_HEADER_RE.fullmatch(raw.strip())
    if match is None:
        logger.info("ignoring a malformed %s header", PERSON_HEADER)
        return None
    return match.group(1).lower()


def direct_invoke_person(event: Any, directory: PeopleDirectory | None) -> Person | None:
    """The enabled person a direct invoke's ``X-Strandly-Person`` names (``by_github``), or ``None``.

    ``None`` for any event that is not a direct invoke, so an API Gateway request never gets here.
    An unknown, disabled or ambiguous login is no person.
    """
    if not is_direct_invoke(event) or directory is None:
        return None
    login = claimed_github(event)
    return directory.introduces(directory.find_by_github(login)) if login else None


# ---------------------------------------------------------------------------
# Write side — for the operator CLI and the (future) dashboard UI. Never called by an ingress.
# ---------------------------------------------------------------------------
def validate_person(person: Person) -> Person:
    """Normalize + validate a person for storage; raises ``ValueError`` on a bad row.

    Enforces the invariants the read side relies on: a slug-shaped ``person_id``, a well-formed
    (lowercased) GitHub login / Slack id when present, and **at least one identity** — a row with
    neither can never authorize anyone and would only be dead weight.
    """
    person_id = person.person_id.strip().lower()
    if not _PERSON_ID_RE.match(person_id):
        raise ValueError(f"person_id {person.person_id!r} must match {_PERSON_ID_RE.pattern}")
    github = person.github_login.strip().lower()
    if github and not _GITHUB_LOGIN_RE.match(github):
        raise ValueError(f"github_login {person.github_login!r} is not a valid GitHub login")
    slack = person.slack_user_id.strip()
    if slack and not _SLACK_USER_ID_RE.match(slack):
        raise ValueError(f"slack_user_id {person.slack_user_id!r} is not a Slack user id")
    if not github and not slack:
        raise ValueError("a person needs at least one identity (github_login or slack_user_id)")
    cognito = person.cognito_sub.strip().lower()
    if cognito and not _COGNITO_SUB_RE.match(cognito):
        raise ValueError(f"cognito_sub {person.cognito_sub!r} is not a Cognito user sub")
    return replace(
        person,
        person_id=person_id,
        name=_one_line(person.name),
        role=_one_line(person.role),
        github_login=github,
        slack_user_id=slack,
        cognito_sub=cognito,
    )


def cognito_claimants(client: Any, table: str, sub: str) -> list[str]:
    """Every ``person_id`` bound to Cognito ``sub`` (one uncached ``by_cognito`` Query)."""
    resp = client.query(
        TableName=table,
        IndexName=PEOPLE_COGNITO_GSI_NAME,
        KeyConditionExpression="#k = :v",
        ExpressionAttributeNames={"#k": "cognito_sub"},
        ExpressionAttributeValues={":v": {"S": sub}},
        ProjectionExpression="person_id",
    )
    return [i["person_id"]["S"] for i in resp.get("Items") or [] if "S" in i.get("person_id", {})]


def cognito_sub_of(client: Any, user_pool_id: str, username: str) -> str:
    """The ``sub`` of Cognito user ``username`` (a username or email alias), via AdminGetUser.

    Raises ``ValueError`` when the user has no well-formed ``sub``; a Cognito error propagates.
    """
    resp = client.admin_get_user(UserPoolId=user_pool_id, Username=username.strip())
    sub = next(
        (a.get("Value") for a in resp.get("UserAttributes") or [] if a.get("Name") == "sub"), None
    )
    if not isinstance(sub, str) or not _COGNITO_SUB_RE.match(sub.strip().lower()):
        raise ValueError(f"Cognito user {username!r} has no valid sub")
    return sub.strip().lower()


def upsert_person(
    client: Any, table: str, person: Person, *, now: datetime | None = None
) -> Person:
    """Validate and write ``person`` (full replace of the row). Returns the stored, normalized row.

    Raises on a bad row or a DynamoDB error — this is an operator action whose failure must be
    seen, not a best-effort side channel. A ``cognito_sub`` already bound to another person is a
    ``ValueError``: one portal user is at most one person (the read side refuses an ambiguous one).
    """
    stored = validate_person(person)
    if stored.cognito_sub:
        others = [pid for pid in cognito_claimants(client, table, stored.cognito_sub) if pid != stored.person_id]
        if others:
            raise ValueError(f"cognito_sub {stored.cognito_sub} is already bound to {others[0]!r}")
    client.put_item(TableName=table, Item=person_to_item(stored, now=now))
    reset_cache()
    logger.info("people: upserted %s (%s)", stored.person_id, ", ".join(stored.identities))
    return stored


def remove_person(client: Any, table: str, person_id: str) -> None:
    """Delete the row for ``person_id``. Raises on a DynamoDB error (operator action)."""
    client.delete_item(TableName=table, Key={"person_id": {"S": person_id.strip().lower()}})
    reset_cache()
    logger.info("people: removed %s", person_id)


def list_people(client: Any, table: str) -> list[Person]:
    """Every row, sorted by ``person_id``. A full Scan — the directory is a team, not a dataset."""
    people: list[Person] = []
    kwargs: dict[str, Any] = {"TableName": table}
    while True:
        resp = client.scan(**kwargs)
        people.extend(p for p in map(person_from_item, resp.get("Items") or []) if p is not None)
        start = resp.get("LastEvaluatedKey")
        if not start:
            break
        kwargs["ExclusiveStartKey"] = start
    return sorted(people, key=lambda p: p.person_id)


def reset_cache() -> None:
    """Drop every cached lookup (tests; after a write)."""
    _CACHE.clear()
