"""``/api/schedules*``: the Maintainer Portal's scheduled-jobs routes (``SchedulesApiFn``).

Its own function and role behind the HTTP API's Cognito authorizer, apart from the read API. Every
change goes through ``ScheduleService`` as the caller from :func:`caller_actor`: the verified Cognito
subject through API Gateway; for a direct Lambda invoke, the person its ``X-Strandly-Person``
names, else :data:`DIRECT_INVOKE_ACTOR`.

    GET    /api/schedules                jobs (+ next runs), the skills and limits the form needs
    POST   /api/schedules                create a job
    POST   /api/schedules/preview        validate a job spec: errors + the next 5 runs
    GET    /api/schedules/{id}           one job + its revisions (a deleted job: revisions only)
    PUT    /api/schedules/{id}           edit fields; ``expected_version`` required
    DELETE /api/schedules/{id}           delete the job and its schedule (revisions stay)
    POST   /api/schedules/{id}/pause     pause
    POST   /api/schedules/{id}/resume    resume (optional ``expires_at``)
    POST   /api/schedules/{id}/run       run now
    POST   /api/schedules/{id}/revert    restore revision ``rev``
    GET    /api/schedules/{id}/runs      past runs: run-ledger rows in the job's sessions

Errors are ``{"error": message, "code": invalid|forbidden|not_found|conflict|...}`` with a 4xx/5xx.
A job's Slack ``notify`` can be kept or cleared here, never set, and is never a DM
(``service.resolve_notify``).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from strandly.core.constants import RUN_LEDGER_GSI_NAME
from strandly.ops import runtime_client
from strandly.ops.people import (
    PEOPLE_TABLE_ENV,
    PeopleDirectory,
    direct_invoke_person,
    is_direct_invoke,
)
from strandly.ops.schedules import dispatch
from strandly.ops.schedules.dispatch import RunOutcome
from strandly.ops.schedules.model import (
    SESSION_DAILY,
    SESSION_MODES,
    SURFACE_DASHBOARD,
    Actor,
    Job,
    Revision,
    iso,
)
from strandly.ops.schedules.service import (
    LIMITS,
    MIN_INTERVAL_SECONDS,
    ForbiddenError,
    JobSpec,
    NotFoundError,
    ScheduleService,
    ScheduleSettings,
    ValidationError,
    builtin_skill_names,
)
from strandly.ops.schedules.store import ConflictError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ENV_RUN_LEDGER_TABLE = "STRANDLY_RUN_LEDGER_TABLE"
ENV_COGNITO_ISSUER = "COGNITO_ISSUER"
LEDGER_GSI_PK = "RUN"

PREVIEW_RUNS = 5
RUNS_DEFAULT_LIMIT = 50
RUNS_MAX_LIMIT = 200
RUNS_DEFAULT_DAYS = 30
RUNS_MAX_DAYS = 90
# Ledger rows one /runs call may read off the newest-first GSI before it stops (and says so).
RUNS_MAX_SCANNED = 5_000
_RUNS_PAGE = 500

# A run-now launch must answer inside the API's 29 s; a cold session takes longer to accept, so
# the launch is reported ``maybe-dispatched`` (sent, still starting) instead of timing out the API.
RUN_NOW_CONNECT_TIMEOUT_S = 3
RUN_NOW_READ_TIMEOUT_S = 20

_SPEC_FIELDS = frozenset(
    {
        "name",
        "expression",
        "prompt",
        "timezone",
        "skill",
        "session_mode",
        "notify",
        "starts_at",
        "expires_at",
        "enabled",
    }
)
_ACTIONS = frozenset({"pause", "resume", "run", "revert"})


class HttpError(Exception):
    def __init__(self, status: int, message: str, code: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


# --- caller identity ---------------------------------------------------------------------------


def _claims(event: Mapping[str, Any]) -> dict[str, Any]:
    ctx = event.get("requestContext") or {}
    claims = ((ctx.get("authorizer") or {}).get("jwt") or {}).get("claims")
    return dict(claims) if isinstance(claims, dict) else {}


# Direct Lambda invoke principals are trusted, as for the read API: IAM is their authorization.
# Lambda does not expose the invoking principal, so a direct invoke acts as the person its
# ``X-Strandly-Person`` header names (an enabled people-directory row, ``by_github``), else as this
# shared actor, unless it synthesizes Cognito claims, which are then trusted like the authorizer's.
DIRECT_INVOKE_ACTOR = Actor("iam:direct-invoke", SURFACE_DASHBOARD)
# The issuer of a ``person:<id>`` actor, the same one the agent's ``schedule`` tool records.
PERSON_ISSUER = "strandly:people"


def caller_actor(event: Mapping[str, Any], people_dir: PeopleDirectory | None = None) -> Actor:
    """Identity from the API Gateway access-token authorizer, scoped to our user pool.

    A direct invoke (:func:`strandly.ops.people.is_direct_invoke`) is instead the person its header
    names, or :data:`DIRECT_INVOKE_ACTOR` without one.
    """
    if is_direct_invoke(event):
        person = direct_invoke_person(event, people_dir)
        if person is None:
            return DIRECT_INVOKE_ACTOR
        return Actor(f"person:{person.person_id}", SURFACE_DASHBOARD, issuer=PERSON_ISSUER)
    claims = _claims(event)
    subject = claims.get("sub")
    issuer = claims.get("iss")
    expected_issuer = os.environ.get(ENV_COGNITO_ISSUER)
    if (
        not expected_issuer
        or issuer != expected_issuer
        or claims.get("token_use") != "access"
        or not isinstance(subject, str)
        or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", subject)
    ):
        raise HttpError(401, "no authenticated caller", "unauthenticated")
    return Actor(f"portal:{subject}", SURFACE_DASHBOARD, issuer=expected_issuer)


# --- past runs ---------------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


class RunsReader:
    """Past runs of a job: run-ledger rows whose session is one of the job's.

    The ledger has no session index, so this Queries its ``recent`` GSI newest-first within a time
    window, filtered server-side to session ids starting ``sched-<id>`` (then matched exactly, so
    job ``a`` does not pick up job ``a-b``), and stops at ``limit`` matches or after
    :data:`RUNS_MAX_SCANNED` rows.
    """

    def __init__(self, table: Any) -> None:
        self._table = table

    def for_job(self, job_id: str, *, since: str, limit: int) -> dict[str, Any]:
        from boto3.dynamodb.conditions import Attr, Key

        own = re.compile(rf"^sched-{re.escape(job_id)}(-\d{{8}}(-\d{{6}})?)?$")
        query: dict[str, Any] = {
            "IndexName": RUN_LEDGER_GSI_NAME,
            "KeyConditionExpression": Key("gsi_pk").eq(LEDGER_GSI_PK) & Key("started_at").gte(since),
            "FilterExpression": Attr("session_id").begins_with(f"sched-{job_id}"),
            "ScanIndexForward": False,
        }
        runs: list[dict[str, Any]] = []
        scanned = 0
        while True:
            resp = self._table.query(**query, Limit=min(_RUNS_PAGE, RUNS_MAX_SCANNED - scanned))
            scanned += int(resp.get("ScannedCount") or 0)
            for item in resp.get("Items") or []:
                if own.match(str(item.get("session_id", ""))):
                    runs.append(_run_json(item))
            last = resp.get("LastEvaluatedKey")
            if not last or len(runs) >= limit or scanned >= RUNS_MAX_SCANNED:
                break
            query["ExclusiveStartKey"] = last
        # ``complete``: the entire window was read and no matching results were truncated.
        return {"runs": runs[:limit], "scanned": scanned, "complete": not last and len(runs) <= limit}


def _run_json(item: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("task_id", "session_id", "status", "started_at", "ended_at", "duration_ms",
            "trigger_source", "error")
    out = {k: _plain(item[k]) for k in keys if item.get(k) not in (None, "")}
    if isinstance(out.get("error"), str):
        out["error"] = out["error"][:500]
    return out


# --- request / response shapes -----------------------------------------------------------------


def _body(event: Mapping[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded") and raw:
        try:
            raw = base64.b64decode(raw).decode()
        except (ValueError, UnicodeDecodeError) as e:
            raise HttpError(400, "body is not valid base64", "bad_request") from e
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise HttpError(400, "body is not JSON", "bad_request") from e
    if not isinstance(data, dict):
        raise HttpError(400, "body must be a JSON object", "bad_request")
    return data


def _int_param(value: Any, name: str, *, required: bool = False) -> int | None:
    if value is None or value == "":
        if required:
            raise HttpError(400, f"{name} is required", "bad_request")
        return None
    if isinstance(value, bool):
        raise HttpError(400, f"{name} must be an integer", "bad_request")
    try:
        return int(str(value))
    except ValueError as e:
        raise HttpError(400, f"{name} must be an integer", "bad_request") from e


def _clamp(raw: Any, name: str, default: int, maximum: int) -> int:
    value = _int_param(raw, name)
    return default if value is None else max(1, min(value, maximum))


def _opt_str(body: Mapping[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HttpError(400, f"{key} must be a string", "bad_request")
    return value


def spec_from_body(body: Mapping[str, Any], *, allow: frozenset[str] = _SPEC_FIELDS) -> JobSpec:
    unknown = set(body) - allow
    if unknown:
        raise HttpError(400, f"unknown fields: {', '.join(sorted(unknown))}", "bad_request")
    notify = body.get("notify")
    if notify is not None and not isinstance(notify, dict):
        raise HttpError(400, "notify must be an object", "bad_request")
    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        raise HttpError(400, "enabled must be true or false", "bad_request")
    return JobSpec(
        name=_opt_str(body, "name") or "",
        expression=_opt_str(body, "expression") or "",
        prompt=_opt_str(body, "prompt") or "",
        timezone=_opt_str(body, "timezone") or "UTC",
        skill=_opt_str(body, "skill") or None,
        session_mode=_opt_str(body, "session_mode") or SESSION_DAILY,
        notify=notify or None,
        starts_at=_opt_str(body, "starts_at") or None,
        expires_at=_opt_str(body, "expires_at") or None,
        enabled=enabled,
    )


def changes_from_body(body: Mapping[str, Any]) -> dict[str, Any]:
    """An edit's fields, type-checked (``null`` clears ``skill`` / ``notify`` / ``starts_at`` /
    ``expires_at``); the service validates the values."""
    unknown = set(body) - (_SPEC_FIELDS - {"enabled"})
    if unknown:
        raise HttpError(400, f"cannot change {', '.join(sorted(unknown))}", "bad_request")
    changes: dict[str, Any] = {}
    for key, value in body.items():
        if key == "notify":
            if value is not None and not isinstance(value, dict):
                raise HttpError(400, "notify must be an object", "bad_request")
        elif value is None:
            if key not in ("skill", "starts_at", "expires_at"):
                raise HttpError(400, f"{key} cannot be cleared", "bad_request")
        elif not isinstance(value, str):
            raise HttpError(400, f"{key} must be a string", "bad_request")
        changes[key] = value
    return changes


def job_json(job: Job, next_runs: list[datetime]) -> dict[str, Any]:
    item = job.to_item()
    item.pop("sk", None)
    item.pop("fire_window", None)
    item.update(
        {
            "skill": job.skill,
            "notify": job.notify.to_item() if job.notify else None,
            "starts_at": job.starts_at,
            "expires_at": job.expires_at,
            "last_run": job.last_run.to_item() if job.last_run else None,
            "manual_run_at": job.manual_run_at,
            "sync_error": "Schedule synchronization failed; contact an operator." if job.sync_error else None,
            "next_runs": [iso(t) for t in next_runs],
        }
    )
    return item


def revision_json(rev: Revision) -> dict[str, Any]:
    return {
        "rev": rev.rev,
        "action": rev.action,
        "actor": rev.actor.to_item(),
        "at": rev.at,
        "snapshot": rev.snapshot,
        **({"via": rev.via} if rev.via else {}),
    }


def _outcome_json(outcome: RunOutcome) -> dict[str, Any]:
    result = {k: v for k, v in vars(outcome).items() if v is not None and k != "detail"}
    if outcome.status == "maybe-dispatched":
        result["detail"] = "Acceptance is unknown. Check the session before running again."
    elif outcome.status == "error":
        result["detail"] = "The runtime could not start this run; contact an operator."
    return result


def _limits() -> dict[str, Any]:
    limits = LIMITS[SURFACE_DASHBOARD]
    return {
        "min_interval_minutes": MIN_INTERVAL_SECONDS // 60,
        "max_prompt_chars": limits.max_prompt_chars,
    }


# --- routes ------------------------------------------------------------------------------------


def _segments(event: Mapping[str, Any]) -> list[str]:
    http = (event.get("requestContext") or {}).get("http") or {}
    path = event.get("rawPath") or http.get("path") or ""
    return [s for s in str(path).split("/") if s]


def _method(event: Mapping[str, Any]) -> str:
    http = (event.get("requestContext") or {}).get("http") or {}
    return str(http.get("method") or event.get("httpMethod") or "GET").upper()


def route(
    event: Mapping[str, Any],
    *,
    service: ScheduleService | None,
    actor_for: Callable[[Mapping[str, Any]], Actor],
    runs: RunsReader | None = None,
    skills: Callable[[], Any] = builtin_skill_names,
) -> tuple[int, dict[str, Any]]:
    """Answer one ``/api/schedules*`` request; pure apart from the injected service and readers."""
    segments = _segments(event)
    if segments[:2] != ["api", "schedules"] or len(segments) > 4:
        return 404, {"error": "not found", "code": "not_found"}
    method = _method(event)
    try:
        actor_for(event)
        if service is None:
            return 503, {"error": "scheduled jobs are not configured", "code": "unconfigured"}
        return _dispatch(event, segments[2:], method, service, actor_for, runs, skills)
    except HttpError as e:
        return e.status, {"error": e.message, "code": e.code}
    except ValidationError as e:
        return 400, {"error": str(e), "code": "invalid"}
    except ForbiddenError as e:
        return 403, {"error": str(e), "code": "forbidden"}
    except NotFoundError as e:
        return 404, {"error": str(e), "code": "not_found"}
    except ConflictError:
        return 409, {
            "error": "the job changed since it was read; reload and retry",
            "code": "conflict",
        }


def _dispatch(
    event: Mapping[str, Any],
    rest: list[str],
    method: str,
    service: ScheduleService,
    actor_for: Callable[[Mapping[str, Any]], Actor],
    runs: RunsReader | None,
    skills: Callable[[], Any],
) -> tuple[int, dict[str, Any]]:
    if not rest:
        if method == "GET":
            jobs = sorted(service.list_jobs(), key=lambda j: (j.name.lower(), j.id))
            return 200, {
                "jobs": [job_json(j, service.next_runs(j, PREVIEW_RUNS)) for j in jobs],
                "skills": sorted(skills()),
                "session_modes": list(SESSION_MODES),
                "limits": _limits(),
                "now": iso(service.now()),
            }
        if method == "POST":
            spec = spec_from_body(_body(event))
            job = service.create(spec, actor_for(event))
            return 201, {"job": job_json(job, service.next_runs(job, PREVIEW_RUNS))}
        raise HttpError(405, "method not allowed", "method_not_allowed")

    if rest == ["preview"] and method == "POST":
        return 200, _preview(_body(event), service, actor_for(event))

    job_id = rest[0]
    if len(rest) == 1:
        if method == "GET":
            revisions = [revision_json(r) for r in service.revisions(job_id)]
            try:
                job = service.get(job_id)
            except NotFoundError:
                if not revisions:
                    raise
                return 200, {"job": None, "revisions": revisions}
            return 200, {
                "job": job_json(job, service.next_runs(job, PREVIEW_RUNS)),
                "revisions": revisions,
            }
        if method == "PUT":
            body = _body(event)
            expected = _int_param(body.pop("expected_version", None), "expected_version", required=True)
            job = service.update(job_id, changes_from_body(body), actor_for(event), expected_version=expected)
            return 200, {"job": job_json(job, service.next_runs(job, PREVIEW_RUNS))}
        if method == "DELETE":
            query = event.get("queryStringParameters") or {}
            expected = _int_param(query.get("expected_version"), "expected_version", required=True)
            deleted = service.delete(job_id, actor_for(event), expected_version=expected)
            return 200, {"deleted": job_id, "rev": deleted.rev}
        raise HttpError(405, "method not allowed", "method_not_allowed")

    action = rest[1]
    if action == "runs" and len(rest) == 2:
        if method != "GET":
            raise HttpError(405, "method not allowed", "method_not_allowed")
        if runs is None:
            raise HttpError(503, "the run ledger is not configured", "unconfigured")
        query = event.get("queryStringParameters") or {}
        days = _clamp(query.get("days"), "days", RUNS_DEFAULT_DAYS, RUNS_MAX_DAYS)
        limit = _clamp(query.get("limit"), "limit", RUNS_DEFAULT_LIMIT, RUNS_MAX_LIMIT)
        # The ledger's started_at is ``datetime.isoformat()`` in UTC; compare like with like.
        since = (service.now() - timedelta(days=days)).astimezone(timezone.utc).isoformat()
        return 200, {**runs.for_job(job_id, since=since, limit=limit), "days": days}
    if action not in _ACTIONS or len(rest) != 2:
        raise HttpError(404, "not found", "not_found")
    if method != "POST":
        raise HttpError(405, "method not allowed", "method_not_allowed")
    body = _body(event)
    actor = actor_for(event)
    allowed = {"expected_version"}
    if action == "resume":
        allowed.add("expires_at")
    elif action == "revert":
        allowed.add("rev")
    if set(body) - allowed:
        raise HttpError(400, "unknown action fields", "bad_request")
    expected = _int_param(body.get("expected_version"), "expected_version", required=action != "run")
    if action == "pause":
        job = service.pause(job_id, actor, expected_version=expected)
    elif action == "resume":
        job = service.resume(
            job_id, actor, expires_at=_opt_str(body, "expires_at"), expected_version=expected
        )
    elif action == "revert":
        rev = _int_param(body.get("rev"), "rev", required=True) or 0
        job = service.revert(job_id, rev, actor, expected_version=expected)
    else:
        if not dispatch.schedules_enabled():
            raise HttpError(503, "scheduled jobs are globally disabled", "disabled")
        outcome = service.run_now(job_id, actor)
        return 200, {"run": _outcome_json(outcome)}
    return 200, {"job": job_json(job, service.next_runs(job, PREVIEW_RUNS))}


def _preview(body: dict[str, Any], service: ScheduleService, actor: Actor) -> dict[str, Any]:
    """Validate a spec as create would; for an edit (``id`` of an existing job) the next runs keep
    that job's phase and window instead of starting from now."""
    job_id = body.pop("id", None)
    if job_id is not None and not isinstance(job_id, str):
        raise HttpError(400, "id must be a string", "bad_request")
    spec = spec_from_body(body)
    if job_id:
        changes = changes_from_body({key: value for key, value in body.items() if key != "enabled"})
        result = service.preview_update(job_id, changes, actor, count=PREVIEW_RUNS)
    else:
        result = service.preview(spec, actor, count=PREVIEW_RUNS)
    kind = None
    if result.ok:
        from strandly.ops.schedules import expressions

        kind = expressions.parse(spec.expression).kind
    return {"ok": result.ok, "errors": result.errors, "kind": kind,
            "next_runs": [iso(t) for t in result.next_runs]}



# --- Lambda wiring -----------------------------------------------------------------------------


def launch_within_budget(job: Job, config: Any, *, now: datetime, trigger_source: str) -> RunOutcome:
    """``dispatch.launch`` with socket timeouts that fit the API's 29 s (see the constants)."""
    saved = (runtime_client.INVOKE_CONNECT_TIMEOUT_S, runtime_client.INVOKE_READ_TIMEOUT_S)
    runtime_client.INVOKE_CONNECT_TIMEOUT_S = RUN_NOW_CONNECT_TIMEOUT_S
    runtime_client.INVOKE_READ_TIMEOUT_S = RUN_NOW_READ_TIMEOUT_S
    try:
        return dispatch.launch(job, config, now=now, trigger_source=trigger_source)
    finally:
        runtime_client.INVOKE_CONNECT_TIMEOUT_S, runtime_client.INVOKE_READ_TIMEOUT_S = saved


_wired: dict[str, Any] = {}


def _wiring() -> dict[str, Any]:
    if not _wired:
        import boto3

        settings = ScheduleSettings.from_env()
        _wired["service"] = (
            ScheduleService.from_settings(settings, launcher=launch_within_budget)
            if settings
            else None
        )
        ledger = os.environ.get(ENV_RUN_LEDGER_TABLE)
        _wired["runs"] = RunsReader(boto3.resource("dynamodb").Table(ledger)) if ledger else None
        people_table = os.environ.get(PEOPLE_TABLE_ENV)
        _wired["people"] = (
            PeopleDirectory(boto3.client("dynamodb"), people_table) if people_table else None
        )
    return _wired


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _plain(value)
    if isinstance(value, datetime):
        return iso(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    try:
        wired = _wiring()
        status, body = route(
            event,
            service=wired["service"],
            actor_for=lambda e: caller_actor(e, wired.get("people")),
            runs=wired["runs"],
        )
    except Exception:  # noqa: BLE001 — an unexpected failure is a 500, logged with its trace
        logger.exception("schedules-api: %s %s failed", _method(event), "/".join(_segments(event)))
        status, body = 500, {"error": "internal error", "code": "internal"}
    if status >= 400 and status != 404:
        logger.info("schedules-api: %s %s -> %s %s", _method(event),
                    "/".join(_segments(event)), status, body.get("code"))
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, default=_json_default),
    }

