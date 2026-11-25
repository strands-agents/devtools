"""GitHub Actions ingress: launch and poll an agent run from a workflow, authenticated by OIDC.

Routes (behind the dashboard HTTP API, with a JWT authorizer for the GitHub Actions issuer):

    POST /api/actions/invoke   ``{"prompt": str, "session"?: str}`` launches a detached run and
                               returns ``{status, taskId, sessionId}``
    GET  /api/actions/invoke   poll it by ``session_id`` + ``task_id``

API Gateway verifies the token's signature, expiry, issuer and audience. This module re-checks the
issuer and audience and then decides, from the verified claims alone, whether the workflow may call
the agent (:func:`verify_claims`). The repository always comes from the token, and
:func:`owns_session` checks a requested session structurally against it, so a caller can only name
sessions inside its own repository; a poll reads only a task the runtime issued for that session.
``route`` takes an injected invoker so it tests without boto3; ``lambda_handler`` builds the real one.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from handler import (
    POLL_ISSUED_KEY,
    RUNTIME_STARTING,
    RuntimeInvoker,
    RuntimeStarting,
    _DecimalEncoder,
    _parse_body,
)

from strandly.core.session_ids import (
    KIND_DISCUSSION,
    KIND_ISSUE,
    KIND_PR,
    canonical_session_id,
    github_session_belongs_to,
)

logger = logging.getLogger(__name__)

ACTIONS_ISSUER = "https://token.actions.githubusercontent.com"
DEFAULT_AUDIENCE = "strandly"
DEFAULT_REPOSITORIES: tuple[str, ...] = ()
DEFAULT_BRANCH = "main"

# Deployment config, set by infra/stacks/dashboard_stack.py from the ``actions_*`` CDK context.
ENV_AUDIENCE = "STRANDLY_ACTIONS_AUDIENCE"
ENV_REPOSITORIES = "STRANDLY_ACTIONS_REPOSITORIES"
ENV_WORKFLOWS = "STRANDLY_ACTIONS_WORKFLOWS"
ENV_DEFAULT_BRANCH = "STRANDLY_ACTIONS_DEFAULT_BRANCH"

TRIGGER_PREFIX = "github-actions:"
# Payload key carrying the verified caller to the runtime (``strandly.core.context``
# GITHUB_ACTIONS_KEY; pinned by tests/test_infra_constants_sync).
GITHUB_ACTIONS_KEY = "githubActions"
GITHUB_URL_KEY = "githubUrl"

# A pull_request_target run executes default-branch code for a pull request, possibly from a fork,
# and the token carries no claim that tells a fork apart. pull_request runs on the merge ref, which
# the default-branch rule rejects anyway; both are denied by name so the reason is explicit.
DENIED_EVENTS = frozenset({"pull_request", "pull_request_target"})

MAX_PROMPT_CHARS = 32_000
_LAUNCH_FIELDS = frozenset({"prompt", "session"})

_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_DIGITS_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_ACTOR_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_ITEM_RE = re.compile(r"^(issue|pr|discussion):([1-9][0-9]{0,9})$")
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
_KINDS = {"issue": KIND_ISSUE, "pr": KIND_PR, "discussion": KIND_DISCUSSION}
_ITEM_PATHS = {KIND_ISSUE: "issues", KIND_PR: "pull", KIND_DISCUSSION: "discussions"}


class Denied(Exception):
    """The verified token is not allowed to call the agent; the message is safe to return."""


@dataclass(frozen=True)
class Policy:
    """Which workflows may call the agent (deployment config)."""

    audience: str = DEFAULT_AUDIENCE
    repositories: tuple[str, ...] = DEFAULT_REPOSITORIES
    workflows: tuple[str, ...] = ()
    default_branch: str = DEFAULT_BRANCH

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Policy:
        """Read the policy; an unset repository list denies every caller (fail closed)."""
        values = os.environ if env is None else env

        def items(key: str) -> tuple[str, ...]:
            return tuple(v.strip() for v in (values.get(key) or "").split(",") if v.strip())

        return cls(
            audience=(values.get(ENV_AUDIENCE) or "").strip(),
            repositories=items(ENV_REPOSITORIES),
            workflows=items(ENV_WORKFLOWS),
            default_branch=(values.get(ENV_DEFAULT_BRANCH) or "").strip(),
        )


@dataclass(frozen=True)
class Caller:
    """The verified workflow run behind a request."""

    repository: str
    repository_id: str
    workflow: str  # .github/workflows/<file>, from workflow_ref
    run_id: str
    run_attempt: str
    actor: str
    actor_id: str
    event_name: str
    sha: str

    @property
    def trigger_source(self) -> str:
        return f"{TRIGGER_PREFIX}{self.repository}/{self.workflow.rsplit('/', 1)[-1]}"

    @property
    def run_url(self) -> str:
        return f"https://github.com/{self.repository}/actions/runs/{self.run_id}"

    def payload(self) -> dict[str, str]:
        return {
            "issuer": ACTIONS_ISSUER,
            "repository": self.repository,
            "repositoryId": self.repository_id,
            "workflow": self.workflow,
            "runId": self.run_id,
            "runAttempt": self.run_attempt,
            "actor": self.actor,
            "actorId": self.actor_id,
            "eventName": self.event_name,
            "sha": self.sha,
        }


def _claim(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    return value.strip() if isinstance(value, str) else ""


def _audiences(raw: Any) -> list[str]:
    # API Gateway hands claims over as strings; a multi-valued aud arrives as "[a b]".
    if isinstance(raw, list):
        return [str(a) for a in raw]
    text = str(raw or "").strip()
    if text.startswith("[") and text.endswith("]"):
        return text[1:-1].replace(",", " ").split()
    return [text] if text else []


def verify_claims(claims: dict[str, Any], policy: Policy) -> Caller:
    """The caller a verified GitHub Actions token describes, if the policy admits it.

    Fails closed: every rule must pass, and a missing or malformed claim denies.

    Raises:
        Denied: a rule rejects the token; the message names the rule.
    """
    if _claim(claims, "iss") != ACTIONS_ISSUER:
        raise Denied("token issuer is not GitHub Actions")
    if not policy.audience or policy.audience not in _audiences(claims.get("aud")):
        raise Denied("token audience is not this deployment's")
    repository = _claim(claims, "repository")
    owner = _claim(claims, "repository_owner")
    if not _REPOSITORY_RE.fullmatch(repository) or repository.split("/")[0].lower() != owner.lower():
        raise Denied("token has no valid repository claim")
    for name in ("repository_id", "run_id", "run_attempt", "actor_id"):
        if not _DIGITS_RE.fullmatch(_claim(claims, name)):
            raise Denied(f"token has no valid {name} claim")
    if not any(fnmatchcase(repository.lower(), rule.lower()) for rule in policy.repositories):
        raise Denied(f"repository {repository} is not allowed")
    event_name = _claim(claims, "event_name")
    if not event_name or event_name in DENIED_EVENTS or _claim(claims, "head_ref"):
        raise Denied(f"event {event_name or '(none)'} is not allowed (pull request triggers are denied)")
    branch = policy.default_branch
    expected_ref = f"refs/heads/{branch}"
    if not _BRANCH_RE.fullmatch(branch) or _claim(claims, "ref") != expected_ref:
        raise Denied(f"only workflows running on {expected_ref} are allowed")
    workflow_ref = _claim(claims, "workflow_ref")
    path, _, ref = workflow_ref.partition("@")
    prefix = f"{repository}/"
    if ref != expected_ref or not path.lower().startswith(prefix.lower()):
        raise Denied(f"the workflow must come from {repository}@{expected_ref}")
    workflow = path[len(prefix):]
    if not workflow.startswith(".github/workflows/") or ".." in workflow:
        raise Denied("token has no valid workflow_ref claim")
    if policy.workflows and not any(
        fnmatchcase(f"{repository}/{workflow}".lower(), rule.lower()) for rule in policy.workflows
    ):
        raise Denied(f"workflow {workflow} is not allowed")
    actor = _claim(claims, "actor")
    if not _ACTOR_RE.fullmatch(actor):
        raise Denied("token has no valid actor claim")
    return Caller(
        repository=repository,
        repository_id=_claim(claims, "repository_id"),
        workflow=workflow,
        run_id=_claim(claims, "run_id"),
        run_attempt=_claim(claims, "run_attempt"),
        actor=actor,
        actor_id=_claim(claims, "actor_id"),
        event_name=event_name,
        sha=_claim(claims, "sha"),
    )


def resolve_session(caller: Caller, raw: Any) -> tuple[str, str | None]:
    """``(session_id, item_url)`` for a launch, always inside the caller's repository.

    ``issue:<n>``, ``pr:<n>`` or ``discussion:<n>`` joins that item's thread
    (``gh-<owner>-<repo>-<h8>-<kind>-<n>``, shared with the other ingresses); another key gives the
    repository's own ``gha-<repository_id>-<key>`` session; empty gives one session per run.

    Raises:
        ValueError: ``raw`` is neither.
    """
    text = raw.strip() if isinstance(raw, str) else ""
    if raw is not None and not isinstance(raw, str):
        raise ValueError("session must be a string")
    if not text:
        return f"gha-{caller.repository_id}-run-{caller.run_id}", None
    if m := _ITEM_RE.fullmatch(text):
        kind = _KINDS[m.group(1)]
        number = m.group(2)
        url = f"https://github.com/{caller.repository}/{_ITEM_PATHS[kind]}/{number}"
        return canonical_session_id(caller.repository, kind, number), url
    if _KEY_RE.fullmatch(text):
        return f"gha-{caller.repository_id}-{text}", None
    raise ValueError("session must be issue:<n>, pr:<n>, discussion:<n> or a key of [A-Za-z0-9_-]")


def owns_session(caller: Caller, session_id: str) -> bool:
    """Whether ``session_id`` is one :func:`resolve_session` could give this caller.

    Structural, not a pattern: an item id must parse, carry the digest of the caller's own
    repository and rebuild to exactly ``session_id`` (see ``github_session_belongs_to``).
    """
    if session_id.startswith(f"gha-{caller.repository_id}-"):
        return bool(_KEY_RE.fullmatch(session_id[len(f"gha-{caller.repository_id}-"):]))
    return github_session_belongs_to(session_id, caller.repository)


def _claims(event: dict[str, Any]) -> dict[str, Any]:
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    claims = (authorizer.get("jwt") or {}).get("claims")
    return claims if isinstance(claims, dict) else {}


def _launch(event: dict[str, Any], caller: Caller, invoker: RuntimeInvoker) -> tuple[int, dict[str, Any]]:
    body = _parse_body(event)
    unknown = sorted(set(body) - _LAUNCH_FIELDS)
    if unknown:
        return 400, {"error": f"unsupported fields: {', '.join(unknown)} (send prompt and session only)"}
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return 400, {"error": "prompt required"}
    if len(prompt) > MAX_PROMPT_CHARS:
        return 400, {"error": f"prompt exceeds {MAX_PROMPT_CHARS} characters"}
    try:
        session_id, item_url = resolve_session(caller, body.get("session"))
    except ValueError as exc:
        return 400, {"error": str(exc)}
    context: dict[str, Any] = {"githubActions": {**caller.payload(), "runUrl": caller.run_url}}
    payload: dict[str, Any] = {
        "prompt": prompt,
        "sessionId": session_id,
        "triggerSource": caller.trigger_source,
        # One trigger per run attempt: retrying a launch whose answer was lost is a no-op.
        "triggerId": f"gha-{caller.repository_id}-{caller.run_id}-{caller.run_attempt}",
        GITHUB_ACTIONS_KEY: caller.payload(),
    }
    if item_url:
        payload[GITHUB_URL_KEY] = item_url
        context["item"] = item_url
        # The ledger's repository and target, in the shape serve.agentcore_app._github_target reads.
        payload["githubContext"] = {"repository": caller.repository, "event": {"issue": {"html_url": item_url}}}
    payload["invocationContext"] = context
    logger.info(
        "actions launch: repo=%s workflow=%s run=%s/%s actor=%s session=%s",
        caller.repository, caller.workflow, caller.run_id, caller.run_attempt, caller.actor, session_id,
    )
    result = invoker.invoke(payload)
    logger.info("actions launch done: session=%s status=%s task=%s",
                session_id, result.get("status"), result.get("taskId"))
    return 200, {**result, "sessionId": session_id}


def _poll(event: dict[str, Any], caller: Caller, invoker: RuntimeInvoker) -> tuple[int, dict[str, Any]]:
    query = event.get("queryStringParameters") or {}
    session_id = str(query.get("session_id") or "").strip()
    task_id = str(query.get("task_id") or "").strip()
    if not session_id or not task_id:
        return 400, {"error": "poll requires 'session_id' and 'task_id'"}
    if not owns_session(caller, session_id):
        return 403, {"error": f"session {session_id} does not belong to {caller.repository}"}
    # issued_only: the runtime answers only for a task it started on this session, so a made-up
    # task id cannot read the session's stored answer through the snapshot fallback.
    result = invoker.poll(session_id, task_id, issued_only=True)
    if result.get(POLL_ISSUED_KEY) is False:
        return 404, {"error": f"task {task_id} was not issued for session {session_id}"}
    return 200, result


def route(
    event: dict[str, Any], invoker: RuntimeInvoker | None, policy: Policy
) -> tuple[int, dict[str, Any]]:
    """Pure router: (status_code, body) for an HTTP API v2 event."""
    http = (event.get("requestContext") or {}).get("http") or {}
    method = (http.get("method") or "GET").upper()
    path = (event.get("rawPath") or http.get("path") or "/").rstrip("/") or "/"
    if path != "/api/actions/invoke":
        return 404, {"error": "not found", "path": path}
    if method not in ("GET", "POST"):
        return 405, {"error": "method not allowed"}
    try:
        caller = verify_claims(_claims(event), policy)
    except Denied as exc:
        logger.warning("actions %s denied: %s (repo=%s workflow_ref=%s event=%s ref=%s)", method, exc,
                       *(_claim(_claims(event), k) for k in ("repository", "workflow_ref", "event_name", "ref")))
        return 403, {"error": str(exc)}
    if invoker is None:
        return 503, {"error": "actions ingress not configured (STRANDLY_RUNTIME_ARN unset)"}
    try:
        return _launch(event, caller, invoker) if method == "POST" else _poll(event, caller, invoker)
    except RuntimeStarting:
        return 504, dict(RUNTIME_STARTING)


def _build_invoker() -> RuntimeInvoker | None:
    runtime_arn = os.environ.get("STRANDLY_RUNTIME_ARN")
    return RuntimeInvoker(runtime_arn, os.environ.get("AWS_REGION")) if runtime_arn else None


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """AWS Lambda entrypoint: route, then serialize to an HTTP API v2 proxy response."""
    status, body = route(event, _build_invoker(), Policy.from_env())
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body, cls=_DecimalEncoder),
    }
