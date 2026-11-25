"""Call a Strandly deployment from a GitHub Actions job with the job's OIDC token.

Standard library only: the composite action runs it with the runner's ``python3``. Reads its inputs
from ``STRANDLY_*`` environment variables (see ``action.yml``), launches a run on
``<url>/api/actions/invoke``, optionally polls it to completion, and writes ``status``,
``session_id``, ``task_id`` and ``result`` to ``$GITHUB_OUTPUT``.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

ROUTE = "/api/actions/invoke"
TERMINAL = frozenset({"completed", "failed", "cancelled", "rejected"})
# A launch on a cold session answers "starting" before the run has a task id; repeating the same
# launch (same run attempt, same prompt) is a no-op that returns the running task.
LAUNCH_BUDGET_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 40
RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504})


class ActionError(Exception):
    """A failure to report as the step's error."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _interval() -> float:
    return float(_env("STRANDLY_POLL_INTERVAL", "15") or "15")


def oidc_token(audience: str) -> str:
    """Request this job's OIDC token for ``audience`` from the Actions runtime."""
    url, bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"), os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not bearer:
        raise ActionError("no OIDC token available: give the job `permissions: id-token: write`")
    sep = "&" if "?" in url else "?"
    request = urllib.request.Request(
        f"{url}{sep}audience={urllib.parse.quote(audience)}", headers={"Authorization": f"bearer {bearer}"}
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        token = json.loads(response.read().decode()).get("value")
    if not isinstance(token, str) or not token:
        raise ActionError("the Actions runtime returned no OIDC token")
    print(f"::add-mask::{token}")
    return token


def call(method: str, url: str, audience: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """One API call with a fresh token; ``(http status, json body)``."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {oidc_token(audience)}",
        "Content-Type": "application/json",
        "User-Agent": "strandly-action",
    })
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        parsed = json.loads(raw.decode() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = {"error": raw.decode(errors="replace")[:500]}
    return status, parsed if isinstance(parsed, dict) else {"error": str(parsed)[:500]}


def session_from_event() -> str:
    """``issue:<n>``, ``pr:<n>`` or ``discussion:<n>`` for the item this job's event is about."""
    path = _env("GITHUB_EVENT_PATH")
    event_name = _env("GITHUB_EVENT_NAME")
    if not path or event_name not in {"issues", "issue_comment", "discussion", "discussion_comment"}:
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            event = json.load(handle)
    except (OSError, ValueError):
        return ""
    if event_name.startswith("discussion"):
        number = (event.get("discussion") or {}).get("number")
        return f"discussion:{number}" if isinstance(number, int) else ""
    issue = event.get("issue") or {}
    number = issue.get("number")
    if not isinstance(number, int):
        return ""
    return f"{'pr' if issue.get('pull_request') else 'issue'}:{number}"


def launch(api: str, audience: str, prompt: str, session: str, deadline: float) -> dict[str, Any]:
    """Start the run; retries a cold start or a gateway error until the run has a task id."""
    while True:
        status, body = call("POST", api, audience, {"prompt": prompt, "session": session})
        if status == 200 and body.get("taskId"):
            return body
        retryable = (status == 200 and body.get("status") == "starting") or status in RETRYABLE_HTTP
        if not retryable:
            raise ActionError(f"launch failed (HTTP {status}): {body.get('error') or body.get('status') or body}")
        if time.monotonic() >= deadline:
            raise ActionError(f"launch did not start within the budget (last: HTTP {status} {body})")
        print(f"launch pending (HTTP {status}, {body.get('status') or body.get('error')}); retrying")
        time.sleep(_interval())


def wait(api: str, audience: str, session_id: str, task_id: str, deadline: float) -> dict[str, Any]:
    """Poll until the run ends or ``deadline``; returns the last poll body."""
    query = urllib.parse.urlencode({"session_id": session_id, "task_id": task_id})
    last: dict[str, Any] = {"status": "running"}
    while time.monotonic() < deadline:
        status, body = call("GET", f"{api}?{query}", audience)
        if status == 200:
            last = body
            print(f"poll: {body.get('status')}")
            if body.get("status") in TERMINAL:
                return body
        elif status not in RETRYABLE_HTTP:
            raise ActionError(f"poll failed (HTTP {status}): {body.get('error') or body}")
        time.sleep(_interval())
    return {**last, "timedOut": True}


def write_outputs(values: dict[str, str]) -> None:
    path = _env("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            delimiter = f"STRANDLY_{secrets.token_hex(8)}"
            handle.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def write_summary(lines: list[str]) -> None:
    path = _env("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def main() -> int:
    base = _env("STRANDLY_URL").rstrip("/")
    prompt = os.environ.get("STRANDLY_PROMPT", "")
    audience = _env("STRANDLY_AUDIENCE", "strandly") or "strandly"
    should_wait = _env("STRANDLY_WAIT", "false").lower() in {"true", "1", "yes"}
    timeout = float(_env("STRANDLY_TIMEOUT", "1800") or "1800")
    try:
        if not base.startswith("https://") and not _env("STRANDLY_ALLOW_HTTP"):
            raise ActionError("url must be the deployment's https:// API endpoint")
        if not prompt.strip():
            raise ActionError("prompt is required")
        session = _env("STRANDLY_SESSION") or session_from_event()
        api = base + ROUTE
        started = time.monotonic()
        body = launch(api, audience, prompt, session, started + min(timeout, LAUNCH_BUDGET_SECONDS))
        session_id, task_id = str(body.get("sessionId") or ""), str(body["taskId"])
        print(f"launched: session={session_id} task={task_id}")
        result = ""
        status = str(body.get("status") or "accepted")
        if should_wait:
            final = wait(api, audience, session_id, task_id, started + timeout)
            status = str(final.get("status") or "unknown")
            result = str(final.get("result") or final.get("error") or "")
            if final.get("timedOut"):
                print(f"::warning::run did not finish within {timeout:.0f}s (last status: {status})")
    except ActionError as exc:
        print(f"::error::{exc}")
        write_outputs({"status": "error", "session_id": "", "task_id": "", "result": str(exc)})
        return 1
    write_outputs({"status": status, "session_id": session_id, "task_id": task_id, "result": result})
    write_summary([
        "## Strandly", "", f"- Session: `{session_id}`", f"- Task: `{task_id}`", f"- Status: `{status}`",
        *(["", "<details><summary>Result</summary>", "", result, "", "</details>"] if result else []),
    ])
    if status in {"failed", "cancelled", "rejected"}:
        print(f"::error::run {status}: {result[:500]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
