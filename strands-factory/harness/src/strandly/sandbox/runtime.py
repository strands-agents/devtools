"""A sandbox backed by a second AgentCore Runtime, driven by ``InvokeAgentRuntimeCommand``.

The agent runtime holds the model, secrets, memory, ledger and ``use_github``; the sandbox runtime
is a shell and a filesystem whose execution role grants nothing beyond pulling its image and writing
logs (``infra/stacks/sandbox_stack.py``), so nothing the model runs can speak for the agent. It runs
on Runtime instances: each session has its own EC2 instance and an EBS workspace at
:data:`~strandly.core.constants.SANDBOX_MOUNT_PATH` that survives stop and resume, and the session
id derives from the harness session (:func:`sandbox_session_id`). Commands on one session are
concurrent-safe, so subagents share the sandbox without a lock. The session wakes lazily on first
use. See docs/operations.md.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextlib
import contextvars
import functools
import hashlib
import json
import logging
import os
import random
import shlex
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from strands.sandbox.errors import SandboxTimeoutError
from strands.sandbox.posix_shell import PosixShellSandbox, build_shell_env_prefix
from strands.sandbox.types import ExecutionResult, StreamChunk

from strandly.core.constants import (
    SANDBOX_CACHE_ROOT,
    SANDBOX_CIRCUIT_OPEN_SECONDS,
    SANDBOX_CLIENT_CONNECT_TIMEOUT_SECONDS,
    SANDBOX_CLIENT_KEEPALIVE_IDLE_SECONDS,
    SANDBOX_CLIENT_KEEPALIVE_INTERVAL_SECONDS,
    SANDBOX_CLIENT_KEEPALIVE_PROBES,
    SANDBOX_CLIENT_MAX_IDLE_SECONDS,
    SANDBOX_CLIENT_READ_TIMEOUT_SECONDS,
    SANDBOX_COMMAND_MAX_BYTES,
    SANDBOX_COMMAND_TIMEOUT_DEFAULT,
    SANDBOX_COMMAND_TIMEOUT_MAX,
    SANDBOX_COMMAND_TIMEOUT_MIN,
    SANDBOX_GIT_COMMIT_EMAIL,
    SANDBOX_GIT_COMMIT_NAME,
    SANDBOX_GIT_CREDENTIAL_KEY,
    SANDBOX_GIT_CREDENTIAL_MISSING_MARKER,
    SANDBOX_GIT_CREDENTIAL_STATUS_KEY,
    SANDBOX_GIT_CREDENTIAL_WRITTEN,
    SANDBOX_GIT_PAGER_ENV,
    SANDBOX_MOUNT_PATH,
    SANDBOX_PLATFORM_PROBE_INTERVAL_SECONDS,
    SANDBOX_READ_CHUNK_BYTES,
    SANDBOX_READ_CONCURRENCY,
    SANDBOX_READ_MAX_ATTEMPTS,
    SANDBOX_READ_MAX_BYTES,
    SANDBOX_RESTART_COOLDOWN_SECONDS,
    SANDBOX_RETRY_BACKOFF_BASE_SECONDS,
    SANDBOX_RETRY_BACKOFF_MAX_SECONDS,
    SANDBOX_RETRY_MAX_ATTEMPTS,
    SANDBOX_SESSION_PREFIX,
    SANDBOX_STORAGE_ERROR_MARKERS,
    SANDBOX_STORAGE_ERROR_THRESHOLD,
    SANDBOX_TRANSPORT_GRACE_SECONDS,
    SANDBOX_WAKE_BUDGET_SECONDS,
    SANDBOX_WAKE_CONNECT_TIMEOUT_SECONDS,
    SANDBOX_WAKE_MAX_ATTEMPTS,
    SANDBOX_WAKE_READ_TIMEOUT_SECONDS,
    SANDBOX_WAKE_RETRY_DELAY_SECONDS,
    SANDBOX_WORKDIR,
    SANDBOX_WRITE_CHUNK_CHARS,
)
from strandly.sandbox.scripts import (
    HEALTH_USED_MARKER,
    WorkspaceHealth,
    cache_env_prefix,
    git_bootstrap_script,
    parse_workspace_health,
    strip_heartbeat,
    with_heartbeat,
    workspace_health_script,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator

    from strands.types.tools import AgentTool

    from strandly.sandbox.ttl import SessionTtl

logger = logging.getLogger(__name__)

#: Set while :meth:`RuntimeSandbox._run_first_use_hooks_sync` runs a first-use hook. A hook's own
#: commands go through :meth:`RuntimeSandbox._ensure_ready` like any other, and must not wait for
#: the hook phase they are part of (that would deadlock on ``_first_use_lock``), and run without the
#: turn's cancel signal (hooks are short, idempotent setup that later turns rely on). A context
#: variable, not a thread-local: the hook's commands run on worker threads (``asyncio.to_thread``),
#: which copy the caller's context.
_IN_FIRST_USE_HOOK: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "strandly_sandbox_in_first_use_hook", default=False
)

#: The cancel signal for commands issued from the current context; it takes precedence over the
#: sandbox's bound one (:meth:`RuntimeSandbox.bind_cancel_signal`). A subagent child shares its
#: parent's sandbox but can outlive the parent's turn, which unbinds the bound signal when it ends,
#: so the child's commands carry the run's signal here instead (see :func:`command_cancel_signal`).
_COMMAND_CANCEL_SIGNAL: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "strandly_sandbox_command_cancel_signal", default=None
)

#: The tool calls the current context's commands belong to, outermost first: one id per agent depth
#: (the main agent's call, then each subagent's). Set by :func:`tag_command_caller` from a
#: ``BeforeToolCallEvent`` hook; read only to attribute a platform failure in its log line.
_COMMAND_CALLER: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "strandly_sandbox_command_caller", default=()
)


def tag_command_caller(tool_use_id: object, depth: int) -> None:
    """Record that commands from the current context belong to tool call ``tool_use_id``.

    A subagent's context inherits its spawning call's tag, so the chain names which subagent made
    the call even though subagent transcripts are not persisted.
    """
    chain = _COMMAND_CALLER.get()[: max(depth, 0)]
    _COMMAND_CALLER.set((*chain, str(tool_use_id or "?")))


def _caller_detail() -> str:
    chain = _COMMAND_CALLER.get()
    if not chain:
        return "caller=unknown"
    agent = "main" if len(chain) == 1 else f"subagent depth={len(chain) - 1}"
    return f"caller={'>'.join(chain)} ({agent})"


def _send_failure_detail(
    trace: _SendTrace | None, error: BaseException | None, *, response_started: bool
) -> str:
    """Who sent the failed command, and what its send did (no command text, output or SDK text)."""
    parts = [_caller_detail()]
    if trace is not None:
        parts.append(f"elapsed={(time.monotonic() - trace.started) * 1000:.0f}ms")
    parts.append(f"error={type(error).__name__ if error is not None else 'none'}")
    # Never "yes" here: a failure that proves the command unsent is retried or raised as such.
    parts.append("pre_send=no")
    parts.append(f"response={'started' if response_started else 'none'}")
    if trace is not None:
        parts.append(f"connection={trace.connection}")
    request_id = (trace.request_id if trace is not None else None) or _request_id(error)
    parts.append(f"request_id={request_id or 'none'}")
    return " ".join(parts)


@contextlib.contextmanager
def command_cancel_signal(signal: threading.Event | None) -> Iterator[None]:
    """Sandbox commands issued in this context (and tasks it spawns) stop waiting once ``signal`` is set."""
    token = _COMMAND_CANCEL_SIGNAL.set(signal)
    try:
        yield
    finally:
        _COMMAND_CANCEL_SIGNAL.reset(token)

#: Shell the command API is asked to run. Every command is wrapped in ``/bin/bash -c '<...>'`` rather
#: than sent bare: the service documents "each command spawns a new bash process", but wrapping is
#: what makes pipelines, heredocs and ``&&`` chaining *guaranteed* rather than inferred — and the
#: SDK's inherited file/code primitives all build shell strings (``base64 -d << 'EOF' | python3``).
SANDBOX_SHELL = "/bin/bash"

#: Service exception members that can arrive **on the result stream** (not as a raised client error).
#: A failure delivered as data must never be mistaken for a success with no output, so any
#: *present* key raises (membership, not truthiness).
_STREAM_EXCEPTION_KEYS = (
    "accessDeniedException",
    "internalServerException",
    "resourceNotFoundException",
    "serviceQuotaExceededException",
    "throttlingException",
    "validationException",
    "runtimeClientError",
)

#: Botocore error codes the service documents as transient for this API:
#: ``RetryableConflictException`` (409, the session is being provisioned or torn down — "Session
#: operation in progress, please retry") and ``ThrottlingException`` (the 25 TPS limit).
_RETRYABLE_ERROR_CODES = frozenset({"RetryableConflictException", "ThrottlingException"})

#: Error code meaning "that session isn't there" — the session idled out between invocations, or has
#: not been provisioned yet. Recoverable exactly once, by waking the session (see :meth:`_wake`).
_SESSION_GONE_ERROR_CODES = frozenset({"ResourceNotFoundException"})

#: Payload of the wake invoke. The sandbox runtime's entrypoint (``sandbox_main.py``) answers it with
#: a status dict; its real purpose is to make AgentCore start the session's instance.
WAKE_PAYLOAD = {"action": "ping"}


class SandboxCommandTimeoutError(SandboxTimeoutError):
    """A sandbox timeout whose message names the session, the budget and what to do next.

    The SDK ``shell`` tool re-raises ``SandboxTimeoutError`` untouched but collapses every other
    exception into ``RuntimeError(str(e))``, and a bare ``TimeoutError`` has an empty ``str()``.
    ``SandboxTimeoutError.__init__`` hardcodes its message, so ``args`` is replaced after it runs.
    The base initializer must run: it sets ``stdout``/``stderr``, which the ``shell`` tool reads on
    timeout. They carry whatever streamed before the cut.
    """

    def __init__(
        self, *, timeout: float, context: str, stdout: str = "", stderr: str = ""
    ) -> None:
        """Initialize with the elapsed budget, identifying sandbox context and partial output.

        Args:
            timeout: The wall-clock budget, in seconds, that elapsed.
            context: Identifying sandbox context (see :meth:`RuntimeSandbox._error_context`).
            stdout: Standard output received before the stream was abandoned.
            stderr: Standard error received before the stream was abandoned.
        """
        super().__init__(timeout, stdout=stdout, stderr=stderr)
        self.timeout = timeout
        self.context = context
        self.args = (
            f"Sandbox command exceeded {timeout:.0f}s of wall clock {context} without the service "
            "closing its output stream. The underlying call cannot be cancelled, so it was "
            "abandoned: the command may still be running or may have completed. Check whether it "
            "finished or is still running before running it again; to wait on a background job, "
            "poll it with short commands rather than one long `sleep`.",
        )

    def with_output(self, *, stdout: str, stderr: str) -> SandboxCommandTimeoutError:
        """The same timeout, carrying the output that streamed before it."""
        error = SandboxCommandTimeoutError(
            timeout=self.timeout, context=self.context, stdout=stdout, stderr=stderr
        )
        error.__cause__ = self.__cause__
        return error


class SandboxUnavailableError(RuntimeError):
    """The sandbox session is unhealthy and one restart did not bring it back.

    Raised instead of letting every later tool call hit the same dead sandbox: production saw one
    run take 34 consecutive ``502`` errors in twelve minutes. The message tells the model to stop
    retrying shell work rather than to try again.
    """


class SandboxExecutionUncertainError(SandboxUnavailableError):
    """Command completion is unknown; output is retained without logging it as diagnostics."""

    def __init__(self, message: str, *, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr

    def with_output(self, *, stdout: str, stderr: str) -> SandboxExecutionUncertainError:
        return type(self)(str(self), stdout=stdout, stderr=stderr)


class SandboxPlatformError(SandboxExecutionUncertainError):
    """The service explicitly reported platform exit -1."""


class SandboxFileTooLargeError(OSError):
    """``read_file`` refused a file over :data:`SANDBOX_READ_MAX_BYTES`; the message says how to read
    a range instead."""

    def __init__(self, path: str, size: int) -> None:
        quoted = shlex.quote(path)
        super().__init__(
            f"{path} is {size:,} bytes, over the {SANDBOX_READ_MAX_BYTES:,}-byte limit for reading "
            f"a whole file. Read only the part you need with the shell: `sed -n '1,200p' {quoted}` "
            f"(lines), `head -c 65536 {quoted}` / `tail -c 65536 {quoted}` (bytes), or "
            f"`grep -n PATTERN {quoted}` to find the lines first."
        )
        self.path = path
        self.size = size


class _CommandAbandoned(RuntimeError):
    """Raised in the pump thread when the caller stopped waiting before the command was sent."""


class _CommandCancelled(Exception):
    """The turn that issued the command was cancelled while it ran."""


# How often a running command checks its turn's cancel signal.
_CANCEL_POLL_SECONDS = 0.1

CANCELLED_NOTE = (
    "\n[strandly-sandbox] cancelled: the run was superseded or stopped, so this stopped waiting "
    "for the command. It was not killed and may still be running in the sandbox."
)


class _MalformedResponse(RuntimeError):
    """The command API answered in a shape this adapter cannot read. The message is adapter-made
    (response keys only, never values), so it is safe to quote in a diagnostic."""


class _StreamServiceError(RuntimeError):
    """A service exception delivered **on the result stream** rather than raised by botocore.

    Carries a botocore-shaped ``response`` so :func:`_error_code` names it like the raised form.
    It is never a rejection, whatever its code (see :func:`_is_rejection`).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


def _error_text(error: BaseException) -> str:
    """Everything an error says about itself: botocore's ``Error.Message`` plus ``str()``."""
    response = getattr(error, "response", None)
    message = ""
    detail = response.get("Error") if isinstance(response, dict) else None
    if isinstance(detail, dict):
        message = str(detail.get("Message") or "")
    return f"{message} {error}"


def _is_init_timeout(error: BaseException) -> bool:
    """True for a wake that failed because the session's start-up took over the 120 s init limit."""
    return "initialization time exceeded" in _error_text(error).lower()


#: Error codes that mean "the session's sandbox is not answering". A command that fails this way may
#: have executed, so it is never replayed (see :meth:`RuntimeSandbox._retrying`).
#: ``RuntimeClientError`` counts only when it carries a 5xx (a 4xx is a rejection).
_UNHEALTHY_ERROR_CODES = frozenset({"InternalServerException", "ServiceUnavailableException"})


def _is_unhealthy(error: BaseException) -> bool:
    """True for errors that mean the sandbox session itself is unhealthy (502 from the runtime)."""
    code = _error_code(error)
    if code in _UNHEALTHY_ERROR_CODES:
        return True
    if code != "RuntimeClientError" or _is_init_timeout(error):
        return False
    text = _error_text(error)
    return any(f"({status})" in text or f" {status} " in text for status in (500, 502, 503, 504))


#: Service error codes that refuse a command before it runs (the API's documented 4xx).
_REJECTION_ERROR_CODES = (
    _RETRYABLE_ERROR_CODES
    | _SESSION_GONE_ERROR_CODES
    | frozenset({"AccessDeniedException", "ServiceQuotaExceededException", "ValidationException"})
)

#: botocore failures raised before any request byte leaves the client.
_PRE_SEND_ERROR_NAMES = frozenset(
    {
        "ConnectTimeoutError",
        "CredentialRetrievalError",
        "EndpointConnectionError",
        "NoCredentialsError",
        "ParamValidationError",
        "PartialCredentialsError",
    }
)


def _request_id(source: object) -> str | None:
    """The AWS request id of a botocore response dict or ``ClientError``, if it carries one."""
    response = getattr(source, "response", source)
    metadata = response.get("ResponseMetadata") if isinstance(response, dict) else None
    request_id = metadata.get("RequestId") if isinstance(metadata, dict) else None
    return request_id if isinstance(request_id, str) and request_id else None


def _is_rejection(error: BaseException) -> bool:
    """True only when ``error`` proves the command was refused before it could run.

    Fail-closed: every other failure once the command API was entered (a transport error, a
    malformed response, a 5xx, an unrecognised code) may have executed and is never replayed.
    """
    if isinstance(error, _StreamServiceError) or type(error).__name__ == "EventStreamError":
        # Delivered on the result stream: HTTP 200 had already accepted the command.
        return False
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return type(error).__name__ in _PRE_SEND_ERROR_NAMES
    metadata = response.get("ResponseMetadata")
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    if _is_unhealthy(error) or (isinstance(status, int) and status >= 500):
        return False
    code = _error_code(error)
    if code == "RuntimeClientError":
        # Its own HTTP status (424) proves nothing: the sandbox's status rides in the message.
        text = _error_text(error)
        return _is_init_timeout(error) or any(f"({s})" in text for s in range(400, 500))
    return code in _REJECTION_ERROR_CODES or (isinstance(status, int) and 400 <= status < 500)


#: Commands one adapter (one sandbox session) runs at once. Well below the platform's 197 in-flight
#: commands per session container: the rest is headroom for processes commands leave behind.
SANDBOX_MAX_CONCURRENT_COMMANDS = 64
#: Pump threads beyond the cap, so a command over it is refused at once instead of queueing.
_COMMAND_POOL_HEADROOM = 4

#: Written to ``$HOME`` by the bootstrap. Home is not on the persistent mount, so a command that
#: finds it missing is in a container this adapter never set up (an out-of-band stop/resume).
_SETUP_SENTINEL = ".strandly-sandbox-setup"
_SETUP_MARKED = "strandly-sandbox: setup-marked"
#: Printed by the bootstrap when the sentinel was already there before it wrote it.
_SETUP_WAS_PRESENT = "strandly-sandbox: setup-was-present"
#: Exit status of a command whose setup guard refused to run it (see :meth:`RuntimeSandbox._wrap`).
_FRESH_SESSION_EXIT = 97

#: How long a stop/reset waits for already-admitted command sends to return from the command API
#: before giving up without stopping anything.
_DISPATCH_QUIESCE_SECONDS = 10.0

#: Wake failures worth another attempt on the same session id. Botocore already retries an
#: ``InternalServerException`` four times internally, so this layer adds a slower, longer backoff
#: (a second attempt usually lands on a now-warm image cache).
_WAKE_RETRYABLE_ERROR_CODES = frozenset(
    {
        "RetryableConflictException",
        "ThrottlingException",
        "InternalServerException",
        "ServiceUnavailableException",
    }
)


def _is_wake_retryable(error: BaseException) -> bool:
    """True for a wake failure that a later attempt on the same session id may not repeat."""
    return (
        _is_init_timeout(error)
        or _error_code(error) in _WAKE_RETRYABLE_ERROR_CODES
        or _is_unhealthy(error)  # a 5xx ``RuntimeClientError`` from the sandbox
    )


def _storage_failure(text: str) -> str | None:
    """The storage-error marker in ``text`` (ENOSPC, EDQUOT, EIO), if any."""
    lowered = text.lower()
    for marker in SANDBOX_STORAGE_ERROR_MARKERS:
        if marker.lower() in lowered:
            return marker
    return None


#: Model-facing advice appended to a command that failed on storage. The workspace is the session's
#: own EBS volume: ``df`` works on it, and the caches live on it (under :data:`SANDBOX_CACHE_ROOT`),
#: so the fix is deleting things.
STORAGE_ERROR_ADVICE = (
    f"[strandly-sandbox] that command failed on storage ({{marker}}). {SANDBOX_MOUNT_PATH} is this "
    "session's persistent disk{capacity}; `df -h "
    f"{SANDBOX_MOUNT_PATH}` shows its real usage and `du -xsh {SANDBOX_MOUNT_PATH}/* "
    f"{SANDBOX_MOUNT_PATH}/.[!.]* {SANDBOX_CACHE_ROOT}/* 2>/dev/null | sort -h` finds what is big "
    "(dot-directories included). Delete checkouts, build output and "
    "downloads you no longer need; package caches and CARGO_TARGET_DIR live in "
    f"{SANDBOX_CACHE_ROOT} and are always safe to delete. /tmp is the instance's own disk: it is "
    "wiped when the sandbox restarts, so keep work you need under the workspace."
)


def storage_error_advice(marker: str, capacity_kb: int | None = None) -> str:
    """:data:`STORAGE_ERROR_ADVICE` for ``marker``, with the volume's size when it is known."""
    capacity = f" (~{round(capacity_kb / (1024 * 1024))} GiB)" if capacity_kb else ""
    return STORAGE_ERROR_ADVICE.format(marker=marker, capacity=capacity)


_SANDBOX_DIGEST_LEN = 32


def sandbox_session_id(session_key: str | None) -> str:
    """The sandbox runtime session id for a harness session (or a fresh one when there is none).

    **This function is the whole of cross-invocation filesystem persistence.** The id is a pure
    function of the harness session key, so invocation N+1 of the same GitHub issue addresses the
    same sandbox session — and with the session's own EBS volume that filesystem is restored
    even if its instance was stopped in between. A derived id has no state to persist and no
    adoption race.

    Prefixed with :data:`~strandly.core.constants.SANDBOX_SESSION_PREFIX` so a sandbox
    session id can never collide with the *agent* runtime's affinity id derived from the same key —
    two different runtimes, two different id spaces. A keyed id ends in a digest of the full,
    unsanitized session key, so two different harness sessions never share a sandbox (and its
    volume) even if their ids would sanitize or truncate to the same text. Both forms fit the
    API's 33-256 character range.

    Args:
        session_key: The harness session key, or ``None`` for a sessionless invoke.

    Returns:
        A valid AgentCore Runtime session id.
    """
    # A sessionless invoke (CLI one-shot, a bare `strandly invoke` with no --session-id) shares no
    # history with anything, so it must not share a filesystem either: give it a fresh box.
    if not session_key:
        return f"{SANDBOX_SESSION_PREFIX}-ephemeral-{uuid.uuid4().hex}"
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:_SANDBOX_DIGEST_LEN]
    return f"{SANDBOX_SESSION_PREFIX}-{digest}"


def _chunk_text(chunks: list[dict[str, Any]]) -> str:
    """All stdout+stderr text in a collected chunk list, for the few places that inspect output.

    Used only by :meth:`RuntimeSandbox._bootstrap`, whose command carries no secret by construction.
    """
    parts: list[str] = []
    for chunk in chunks:
        delta = chunk.get("contentDelta")
        if isinstance(delta, dict):
            parts += [str(delta.get("stdout") or ""), str(delta.get("stderr") or "")]
    # A heartbeat byte can land between two stdout lines; left in, it glues itself to the next
    # marker line and the marker no longer parses.
    return strip_heartbeat("".join(parts))


#: Appended to the exit -1 error.
_SLOT_EXHAUSTION_HINT = (
    "The most common cause is command-slot exhaustion: leaked or unreaped (zombie) processes left "
    "by earlier commands each hold one of the session container's 197 command slots, and with "
    "none free every command returns -1. Stopping and resuming the session clears it. "
)

#: The health probe, and its answer. Only the sentinel decides health; the zombie count is
#: diagnostic only (no ``ps`` in the image) and may be missing or unreadable.
_HEALTH_PROBE = (
    "/bin/sh -c 'cd / && printf strandly-sandbox-health && "
    'printf " zombies=%s" "$(grep -sl "^State:.Z" /proc/[0-9]*/status | wc -l)"\''
)
_HEALTH_PROBE_SENTINEL = "strandly-sandbox-health"
_HEALTH_PROBE_ANSWER = _HEALTH_PROBE_SENTINEL + " zombies="


def _clamp_timeout(timeout: float | None) -> int:
    """Resolve a caller timeout to the integer seconds the command API accepts.

    ``None`` becomes :data:`SANDBOX_COMMAND_TIMEOUT_DEFAULT`; anything outside the service's 1–3600
    range is clamped rather than sent (an out-of-range value is an opaque ``ValidationException``,
    which the model cannot act on).
    """
    seconds = SANDBOX_COMMAND_TIMEOUT_DEFAULT if timeout is None else int(timeout)
    return max(SANDBOX_COMMAND_TIMEOUT_MIN, min(SANDBOX_COMMAND_TIMEOUT_MAX, seconds))


def _error_code(error: BaseException) -> str:
    """The botocore error code of ``error``, or its class name for a non-botocore exception."""
    response = getattr(error, "response", None)
    detail = response.get("Error") if isinstance(response, dict) else None
    if isinstance(detail, dict):
        code = detail.get("Code")
        if isinstance(code, str) and code:
            return code
    return type(error).__name__


def _sleep(seconds: float) -> None:
    """``time.sleep``, behind a seam so tests can skip retry delays without patching ``time``."""
    time.sleep(seconds)


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter for retry ``attempt`` (1-based)."""
    base = min(
        SANDBOX_RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), SANDBOX_RETRY_BACKOFF_MAX_SECONDS
    )
    return base + random.uniform(0, base / 2)


def sandbox_client_config() -> Any:
    """botocore ``Config`` for the sandbox's ``bedrock-agentcore`` client.

    Exactly one HTTP attempt: an uncertain command must not be silently replayed. Explicit
    rejection retries (conflicts/throttles) are bounded in ``_retrying``.
    """
    from botocore.config import Config

    return Config(
        read_timeout=SANDBOX_CLIENT_READ_TIMEOUT_SECONDS,
        connect_timeout=SANDBOX_CLIENT_CONNECT_TIMEOUT_SECONDS,
        retries={"total_max_attempts": 1, "mode": "standard"},
        # Every admitted command (and its pump headroom) gets its own pooled connection: with
        # botocore's default of 10, connections past the 10th are opened and thrown away per call.
        max_pool_connections=SANDBOX_MAX_CONCURRENT_COMMANDS + _COMMAND_POOL_HEADROOM,
        tcp_keepalive=True,
    )


def sandbox_wake_client_config() -> Any:
    """botocore ``Config`` for the client that sends the WAKE (``InvokeAgentRuntime``) only.

    A wake on Runtime Instances blocks for the instance boot (53–83 s), so: a 300 s read timeout
    (botocore's 60 s default timed out mid-boot) and exactly ONE send per call. Not botocore's
    default retries: a re-sent wake is a duplicate wake of a session that is still booting, and
    :meth:`RuntimeSandbox._wake` already retries transient failures with its own delay and budget.
    ``total_max_attempts`` — not ``max_attempts``, which in botocore 1.43 counts *retries*, so
    ``max_attempts=1`` would still send twice. The operator-approved recovery stop also uses this
    client, so that control call is bounded by this read timeout; commands and best-effort stops
    keep :func:`sandbox_client_config` (one HTTP attempt, with explicit rejection retries only).
    """
    from botocore.config import Config

    return Config(
        read_timeout=SANDBOX_WAKE_READ_TIMEOUT_SECONDS,
        connect_timeout=SANDBOX_WAKE_CONNECT_TIMEOUT_SECONDS,
        retries={"total_max_attempts": 1, "mode": "standard"},
        tcp_keepalive=True,
    )


#: botocore errors that prove the request never left the client: no TCP connection was made, so not
#: one byte of it was written. A command that fails this way is sent once more (see ``_retrying``).
#: ``ConnectionClosedError`` is NOT one: botocore raises it alike when a stale connection is reset
#: before the service reads the request and when the service read it and then closed (urllib3 2
#: swallows a failed write and reports the read), so it stays "completion unknown".
_NEVER_CONNECTED_ERROR_NAMES = frozenset({"ConnectTimeoutError", "EndpointConnectionError"})


@dataclass
class _SendTrace:
    """What one command send did on the wire, for the platform-failure log line."""

    started: float = field(default_factory=time.monotonic)
    connection: str = "none"
    request_id: str | None = None


#: The current thread's command send. botocore sends on the calling thread, so the pool hook below
#: and ``_retrying`` meet here.
_SEND_TRACE = threading.local()


@functools.cache
def _idle_bounded_pool(base: type) -> type:
    """``base`` (a botocore urllib3 pool class) that never reuses a connection idle too long.

    urllib3 already drops a pooled connection whose peer closed it (it is readable), but not one
    a path dropped silently, nor one the server is closing at that very moment. Either fails the
    next request with ``ConnectionClosedError`` and no way to know whether it was received.
    """

    if getattr(base, "_strandly_idle_bounded", False):
        return base

    class _IdleBoundedPool(base):  # type: ignore[misc,valid-type]
        _strandly_idle_bounded = True

        def _get_conn(self, timeout: float | None = None) -> Any:
            conn = super()._get_conn(timeout)
            idle_since = getattr(conn, "_strandly_idle_since", None)
            idle = None if idle_since is None else time.monotonic() - idle_since
            if getattr(conn, "sock", None) is None:
                label = "new"
            elif idle is not None and idle > SANDBOX_CLIENT_MAX_IDLE_SECONDS:
                conn.close()  # urllib3 reconnects a closed connection when it is used
                label = f"new (retired one idle {idle:.0f}s)"
            else:
                label = "reused" if idle is None else f"reused after {idle:.1f}s idle"
            trace = getattr(_SEND_TRACE, "current", None)
            if trace is not None:
                trace.connection = label
            return conn

        def _put_conn(self, conn: Any) -> None:
            if conn is not None:
                conn._strandly_idle_since = time.monotonic()
            super()._put_conn(conn)

    _IdleBoundedPool.__name__ = _IdleBoundedPool.__qualname__ = f"IdleBounded{base.__name__}"
    return _IdleBoundedPool


def _keepalive_socket_options() -> list[tuple[int, int, int]]:
    import socket

    options = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    # TCP_KEEPIDLE on Linux, TCP_KEEPALIVE on macOS.
    idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
    for name, value in (
        (idle, SANDBOX_CLIENT_KEEPALIVE_IDLE_SECONDS),
        (getattr(socket, "TCP_KEEPINTVL", None), SANDBOX_CLIENT_KEEPALIVE_INTERVAL_SECONDS),
        (getattr(socket, "TCP_KEEPCNT", None), SANDBOX_CLIENT_KEEPALIVE_PROBES),
    ):
        if name is not None:
            options.append((socket.IPPROTO_TCP, name, value))
    return options


def keep_connections_healthy(client: Any) -> Any:
    """Make ``client``'s pooled connections safe to reuse; returns ``client``.

    Pooled connections idle over ``SANDBOX_CLIENT_MAX_IDLE_SECONDS`` are reconnected instead of
    reused, and every socket gets TCP keepalive probes after
    ``SANDBOX_CLIENT_KEEPALIVE_IDLE_SECONDS``. botocore has no public setting for either, so this
    edits its URLLib3Session before the first request (pools are created on first use). If a
    botocore upgrade moves those internals, it logs and leaves the client as it is; a unit test
    pins that it applies to the installed botocore.
    """
    session = getattr(getattr(client, "_endpoint", None), "http_session", None)
    manager = getattr(session, "_manager", None)
    classes = getattr(manager, "pool_classes_by_scheme", None)
    options = getattr(session, "_socket_options", None)
    pool_kw = getattr(manager, "connection_pool_kw", None)
    if not isinstance(classes, dict) or not isinstance(options, list) or not isinstance(pool_kw, dict):
        logger.warning(
            "sandbox client: botocore internals changed; idle connections are not bounded (%s)",
            type(session).__name__,
        )
        return client
    for option in _keepalive_socket_options():
        if option not in options:
            options.append(option)
    if pool_kw.get("socket_options") is not options:
        pool_kw["socket_options"] = list(options)
    # In place: botocore's proxy managers share this mapping.
    for scheme, pool_cls in list(classes.items()):
        classes[scheme] = _idle_bounded_pool(pool_cls)
    return client


def _resolve_region(region: str | None) -> str | None:
    """Resolve an AWS region from the argument then the standard environment variables.

    Returns ``None`` when neither is set, letting boto3 apply its own resolution (profile, IMDS)
    rather than inventing a region — a wrong region here surfaces as a confusing
    ``ResourceNotFoundException`` on the runtime ARN.
    """
    return region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")


async def _await(hook: Callable[[], Awaitable[None]]) -> None:
    """Await ``hook()`` — a coroutine for :func:`asyncio.run`, which wants one, not a callable."""
    await hook()


async def _wait_or_cancel(future: asyncio.Future[Any], cancel_signal: threading.Event | None) -> None:
    """Wait until ``future`` is done, without ever cancelling it.

    Raises :class:`_CommandCancelled` as soon as ``cancel_signal`` is set; ``future`` keeps running.
    """
    while not future.done():
        if cancel_signal is not None and cancel_signal.is_set():
            raise _CommandCancelled()
        await asyncio.wait({future}, timeout=None if cancel_signal is None else _CANCEL_POLL_SECONDS)


#: How much of a failure's message a warning keeps.
_FAILURE_DETAIL_CHARS = 500


def _failure_detail(error: BaseException, context: str) -> str:
    """``error``'s message on one line, minus ``context``, keeping the end (where stderr lands)."""
    text = " ".join(str(error).replace(context, "").split())
    if len(text) <= _FAILURE_DETAIL_CHARS:
        return text
    return "…" + text[-_FAILURE_DETAIL_CHARS:]


class RuntimeSandbox(PosixShellSandbox):
    """Execute commands and file operations inside a dedicated AgentCore **Runtime** session.

    Only :meth:`execute_streaming` (and, for the 64 KB limit, :meth:`write_file`) is implemented; the
    rest of the :class:`~strands.sandbox.base.Sandbox` surface is
    :class:`~strands.sandbox.posix_shell.PosixShellSandbox`'s shell-based defaults.

    ``cwd`` defaults to :data:`~strandly.core.constants.SANDBOX_WORKDIR` — the persistent
    persistent EBS workspace volume — so work the agent does without thinking about paths lands somewhere that
    survives a session resume.

    Example:
        Attach to the sandbox runtime for a harness session::

            sandbox = RuntimeSandbox(
                runtime_arn="arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/strandly-sandbox",
                session_key="gh-strands-agents-sdk-python-b00b0499-issue-42",
                region="us-west-2",
            )
            # Nothing is provisioned yet: the first command wakes the session (and waits for it).
            result = await sandbox.execute(
                "git clone --filter=blob:none https://github.com/strands-agents/sdk-python"
            )
    """

    def __init__(
        self,
        *,
        runtime_arn: str,
        session_key: str | None = None,
        session_id: str | None = None,
        region: str | None = None,
        qualifier: str | None = None,
        workdir: str = SANDBOX_WORKDIR,
        github_token: str | None = None,
        git_author_name: str = SANDBOX_GIT_COMMIT_NAME,
        git_author_email: str = SANDBOX_GIT_COMMIT_EMAIL,
        client: Any | None = None,
        ttl: SessionTtl | None = None,
    ) -> None:
        """Initialize the sandbox.

        Args:
            runtime_arn: ARN of the **sandbox** AgentCore Runtime (never the agent's own).
            session_key: The harness session key the sandbox session is derived from (see
                :func:`sandbox_session_id`). Ignored when ``session_id`` is given.
            session_id: An explicit sandbox session id, bypassing derivation. For tests and for a
                caller that owns session naming itself.
            region: AWS region. Falls back to ``AWS_REGION`` / ``AWS_DEFAULT_REGION``, then boto3's
                own resolution. Unused when ``client`` is supplied.
            qualifier: Runtime endpoint qualifier (e.g. ``"DEFAULT"``, or a version). ``None`` lets
                the service pick the default endpoint.
            workdir: Default working directory for every command.
            github_token: The sandbox's **own**, deliberately weaker git credential — never the
                ``use_github`` token. Anything the agent runs can read it (for example with
                ``git credential fill``), so scope it to ``Contents`` only: it may clone/fetch/
                push, and it may not comment, review or approve as the agent. ``None`` leaves the
                sandbox credential-free.
            client: A pre-built ``bedrock-agentcore`` boto3 client (tests inject a fake).
            ttl: The sandbox sessions' sliding TTL (:mod:`~strandly.sandbox.ttl`): every wake
                and command re-schedules this session's deletion (instance + EBS volume). ``None`` (a deploy
                without the wiring, or a test) schedules nothing.
        """
        self.git_author_name = git_author_name
        self.git_author_email = git_author_email
        self.runtime_arn = runtime_arn
        self.qualifier = qualifier
        self.workdir = workdir
        self.session_id = session_id or sandbox_session_id(session_key)
        self._region = _resolve_region(region)
        self._github_token = github_token
        self._client = client
        # The wake's own client (``sandbox_wake_client_config``), built lazily; an injected
        # ``client`` (tests) serves wakes too.
        self._wake_client: Any | None = client
        self._ttl = ttl
        # The running turn's cancel signal (``bind_cancel_signal``); a command abandons its wait
        # once the signal it started under is set.
        self._cancel_signal: threading.Event | None = None
        # A sessionless invoke's throwaway session (no key, no explicit id): no later run reuses it,
        # so it gets one fixed TTL past the longest possible run instead of a sliding one.
        self._ephemeral = session_id is None and not session_key
        # Set once the session has been woken + git-configured. Guarded by ``_ready_lock`` so N
        # concurrent tool calls (or concurrent subagents) bootstrap exactly once between
        # them — it is released before any user command runs. Commands themselves are never
        # serialized: the API is documented as concurrent-safe on one session, which is what makes
        # concurrent subagents work here.
        #
        # It is a ``threading.Lock``, NOT an ``asyncio.Lock``, and that is a correctness requirement
        # rather than a preference. ``subagent`` hands a child this *same object*, and a subagent can
        # run on a different event loop (``strands._async.run_async`` submits ``asyncio.run`` to a
        # thread pool). An ``asyncio.Lock`` binds to the first loop that ever *waits* on it, so a
        # second loop contending for it hangs forever — three ingredients of the v30–v32 production
        # deadlock, one of which was a lock in exactly this position. A threading lock is held inside
        # a worker thread (:meth:`_prepare_once_sync`), belongs to no loop, and cannot wedge one.
        self._ready = False
        self._ready_lock = threading.Lock()
        # Whether the sandbox confirmed writing the git credential we sent (see
        # :meth:`_note_wake_response`). Only meaningful when ``github_token`` was given.
        self._credential_written = False
        # Background warm-up task (see :meth:`warm_up`). When set, ``_ensure_ready`` awaits it so the
        # first real command waits only for whatever provisioning has not already overlapped the
        # agent's earlier non-sandbox work (its first model call, a GitHub read). The loop it was
        # created on is remembered because an ``asyncio.Task`` is as loop-bound as a lock: awaiting it
        # from a foreign loop is the same hazard, on the same shared object.
        self._warmup_task: asyncio.Task[None] | None = None
        self._warmup_loop: asyncio.AbstractEventLoop | None = None
        # First-use hooks (see :meth:`add_first_use_hook`): work that needs the sandbox but must not
        # sit on the agent-build path — today the built-in skills push. Run once each, in order,
        # after the wake and before the first command that triggered it; ``_first_use_ran`` counts
        # the hooks already started. A ``threading.Lock`` for the same cross-loop reason as
        # ``_ready_lock`` (subagents share this object and may run on another loop).
        self._first_use_hooks: list[Callable[[], Awaitable[None]]] = []
        self._first_use_ran = 0
        self._first_use_lock = threading.Lock()
        # ---- reliability state ----
        # Model-facing notes (what a prune deleted, that a restart killed background jobs, that the
        # workspace is full). Append-only: each agent's :class:`~strandly.sandbox.notices.
        # SandboxNotices` plugin delivers the ones it has not delivered yet on its next tool result,
        # so no internal call (skills push, environment probe) can swallow one.
        self._notices: list[str] = []
        self._notices_lock = threading.Lock()
        # Consecutive commands that failed on storage (ENOSPC / EIO), and the lock that makes one
        # caller — not every concurrent subagent — run the recovery.
        self._storage_strikes = 0
        self._recovery_lock = threading.Lock()
        self._platform_lock = threading.Lock()
        self._platform_error: SandboxExecutionUncertainError | None = None
        self._platform_recovering = False
        # monotonic time of the last health probe (``-inf``: none yet), and what it saw (status and
        # exit code, or the error type; never output): see _probe_platform_once_sync
        self._platform_last_probe = float("-inf")
        self._last_probe_outcome = "none"
        self._platform_needs_bootstrap = False
        self._platform_reset_attempted = False
        # An approved reset reached its stop boundary and has not completed: the container and
        # its setup may be gone, so a bare health probe must not restore readiness.
        self._platform_reset_incomplete = False
        # Dispatch boundary between command sends and a stop/reset (see :meth:`_dispatch_fence`):
        # sends admitted but not yet returned from the command API, and fences currently held.
        self._dispatch_cond = threading.Condition()
        self._dispatching = 0
        self._dispatch_fences = 0
        self._commands_lock = threading.Lock()
        self._commands_running = 0
        # Command pumps run here, not on the loop's default executor (``min(32, cpus + 4)``
        # workers), so the cap above is the limit that applies. Created on first use.
        self._command_pool: concurrent.futures.ThreadPoolExecutor | None = None
        # The workspace volume's real size, from the wake response or the health script's ``df``
        # (``None`` until one reported it). Only used to word model-facing advice.
        self._capacity_kb: int | None = None
        # Bootstraps that wrote the setup sentinel; user commands are guarded once there is one.
        self._setup_epoch = 0
        # Whether the last bootstrap found the sentinel already there, and the epoch a redo started
        # from when its bootstrap did: a refusal seen in that epoch was not the guard's (P3-A).
        self._setup_was_present = False
        self._forged_epoch: int | None = None
        self._fresh_marker = f"strandly-sandbox-fresh-{uuid.uuid4().hex[:16]}"
        # Unhealthy-session restarts are rate limited; a failed restart opens a short circuit in
        # which commands fail fast instead of hammering a dead sandbox.
        self._last_restart = float("-inf")
        # Fence late failures from commands issued before recovery: they must not poison the
        # new healthy generation. Parent and subagents share this state across event loops.
        self._restart_generation = 0
        self._circuit_open_until = float("-inf")

    # ---- client / session lifecycle ------------------------------------------------------------

    def _ensure_client(self) -> Any:
        """Lazily build the ``bedrock-agentcore`` data-plane client.

        The read timeout must out-wait the longest command budget: command output is streamed, so a
        silent command means a silent socket, and botocore's 60s default cut every quiet command off
        at ~60s regardless of its ``timeout`` (see ``SANDBOX_CLIENT_READ_TIMEOUT_SECONDS``).
        """
        if self._client is None:
            import boto3

            self._client = keep_connections_healthy(
                boto3.Session(region_name=self._region).client(
                    "bedrock-agentcore", config=sandbox_client_config()
                )
            )
        return self._client

    def _ensure_wake_client(self) -> Any:
        """Lazily build the client the wake is sent on: long read timeout, a single send."""
        if self._wake_client is None:
            import boto3

            self._wake_client = keep_connections_healthy(
                boto3.Session(region_name=self._region).client(
                    "bedrock-agentcore", config=sandbox_wake_client_config()
                )
            )
        return self._wake_client

    def _error_context(self) -> str:
        """Identifying context appended to every error this adapter raises.

        Production surfaced sandbox failures that named neither the session nor the resource, which
        made them unattributable in the logs. Every raise below carries this.
        """
        return f"[sandbox_runtime={self.runtime_arn}, session={self.session_id}]"

    def _command_kwargs(self, command: str, timeout: int) -> dict[str, Any]:
        """Request kwargs for one ``InvokeAgentRuntimeCommand`` call."""
        kwargs: dict[str, Any] = {
            "agentRuntimeArn": self.runtime_arn,
            "runtimeSessionId": self.session_id,
            "contentType": "application/json",
            "accept": "application/vnd.amazon.eventstream",
            "body": {"command": command, "timeout": timeout},
        }
        if self.qualifier:
            kwargs["qualifier"] = self.qualifier
        return kwargs

    def _wake(self) -> None:
        """:meth:`_wake_once`, retried with backoff on transient wake failures.

        Seven production runs died on a single ``Runtime initialization time exceeded`` (a cold
        start over the 120 s init limit) because the wake was tried exactly once. A second attempt
        usually lands on a warm image cache, so init timeouts, throttles, conflicts and 5xx get
        :data:`SANDBOX_WAKE_MAX_ATTEMPTS` attempts, :data:`SANDBOX_WAKE_RETRY_DELAY_SECONDS` apart
        (plus jitter), within :data:`SANDBOX_WAKE_BUDGET_SECONDS`. Anything else — an access error,
        a bad ARN — raises at once. Running out of attempts or budget opens the circuit and raises
        :class:`SandboxUnavailableError`, so the next tool calls fail fast instead of each paying
        for another full round of wakes.
        """
        attempt = 0
        started = time.monotonic()
        while True:
            attempt += 1
            try:
                self._wake_once()
                self._bump_ttl()
                return
            except Exception as e:  # noqa: BLE001 — classified immediately below; unknowns re-raise
                if not _is_wake_retryable(e):
                    raise
                delay = SANDBOX_WAKE_RETRY_DELAY_SECONDS + random.uniform(0, 5)
                elapsed = time.monotonic() - started
                if attempt >= SANDBOX_WAKE_MAX_ATTEMPTS or elapsed + delay >= SANDBOX_WAKE_BUDGET_SECONDS:
                    # A transient wake failure (an init timeout, a 5xx) can leave a launched
                    # instance and an attached volume behind; schedule its deletion anyway. A delete
                    # of a session that never came up is a harmless target error.
                    self._bump_ttl()
                    self._circuit_open_until = time.monotonic() + SANDBOX_CIRCUIT_OPEN_SECONDS
                    raise SandboxUnavailableError(
                        f"The sandbox could not be started {self._error_context()}: "
                        f"{_error_code(e)}: {_error_text(e).strip()[:300]} (after {attempt} "
                        f"attempt(s) over {elapsed:.0f}s). Shell and file tools will fail fast for "
                        f"the next {SANDBOX_CIRCUIT_OPEN_SECONDS:.0f}s. Do not retry in a loop: "
                        "finish with what you have, or report that the sandbox is down."
                    ) from e
                logger.warning(
                    "sandbox wake failed (%s) on attempt %d/%d; retrying in %.0fs %s",
                    _error_code(e),
                    attempt,
                    SANDBOX_WAKE_MAX_ATTEMPTS,
                    delay,
                    self._error_context(),
                )
                _sleep(delay)

    def _bump_ttl(self) -> None:
        """Slide this session's deletion TTL (:mod:`~strandly.sandbox.ttl`). Never raises.

        Fire-and-forget: only the throttle check runs here; the (synchronous, boto) Scheduler upsert
        runs on a background thread, so neither a wake nor a command ever waits on Scheduler.
        Called after every wake (so a session is scheduled on its first command and after every
        restart), after a wake that gave up on transient errors (which may still have launched the
        instance), and at the start of every command — shell and file operations alike — so a run
        that keeps using its sandbox keeps sliding the TTL however long it lasts. Throttled to once
        per hour per session after a success, and retried a minute after a failure, inside
        :class:`SessionTtl`.
        """
        if self._ttl is None:
            return
        self._ttl.bump_soon(self.session_id, ephemeral=self._ephemeral)

    def _wake_once(self) -> None:
        """Start the session's instance, and hand it the git credential, in one invoke.

        ``InvokeAgentRuntimeCommand`` runs inside an *active* session and answers
        ``ResourceNotFoundException`` when there isn't one, so the session has to exist first. This is
        that step: an agent invoke whose main job is to make AgentCore start the box. Runs in a worker
        thread (blocking boto3) — see :meth:`_ensure_ready`.

        **SECURITY: this is also the only channel the git credential travels on.** The service writes
        every executed *command* to the sandbox's CloudWatch log group, so a credential passed as part
        of a shell command would be logged in plaintext for every session, forever. Invoke payloads
        are not logged by the service, CloudTrail logs no payloads at all, and ``BedrockAgentCoreApp``
        does not log request bodies — so the token rides here and ``sandbox_main.py`` writes the
        credential file itself. Nothing in this method logs the payload.

        The credential is (re)sent on every wake on purpose: it lives in the container's ``$HOME``,
        not on the persistent mount, so a session whose instance was stopped and started again needs it
        written again.
        """
        client = self._ensure_wake_client()
        payload = dict(WAKE_PAYLOAD)
        if self._github_token:
            payload[SANDBOX_GIT_CREDENTIAL_KEY] = self._github_token
        kwargs: dict[str, Any] = {
            "agentRuntimeArn": self.runtime_arn,
            "runtimeSessionId": self.session_id,
            "payload": json.dumps(payload).encode(),
            "contentType": "application/json",
        }
        if self.qualifier:
            kwargs["qualifier"] = self.qualifier
        response = client.invoke_agent_runtime(**kwargs)
        # Drain the body so the connection is released — and read it, because the sandbox reports
        # things only it can know (did the credential land? is the mount writable? is a tool missing?).
        # `read` is fetched as an attribute so mypy sees a callable rather than an optional streaming
        # body.
        body = response.get("response") if isinstance(response, dict) else None
        read = getattr(body, "read", None)
        self._note_wake_response(read() if callable(read) else None)

    def _note_wake_response(self, raw: Any) -> None:
        """Log what the sandbox said about itself. Best-effort, and never logs the response verbatim.

        Three findings, each of which is otherwise invisible until it surfaces as a confusing failure
        mid-turn — and the first two are the checks no local test can make (the image's real contents,
        and whether a service-mounted volume lands writable for a non-root user):

        * a missing expected tool (``git``, ``rg``, ``uv``, …) — an image built wrong;
        * a workspace that is not writable;
        * a git credential that was sent but not confirmed written, which means the sandbox image
          predates payload-delivered credentials. There is deliberately **no fallback** to the old
          command-based write in that case: it would put the token back in CloudWatch, which is the
          whole reason for this change. Pushes fail loudly instead.

        Only known keys are logged, and only after coercion — the response is data from another
        process, and this method must not become a way for it to get arbitrary text into our logs.
        """
        try:
            status = json.loads(raw) if raw else None
        except (TypeError, ValueError, UnicodeDecodeError):
            status = None
        if not isinstance(status, dict):
            return
        missing = status.get("missingTools")
        if isinstance(missing, list) and missing:
            logger.warning(
                "sandbox image is missing expected tools %s %s; commands needing them will fail",
                sorted(str(name)[:40] for name in missing)[:20],
                self._error_context(),
            )
        capacity = status.get("workspaceCapacityBytes")
        if isinstance(capacity, int) and not isinstance(capacity, bool) and capacity > 0:
            self._capacity_kb = capacity // 1024
        if status.get("workspaceReady") is False:
            logger.warning(
                "sandbox workspace %s is not writable %s; the workspace volume may not be "
                "owned by the image's user",
                self.workdir,
                self._error_context(),
            )
        credential_status = status.get(SANDBOX_GIT_CREDENTIAL_STATUS_KEY)
        self._credential_written = credential_status == SANDBOX_GIT_CREDENTIAL_WRITTEN
        if self._github_token and not self._credential_written:
            logger.warning(
                "the sandbox did not confirm writing the git credential (status=%r) %s; git pushes "
                "will fail. Is the sandbox image built from the current Dockerfile.sandbox?",
                str(credential_status)[:60],
                self._error_context(),
            )

    def _bootstrap(self, *, force_prune: bool = False) -> WorkspaceHealth:
        """Configure git and check the workspace in the freshly-woken session.

        Fail-open only for a command the service provably refused (optional git setup should cost
        the agent ``git commit``, not the whole turn). An uncertain or platform outcome raises
        :class:`SandboxExecutionUncertainError`: it is not readiness.

        This command carries **no secret** — the credential arrived with the wake payload
        (:meth:`_wake`) precisely because the service logs commands — so the script and its output
        are safe to log. What it *is* read for:

        * the one failure mode the wake response cannot see: a credential written to a different
          ``$HOME`` than the one the command shell has;
        * the workspace's health (:func:`workspace_health_script`, same command, no extra round
          trip): its real usage, whether regenerable directories were pruned, and whether it still
          accepts a write. The caller acts on the result (:meth:`_settle_workspace`).
        """
        self._setup_was_present = False
        script = "\n".join(
            [
                # A subshell: the git script is `set -e` and exits early on an unwritable workspace,
                # which is exactly when the health check below must still run.
                "(",
                git_bootstrap_script(
                    workdir=self.workdir, expect_credential=bool(self._github_token),
                    author_name=self.git_author_name, author_email=self.git_author_email
                ),
                ")",
                f'[ -e "$HOME/{_SETUP_SENTINEL}" ] && echo "{_SETUP_WAS_PRESENT}"',
                f': > "$HOME/{_SETUP_SENTINEL}" 2>/dev/null && echo "{_SETUP_MARKED}"',
                workspace_health_script(self.workdir, force_prune=force_prune),
            ]
        )
        try:
            chunks = self._run_command_sync(
                # cwd="/" deliberately: this script is what guarantees the workspace exists, so it
                # must not `cd` into it first.
                self._wrap(script, cwd="/"),
                _clamp_timeout(180),
                allow_wake=False,
            )
        except SandboxExecutionUncertainError:
            raise
        except Exception as e:  # noqa: BLE001 — git setup is best-effort, platform health is not
            logger.warning(
                "sandbox git bootstrap failed (%s) %s; git may be unconfigured",
                type(e).__name__,
                self._error_context(),
            )
            return WorkspaceHealth()
        text = _chunk_text(chunks)
        self._setup_was_present = _SETUP_WAS_PRESENT in text
        if _SETUP_MARKED in text:
            self._setup_epoch += 1
        if SANDBOX_GIT_CREDENTIAL_MISSING_MARKER in text:
            logger.warning(
                "the git credential is not visible to the command shell %s: the entrypoint's $HOME "
                "and the shell's differ, so pushes will fail while everything else works",
                self._error_context(),
            )
        health = parse_workspace_health(text)
        if health.capacity_kb:
            self._capacity_kb = health.capacity_kb
        if health.used_kb is None and HEALTH_USED_MARKER in text:
            logger.warning(
                "could not measure sandbox workspace %s with df (timed out or failed); not pruning "
                "on a guess %s",
                self.workdir,
                self._error_context(),
            )
        if health.pruned_kb is not None and not health.pruned_paths and health.pruned_kb > 0:
            # Only the caches went (no regenerable directory matched): still worth a log line.
            logger.warning(
                "sandbox workspace was %s KiB of %s; emptied %s (%s KiB) %s",
                health.used_kb,
                health.capacity_kb,
                SANDBOX_CACHE_ROOT,
                health.pruned_kb,
                self._error_context(),
            )
        if health.pruned_paths:
            listing = ", ".join(health.pruned_paths)
            logger.warning(
                "sandbox workspace was %s KiB; pruned %s KiB of regenerable directories (%s) %s",
                health.used_kb,
                health.pruned_kb,
                listing,
                self._error_context(),
            )
            freed = (
                f" to free ~{health.pruned_kb // 1024} MB"
                if health.pruned_kb and health.pruned_kb > 0
                else ""
            )
            self._add_notice(
                f"[strandly-sandbox] {self.workdir} was nearly full, so these regenerable paths "
                f"(relative to {self.workdir}) were deleted{freed}: {listing}, and the package "
                f"caches in {SANDBOX_CACHE_ROOT} were emptied. Clones, .git and tracked files were "
                "not touched. Reinstall dependencies if a project needs them; the caches refill on "
                "demand."
            )
        return health

    def _add_notice(self, notice: str) -> None:
        """Record a model-facing note (delivered by :class:`~.notices.SandboxNotices`)."""
        with self._notices_lock:
            if not self._notices or self._notices[-1] != notice:
                self._notices.append(notice)

    def notices_since(self, seen: int) -> tuple[list[str], int]:
        """Notes recorded after the first ``seen``, and the new count to pass next time."""
        with self._notices_lock:
            return self._notices[seen:], len(self._notices)

    def _stop_session_sync(self) -> None:
        """``StopRuntimeSession`` for the current session. Blocking; best-effort; never raises."""
        kwargs: dict[str, Any] = {
            "agentRuntimeArn": self.runtime_arn,
            "runtimeSessionId": self.session_id,
        }
        if self.qualifier:
            kwargs["qualifier"] = self.qualifier
        with self._dispatch_fence() as quiet:
            try:
                if not quiet:
                    raise RuntimeError("a command dispatch is still in progress; not stopped")
                self._ensure_client().stop_runtime_session(**kwargs)
            except Exception as e:  # noqa: BLE001 — a restart proceeds to the wake either way
                logger.warning("stopping sandbox session failed (%s) %s", e, self._error_context())
        self._ready = False

    def _wake_and_bootstrap(self) -> bool:
        """Wake (with retries), bootstrap, and settle the workspace. **Blocking; worker thread.**

        There is exactly one session per harness session: it is never swapped for a fresh one, so
        a broken or full workspace is reported rather than silently abandoned with its clones.

        Returns whether the bootstrap found the setup sentinel already in place (read before
        :meth:`_settle_workspace`, whose own bootstraps see the one just written).
        """
        self._wake()
        health = self._bootstrap()
        present = self._setup_was_present
        self._settle_workspace(health)
        with self._platform_lock:
            if self._platform_error is not None:
                raise self._platform_error
            self._ready = True
        return present

    def _settle_workspace(self, health: WorkspaceHealth) -> None:
        """React to a failed write probe. Blocking.

        First, if the usage check did not prune, prune regenerable directories and probe again.

        * **Out of space** (the probe failed even after the health script pruned): a restart cannot
          free files. The session stays usable — deleting works on a full disk — and the model is
          told how full it is and what to remove. The session is never swapped for a fresh one:
          that would silently throw away its workspace.
        * **I/O fault** (``Input/output error``, a read-only remount, a permission fault): restart
          the session once — its instance comes back with the volume re-attached — and re-probe. If
          it still fails, raise :class:`SandboxUnavailableError` (and open the circuit): nothing the
          model does can fix it.
        """
        if self._platform_error is not None:
            raise self._platform_error
        if health.writable is not False:
            return
        logger.warning(
            "sandbox workspace %s rejects writes (%s) %s",
            self.workdir,
            health.detail or "writes fail",
            self._error_context(),
        )
        if health.pruned_kb is None:
            # The usage check did not prune (``df`` reported under the threshold): before anything
            # more drastic, prune regenerable directories and probe again.
            health = self._bootstrap(force_prune=True)
            if health.writable is not False:
                return
        if not health.full:
            with self._platform_lock:
                # End the generation first: a command readied before this stop is refused rather
                # than sent between the stop and the wake below.
                self._restart_generation += 1
                self._ready = False
            self._stop_session_sync()
            self._add_notice(
                "[strandly-sandbox] the sandbox was restarted because its workspace stopped "
                "accepting writes. Persistent /mnt/workspace was retained; background processes "
                "and files outside that mount may be lost."
            )
            self._wake()
            health = self._bootstrap(force_prune=True)
            if health.writable is not False:
                return
        if health.full:
            used = f"~{health.used_kb // 1024} MB used" if health.used_kb is not None else "no room"
            capacity_kb = health.capacity_kb or self._capacity_kb
            size = f" of its ~{round(capacity_kb / (1024 * 1024))} GiB" if capacity_kb else ""
            logger.warning(
                "sandbox workspace %s is full (%s KiB used) after pruning %s",
                self.workdir,
                health.used_kb,
                self._error_context(),
            )
            self._add_notice(
                f"[strandly-sandbox] {self.workdir} is FULL ({used}{size}) even after regenerable "
                "directories and package caches were pruned, so writes there fail. Free space "
                f"before anything else: `du -xsh {self.workdir}/* {self.workdir}/.[!.]* 2>/dev/null "
                "| sort -h` shows what is using it (dot-directories included); delete clones or files you no longer need with `rm -rf` (deleting works on a "
                "full disk). Nothing was deleted for you beyond regenerable directories and caches."
            )
            return
        self._circuit_open_until = time.monotonic() + SANDBOX_CIRCUIT_OPEN_SECONDS
        logger.error(
            "sandbox workspace %s still rejects writes after a restart (%s) %s",
            self.workdir,
            health.detail or "writes fail",
            self._error_context(),
        )
        raise SandboxUnavailableError(
            f"The sandbox is unavailable {self._error_context()}: its workspace {self.workdir} "
            f"rejects writes ({health.detail or 'writes fail'}) even after a restart. This is a "
            "workspace access failure, not proof of a damaged volume. Do not retry in a loop. "
            "Operators: check mount health, permissions and storage limits. Do not delete the "
            "session or its persistent volume without an explicit owner-approved data-loss decision."
        )

    def _prepare_once_sync(self) -> None:
        """Wake + git-configure the session, at most once per instance. **Blocking; worker thread.**

        The whole critical section runs in one thread under a ``threading.Lock`` rather than as
        ``async with`` around two ``to_thread`` calls. That is the fix for a hang, not a style choice:
        an ``asyncio.Lock`` on an object subagents share across event loops wedges the second loop
        permanently once it has to wait. Here the waiting happens on a worker thread, so a contending
        caller on any loop blocks for the length of one wake and then proceeds.

        The cost is one blocked worker thread per concurrent first-command, for the duration of the
        wake. Bounded and brief (this runs at most once per instance), and the alternative was
        unbounded.
        """
        if self._ready:
            return
        with self._ready_lock:
            if self._ready:  # re-check under the lock
                return
            if self._platform_error is not None:
                self._probe_platform_once_sync()
                return
            logger.info("waking sandbox session %s", self._error_context())
            self._wake_and_bootstrap()
            logger.info("sandbox session ready %s", self._error_context())

    def _record_platform_failure(
        self,
        generation: int,
        *,
        reason: str | None = None,
        trace: _SendTrace | None = None,
        error: BaseException | None = None,
        response_started: bool = True,
    ) -> SandboxExecutionUncertainError:
        error_type = SandboxPlatformError if reason is None else SandboxExecutionUncertainError
        detail = "command platform failure (exitCode=-1)" if reason is None else reason
        cause = "" if reason is not None else _SLOT_EXHAUSTION_HINT
        uncertain = error_type(
            f"AgentCore {detail} {self._error_context()} [generation={generation}]. "
            "The command's execution and side effects are unknown; it was NOT replayed. "
            "No process error or OOM cause can be inferred from this result, even with empty stderr. "
            f"{cause}"
            "The sandbox is unhealthy. Calls share one harmless health probe (at most one every "
            f"{SANDBOX_PLATFORM_PROBE_INTERVAL_SECONDS:.0f}s) and otherwise fail fast without being "
            "sent; do not retry commands in a loop. Operator-approved StopRuntimeSession/resume "
            "may recover it, but files outside /mnt/workspace and background processes can be lost. "
            "Never delete the capacity-provider session to recover user work."
        )
        with self._platform_lock:
            if generation == self._restart_generation:
                if self._platform_error is None:
                    self._platform_needs_bootstrap = not self._ready
                self._platform_error = uncertain
                self._ready = False
        # The only record of the failure outside the tool result (codes and types only, never SDK
        # text or command output).
        logger.warning(
            "sandbox command platform failure: %s %s [generation=%d] [%s]; calls now fail fast "
            "until a health probe passes",
            detail,
            self._error_context(),
            generation,
            _send_failure_detail(trace, error, response_started=response_started),
        )
        return uncertain

    def _probe_platform_once_sync(self) -> None:
        """Probe without replaying work or stopping a runtime. Caller holds the readiness lock."""
        assert self._platform_error is not None
        error = self._platform_error
        if self._platform_reset_incomplete:
            raise error
        # One probe per window, shared by every caller and every failure in it; between probes,
        # calls fail fast without sending anything.
        now = time.monotonic()
        if now - self._platform_last_probe < SANDBOX_PLATFORM_PROBE_INTERVAL_SECONDS:
            raise error
        self._platform_last_probe = now
        healthy = self._command_plane_healthy_sync()
        logger.warning(
            "sandbox health probe after a platform failure %s: %s %s",
            "passed" if healthy else "failed",
            self._last_probe_outcome,
            self._error_context(),
        )
        if not healthy:
            raise error
        if self._platform_needs_bootstrap:
            self._platform_recovering = True
            try:
                self._wake()
                if self._bootstrap().writable is False:
                    raise error
            finally:
                self._platform_recovering = False
        with self._platform_lock:
            self._platform_error = None
            self._restart_generation += 1
            self._ready = True

    def _command_plane_healthy_sync(self) -> bool:
        """One bounded, non-mutating probe; neither API READY nor bootstrap success is health."""
        done: concurrent.futures.Future[bool] = concurrent.futures.Future()

        def probe() -> None:
            try:
                # Bypass cwd, heartbeat, caches and command retries. A missing user cwd is not
                # a reason to reset a runtime; only a successful root probe restores health.
                self._last_probe_outcome = "no answer within 10s"
                chunks = list(self._iter_events(_HEALTH_PROBE, 10))
                stops = [chunk["contentStop"] for chunk in chunks if "contentStop" in chunk]
                self._last_probe_outcome = "stops=" + ",".join(
                    f"{stop.get('status')}/{stop.get('exitCode')}" if isinstance(stop, dict) else "?"
                    for stop in stops
                )
                output = "".join(chunk.get("contentDelta", {}).get("stdout", "") for chunk in chunks)
                zombies = output.removeprefix(_HEALTH_PROBE_ANSWER).strip()
                healthy = (
                    len(stops) == 1
                    and stops[0].get("exitCode") == 0
                    and stops[0].get("status") == "COMPLETED"
                    and output.startswith(_HEALTH_PROBE_SENTINEL)
                )
                # Diagnostic only: a missing or non-numeric count is ignored, never unhealthy.
                if healthy and zombies.isascii() and zombies.isdigit() and zombies.strip("0"):
                    logger.warning(
                        "sandbox has %s unreaped (zombie) processes %s; each can hold a command "
                        "slot, and with no slot left every command is exitCode -1",
                        zombies,
                        self._error_context(),
                    )
            except Exception as e:  # noqa: BLE001 — keep SDK/output details out of diagnostics
                self._last_probe_outcome = f"error={_error_code(e)}"
                healthy = False
            done.set_result(healthy)

        # The service timeout does not bound a stuck HTTP stream. A late response cannot change
        # readiness, and this harmless command's daemon must not hold up cancellation/shutdown.
        threading.Thread(target=probe, name="strandly-sandbox-health", daemon=True).start()
        try:
            healthy = done.result(timeout=10)
        except concurrent.futures.TimeoutError:
            healthy = False
        return healthy

    async def recover_platform_session(self, *, allow_ephemeral_loss: bool = False) -> None:
        """Explicit operator-approved stop/resume; never delete or replay a failed command.

        Locks, generations and the dispatch fence belong to this adapter object only. The
        operator must own the runtime/session/qualifier exclusively: another adapter or process
        on the same session is not fenced.
        """
        if not allow_ephemeral_loss:
            raise ValueError(
                "Recovery requires approval: StopRuntimeSession preserves /mnt/workspace, "
                "but may discard other files and background processes."
            )
        await asyncio.shield(asyncio.to_thread(self._recover_platform_session_sync))

    def _recover_platform_session_sync(self) -> None:
        """Serialize one approved reset, including callers from different event loops."""
        with self._first_use_lock, self._recovery_lock, self._ready_lock:
            error = self._platform_error
            if error is None:
                return
            if self._platform_reset_attempted:
                raise error
            with self._dispatch_fence() as quiet:
                if not quiet:
                    raise SandboxUnavailableError(
                        f"Recovery was not attempted {self._error_context()}: a command dispatch "
                        "is still in progress, so a stop now could be followed by that send. "
                        "Nothing was stopped; the approved recovery may be requested again."
                    )
                self._recover_fenced(error)

    def _recover_fenced(self, error: SandboxExecutionUncertainError) -> None:
        """The approved stop/resume itself. Caller holds the lifecycle locks and the fence."""
        self._platform_reset_attempted = True
        # The stop boundary. Whatever the stop's outcome, the container and everything set up in
        # it may be gone: invalidate it all now, so only a completed recovery restores readiness.
        with self._platform_lock:
            self._restart_generation += 1
            self._platform_reset_incomplete = True
            self._platform_needs_bootstrap = True
            self._ready = False
        self._first_use_ran = 0
        self._last_restart = time.monotonic()
        kwargs: dict[str, Any] = {
            "agentRuntimeArn": self.runtime_arn,
            "runtimeSessionId": self.session_id,
        }
        if self.qualifier:
            kwargs["qualifier"] = self.qualifier
        self._platform_recovering = True
        try:
            # Unlike best-effort legacy recovery, a failed/unknown stop is not a reset. The wake
            # client bounds this control call (one attempt, wake read timeout), not the
            # hour-long command read timeout.
            response = self._ensure_wake_client().stop_runtime_session(**kwargs)
            if response.get("statusCode") != 200:
                raise error
            self._wake()
            if not self._command_plane_healthy_sync():
                raise error
            # Stricter than a first wake: a bootstrap that was refused or reported nothing has
            # not redone the lost setup.
            if self._bootstrap().writable is not True:
                raise error
            with self._platform_lock:
                self._platform_error = None
                self._platform_reset_incomplete = False
                self._ready = True
        except Exception:  # noqa: BLE001 — no SDK messages, commands or output in diagnostics
            with self._platform_lock:
                self._platform_error = error
                self._ready = False
            raise error from None
        finally:
            self._platform_recovering = False
        self._add_notice(
            "[strandly-sandbox] operator-approved stop/resume completed. Persistent "
            "/mnt/workspace was retained; files outside it and background processes may "
            "be lost. The failed command was NOT replayed; check its effects before retrying."
        )

    @contextlib.contextmanager
    def _dispatch_fence(self) -> Iterator[bool]:
        """Exclusive dispatch boundary for a stop/reset. Caller holds ``_ready_lock``.

        While it is held no command send is admitted (:meth:`_admit_dispatch`). Yields whether
        every already-admitted send has returned from the command API; if not, the caller must
        not stop the runtime, because that request could still leave after the stop began.
        """
        with self._dispatch_cond:
            self._dispatch_fences += 1
            quiet = self._dispatch_cond.wait_for(
                lambda: not self._dispatching, _DISPATCH_QUIESCE_SECONDS
            )
        try:
            yield quiet
        finally:
            with self._dispatch_cond:
                self._dispatch_fences -= 1
                self._dispatch_cond.notify_all()

    def _admit_dispatch(
        self, *, lifecycle: bool, seen_generation: int | None, cancel: threading.Event | None = None
    ) -> int:
        """Admit one command send atomically with respect to a stop/reset; return its generation.

        ``lifecycle`` sends (bootstrap) are made by the ``_ready_lock`` holder, which is also
        whoever holds a fence. Every other send is refused while a fence is held or once the
        generation it was readied in (``seen_generation``) has ended, so a queued command cannot
        cross a reset; it counts as dispatching until :meth:`_dispatch_returned`.
        """
        with self._dispatch_cond:
            if not lifecycle and self._dispatch_fences and self._platform_error is None:
                # A best-effort stop holds the fence only for its own API call: wait it out.
                self._dispatch_cond.wait_for(
                    lambda: not self._dispatch_fences, _DISPATCH_QUIESCE_SECONDS
                )
                if cancel is not None and cancel.is_set():
                    raise _CommandAbandoned(f"command abandoned before it was sent {self._error_context()}")
            error = self._platform_error
            if error is not None and not (lifecycle and self._platform_recovering):
                raise error
            if not lifecycle:
                if self._dispatch_fences or seen_generation not in (None, self._restart_generation):
                    raise SandboxUnavailableError(
                        f"The sandbox was stopped or recovered before this command was sent "
                        f"{self._error_context()}. The command was NOT sent. Check the sandbox "
                        "state, then run it again if it is still needed."
                    )
                self._dispatching += 1
            return self._restart_generation

    def _dispatch_returned(self) -> None:
        """An admitted send has returned from (or failed in) the command API call."""
        with self._dispatch_cond:
            self._dispatching -= 1
            self._dispatch_cond.notify_all()

    def _restart_session_sync(self, reason: str, *, seen_generation: int | None = None) -> None:
        """Stop/resume the runtime (only persistent mounts survive). Blocking; worker thread.

        With ``seen_generation``, a restart that another command started after that generation was
        read is joined (this waits for it on the lock) rather than repeated.
        """
        with self._ready_lock:
            if self._platform_error is not None:
                raise self._platform_error
            if seen_generation is not None and self._restart_generation != seen_generation:
                return
            self._restart_generation += 1
            logger.warning("restarting sandbox session (%s) %s", reason, self._error_context())
            self._last_restart = time.monotonic()
            self._stop_session_sync()
            self._wake_and_bootstrap()
        self._add_notice(
            f"[strandly-sandbox] the sandbox was restarted ({reason}). Persistent files under "
            "/mnt/workspace are retained; background processes and files outside that mount "
            "may be lost."
        )

    def _may_restart(self) -> bool:
        """Whether an unhealthy-session restart is allowed now (rate limited per instance)."""
        return time.monotonic() - self._last_restart >= SANDBOX_RESTART_COOLDOWN_SECONDS

    def _note_storage_result(self, failed_on_storage: bool) -> None:
        """Track consecutive storage-failed commands; recover at the threshold. Blocking.

        Recovery = measure + probe (:meth:`_bootstrap`, which prunes when nearly full), then
        :meth:`_settle_workspace`. Only one
        caller recovers at a time; concurrent subagents hitting the same full disk skip it.
        """
        if not failed_on_storage:
            self._storage_strikes = 0
            return
        self._storage_strikes += 1
        if self._storage_strikes < SANDBOX_STORAGE_ERROR_THRESHOLD:
            return
        if not self._recovery_lock.acquire(blocking=False):
            return
        try:
            self._storage_strikes = 0
            logger.warning(
                "%d consecutive sandbox commands failed on storage; measuring and probing %s",
                SANDBOX_STORAGE_ERROR_THRESHOLD,
                self._error_context(),
            )
            with self._ready_lock:
                # Not forced: the strikes come from command output, which untrusted code controls
                # (a test printing "No space left on device"). The health script prunes only when
                # `df` says the workspace is really nearly full (never when it cannot measure), and a
                # failed write probe forces a prune in _settle_workspace.
                health = self._bootstrap()
                self._settle_workspace(health)
                self._ready = True
        except Exception as e:  # noqa: BLE001 — recovery is best-effort; the command result stands
            logger.warning("sandbox storage recovery failed (%s) %s", e, self._error_context())
        finally:
            self._recovery_lock.release()

    async def _prepare_once(self) -> None:
        """Await the one-time wake + git-configure, from whichever loop asks first.

        Split out of :meth:`_ensure_ready` on purpose: the background warm-up task runs *this*, not
        ``_ensure_ready``, because ``_ensure_ready`` awaits the warm-up task — a warm-up that went
        through it would await itself and hang the sandbox forever.
        """
        if self._ready:
            return
        await asyncio.to_thread(self._prepare_once_sync)

    async def _ensure_ready(self, cancel_signal: threading.Event | None = None) -> None:
        """Wake + bootstrap the session, then run the first-use hooks, before the first command.

        **The sandbox is lazy**: nothing in agent build or the first model call wakes it (docs/
        operations.md, "The sandbox"). The first shell/file operation of the run lands here and waits for
        the wake; concurrent first operations — the parent's, or a subagent's on the same
        shared object — share that one wake (``_ready_lock``) and the one hook phase
        (``_first_use_lock``). A failed wake raises to the operation that asked (a tool error),
        leaves ``_ready`` false, and the next operation tries again once the circuit allows it.

        ``cancel_signal`` set while waiting raises :class:`_CommandCancelled` at once; the wake and
        the hooks carry on in the background, so the next operation reuses them.
        """
        in_hook = _IN_FIRST_USE_HOOK.get()
        if self._ready and (in_hook or self._first_use_ran >= len(self._first_use_hooks)):
            return
        if cancel_signal is not None and cancel_signal.is_set():
            raise _CommandCancelled()
        task = self._warmup_task
        if task is not None and self._warmup_loop is asyncio.get_running_loop():
            # Never raises (warm-up is best-effort) — this only waits out any overlap still pending.
            # Skipped from a foreign loop, where awaiting another loop's task is the hazard this class
            # avoids everywhere else; ``_prepare_once`` then serializes on the threading lock instead,
            # which is exactly as correct and cannot hang.
            await _wait_or_cancel(task, cancel_signal)
        if cancel_signal is None:
            await self._prepare_once()
            if not in_hook and self._first_use_ran < len(self._first_use_hooks):
                await asyncio.to_thread(self._run_first_use_hooks_sync)
            return
        await self._in_background(
            lambda: self._ready_sync(run_hooks=not in_hook), cancel_signal
        )

    def _ready_sync(self, *, run_hooks: bool) -> None:
        """Wake + bootstrap, then (``run_hooks``) the pending first-use hooks. **Worker thread.**"""
        self._prepare_once_sync()
        if run_hooks and self._first_use_ran < len(self._first_use_hooks):
            self._run_first_use_hooks_sync()

    async def _in_background(self, work: Callable[[], None], cancel_signal: threading.Event) -> None:
        """Run blocking ``work`` on its own thread and wait for it until ``cancel_signal`` is set.

        A daemon thread rather than ``asyncio.to_thread``: a cancelled wait leaves ``work`` running,
        and neither the turn's event loop nor its executor may wait on it on the way out.
        """
        done: concurrent.futures.Future[None] = concurrent.futures.Future()
        context = contextvars.copy_context()

        def run() -> None:
            try:
                context.run(work)
            except BaseException as e:  # noqa: BLE001 — handed to the waiter
                done.set_exception(e)
            else:
                done.set_result(None)

        threading.Thread(target=run, name="strandly-sandbox-ready", daemon=True).start()
        waiter = asyncio.wrap_future(done)
        try:
            await _wait_or_cancel(waiter, cancel_signal)
            waiter.result()
        finally:
            if not waiter.done():
                waiter.add_done_callback(self._log_background_failure)

    def _log_background_failure(self, future: asyncio.Future[None]) -> None:
        error = None if future.cancelled() else future.exception()
        if error is not None:
            logger.warning(
                "sandbox wake failed after its command stopped waiting (%s: %s) %s; the next "
                "command tries again",
                type(error).__name__,
                _failure_detail(error, self._error_context()),
                self._error_context(),
            )

    @property
    def ready(self) -> bool:
        """Whether this object has woken its session yet. A plain read: never wakes anything."""
        return self._ready

    def add_first_use_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Run ``hook()`` once, after the wake and before the first command that needs the sandbox.

        The seam that keeps sandbox-needing setup off the agent-build path: the skills loader
        registers the built-in skills push here instead of awaiting it at build time, so a run that
        never touches the sandbox never starts a session (no instance, no EBS volume, no TTL
        schedule). A hook registered after the first use runs before the next command.

        The hook runs on a private event loop in a worker thread (cross-loop safe, see
        ``_ready_lock``), may itself use this sandbox's file and command API (those calls skip the
        hook phase and ignore the turn's cancel signal), and is best-effort: an exception is logged
        and the triggering command still runs. Each hook runs once, even if it fails; one that ends
        cancelled runs again before the next command. A command cancelled while the hooks run stops
        waiting for them; they finish in the background.
        """
        self._first_use_hooks.append(hook)

    def _run_first_use_hooks_sync(self) -> None:
        """Run every not-yet-finished first-use hook, in order. **Blocking; worker thread.**

        A hook that ends cancelled is not counted: the phase stops there and the hook runs again
        before the next command.
        """
        with self._first_use_lock:
            while self._first_use_ran < len(self._first_use_hooks):
                hook = self._first_use_hooks[self._first_use_ran]
                name = getattr(hook, "__qualname__", type(hook).__name__)
                token = _IN_FIRST_USE_HOOK.set(True)
                done = True
                try:
                    asyncio.run(_await(hook))
                except (asyncio.CancelledError, _CommandCancelled):
                    done = False
                    logger.info(
                        "sandbox first-use hook %s was cancelled; it runs again before the next "
                        "command %s",
                        name,
                        self._error_context(),
                    )
                except Exception as e:  # noqa: BLE001 — first-use setup is best-effort
                    logger.warning(
                        "sandbox first-use hook %s failed (%s: %s) %s",
                        name,
                        type(e).__name__,
                        _failure_detail(e, self._error_context()),
                        self._error_context(),
                    )
                finally:
                    _IN_FIRST_USE_HOOK.reset(token)
                    # Count the hook only once it has finished: callers check this counter without the
                    # lock, and must block on the lock until the hook phase is over.
                    if done:
                        self._first_use_ran += 1
                if not done:
                    return

    def warm_up(self) -> None:
        """Opt-in prefetch: wake + bootstrap the session in the background.

        **Not called by the agent factory any more** (docs/operations.md, "The sandbox"): an eager wake
        starts an instance, a 40 GiB EBS volume and a TTL schedule for every run, including the
        many that never run a command, so the sandbox now wakes at its first use instead. Kept for
        callers that know they are about to use the sandbox. Fire-and-forget and idempotent:
        :meth:`_ensure_ready` awaits whatever is still outstanding, and a failure here just means the
        first command pays for the wake itself.

        A no-op without a running event loop (a synchronous caller), and a no-op once ready.
        """
        if self._warmup_task is not None or self._ready:
            return

        async def _run() -> None:
            try:
                await self._prepare_once()
            except Exception as e:  # noqa: BLE001 — warm-up is best-effort; the lazy path covers it
                logger.warning(
                    "sandbox warm-up failed (%s); will retry on first use %s",
                    e,
                    self._error_context(),
                )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — skip; the first command wakes the session lazily.
            self._warmup_task = None
            self._warmup_loop = None
            return
        self._warmup_task = loop.create_task(_run())
        self._warmup_loop = loop

    async def close(self) -> None:
        """Release the sandbox. **Does not stop the session** — persistence is the feature.

        A sandbox session is meant to outlive one invocation: the next turn on the same GitHub issue
        should find its clone, its virtualenv and its build cache exactly where it left them, which
        is precisely what ``StopRuntimeSession`` would throw away. The session's own idle timeout
        (:data:`~strandly.core.constants.SANDBOX_IDLE_TIMEOUT_SECONDS`) reclaims the compute,
        and the session's EBS volume means even that does not lose the files.

        Use :meth:`stop_session` to end one deliberately (a poisoned workspace, or a test cleaning
        up after itself).
        """
        task = self._warmup_task
        if task is not None and self._warmup_loop is asyncio.get_running_loop():
            # Only from its own loop: awaiting another loop's task raises (or worse, waits forever),
            # and a warm-up that outlives the caller is harmless — it is idempotent and best-effort.
            await task
            self._warmup_task = None
            self._warmup_loop = None

    async def stop_session(self) -> None:
        """Explicitly terminate the sandbox session's compute (``StopRuntimeSession``).

        Best-effort: stopping is a cleanup, and a failure to clean up must not fail a turn. The
        session id stays valid — a later command simply starts its instance again, with the
        session's EBS volume (the filesystem) re-attached.
        """
        try:
            kwargs: dict[str, Any] = {
                "agentRuntimeArn": self.runtime_arn,
                "runtimeSessionId": self.session_id,
            }
            if self.qualifier:
                kwargs["qualifier"] = self.qualifier

            def stop() -> None:
                # The lifecycle lock and dispatch fence every other stop uses.
                with self._ready_lock, self._dispatch_fence() as quiet:
                    if not quiet:
                        raise RuntimeError("a command dispatch is still in progress; not stopped")
                    self._ensure_client().stop_runtime_session(**kwargs)

            await asyncio.to_thread(stop)
        except Exception as e:  # noqa: BLE001 — cleanup is best-effort
            logger.warning("stopping sandbox session failed (%s) %s", e, self._error_context())
        else:
            self._ready = False

    # ---- command execution ---------------------------------------------------------------------

    def _wrap(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        guard: bool = False,
    ) -> str:
        """Build the ``/bin/bash -c '<...>'`` string sent to the service.

        Composition order is load-bearing: ``cd`` first (so a relative path in the command means what
        the caller meant), then the pager pins, then the caller's ``env`` — so an explicit ``PATH`` or
        ``GIT_PAGER`` in ``env`` still wins over ours.

        Every command is a fresh bash process (the service is explicit about this: no shell history,
        no environment carried over), which is why the prefix is rebuilt every time rather than set
        up once at bootstrap.

        Two reliability pieces ride along:

        * the **heartbeat** (:func:`with_heartbeat`) wraps the whole ``cd && export && command``
          chain, so a failed ``cd`` still stops the command;
        * the **cache env** (:data:`SANDBOX_CACHE_ENV`) sends package caches and build output to
          :data:`SANDBOX_CACHE_ROOT` on the workspace volume — warm across invocations, and one
          directory the prune can empty. It precedes the caller's ``env``, so a caller can still
          override any of it.
        """
        target = cwd or self.workdir
        prefix = (
            f"cd {shlex.quote(target)} && export {SANDBOX_GIT_PAGER_ENV} && " + cache_env_prefix()
        )
        full = with_heartbeat(prefix + build_shell_env_prefix(env) + command)
        if guard:
            # Refuse before anything runs in a container without this adapter's setup, so the
            # command can be sent again once the setup is redone.
            full = (
                f'[ -e "$HOME/{_SETUP_SENTINEL}" ] || {{ printf "%s\\n" {self._fresh_marker} >&2; '
                f"exit {_FRESH_SESSION_EXIT}; }}\n{full}"
            )
        return f"{SANDBOX_SHELL} -c {shlex.quote(full)}"

    def _iter_events(
        self,
        command: str,
        timeout: int,
        *,
        on_returned: Callable[[], object] | None = None,
        on_accepted: Callable[[], object] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Invoke the command API and yield its result-stream events (blocking; worker thread only).

        Raises on a service exception delivered *as a stream member* — a failure must never look like
        a command that produced no output. ``on_returned`` runs once the API call has returned or
        raised, i.e. when the request can no longer leave the client later; ``on_accepted`` once it
        has returned, i.e. the service accepted the command.
        """
        trace = getattr(_SEND_TRACE, "current", None)
        if trace is not None:
            trace.started = time.monotonic()
        try:
            response = self._ensure_client().invoke_agent_runtime_command(
                **self._command_kwargs(command, timeout)
            )
        finally:
            if on_returned is not None:
                on_returned()
        if on_accepted is not None:
            on_accepted()
        if trace is not None:
            trace.request_id = _request_id(response)
        stream = response.get("stream") if isinstance(response, dict) else None
        if stream is None:
            # Name the response's *keys* (never values — output can be large/sensitive) so a missing
            # stream is diagnosable instead of a mystery.
            shape = sorted(map(str, response)) if isinstance(response, dict) else type(response).__name__
            raise _MalformedResponse(f"no result stream; response shape: {shape}")
        for event in stream:
            for key in _STREAM_EXCEPTION_KEYS:
                # Membership, not truthiness: an exception member whose payload is an EMPTY dict
                # must still raise, or the invoke returns no events and reads back as a successful
                # command with no output.
                if key in event:
                    detail = event[key].get("message") if isinstance(event[key], dict) else None
                    raise _StreamServiceError(
                        key[0].upper() + key[1:],
                        f"Sandbox command failed with {key} {self._error_context()}: "
                        f"{detail or '<the service returned no message>'}",
                    )
            chunk = event.get("chunk")
            if isinstance(chunk, dict):
                yield chunk

    def _run_command_sync(
        self, command: str, timeout: int, *, allow_wake: bool = True
    ) -> list[dict[str, Any]]:
        """Run a command to completion, collecting its chunks (blocking; worker thread only).

        Used by the bootstrap path, which has nobody to stream to. The streaming path
        (:meth:`execute_streaming`) drives :meth:`_iter_events` directly.
        """
        return list(self._retrying(command, timeout, allow_wake=allow_wake))

    def _retrying(
        self,
        command: str,
        timeout: int,
        *,
        allow_wake: bool,
        cancel: threading.Event | None = None,
        on_send: Callable[[bool], object] | None = None,
        seen_generation: int | None = None,
        guarded: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Yield a command's chunks, retrying the failures the service documents as transient.

        Rejection retries are bounded and **only before the first chunk is emitted** — retrying
        after output has already been handed to the caller would duplicate it, which is worse than
        the failure:

        * ``RetryableConflictException`` / ``ThrottlingException`` — back off and retry.
        * ``ResourceNotFoundException`` — the session idled out between invocations (or never
          existed). Wake it and retry once. This is the ordinary cost of persistence: sessions are
          *meant* to be reaped when idle, and the session's EBS volume comes back with them.
        Every other failure once the command API was entered (platform exit, 5xx, transport
        error, any error on the result stream, malformed or incomplete stream) has unknown
        execution effects, even before the first output chunk: it raises
        :class:`SandboxExecutionUncertainError` and never replays the command or automatically
        stops its runtime. Only :func:`_is_rejection` failures and local errors raised before
        the send stay ordinary errors.

        Each send is admitted through :meth:`_admit_dispatch` immediately before the API call.

        ``allow_wake=False`` (the bootstrap's own commands) disables the wake and the restart, so
        recovery never recurses into itself.

        A ``guarded`` command (:meth:`_wrap`) that its setup guard refused did not run: the lost
        setup is redone (:meth:`_redo_lost_setup`) and the command sent once more.
        """
        woken = not allow_wake
        refreshed = reconnected = False
        attempt = 0
        if allow_wake:
            self._check_circuit()
        while True:
            attempt += 1
            emitted = 0
            completed = False
            sent = False
            accepted: list[bool] = []
            held: dict[str, Any] | None = None
            spoke = fresh = False
            epoch = self._setup_epoch
            generation = self._restart_generation
            trace = _SEND_TRACE.current = _SendTrace()
            try:
                if cancel is not None and cancel.is_set():
                    # The caller gave up (its deadline, or a cancellation) while recovery ran:
                    # sending now would run a command whose result nobody reads — and which the
                    # model will likely re-run.
                    raise _CommandAbandoned(f"command abandoned before it was sent {self._error_context()}")
                if on_send is not None:
                    on_send(True)  # in flight: the caller's deadline runs
                # Nothing may come between admission and the API call: a stop/reset that begins
                # after this point waits for the call to return before it stops anything.
                generation = self._admit_dispatch(
                    lifecycle=not allow_wake, seen_generation=seen_generation, cancel=cancel
                )
                sent = True
                for chunk in self._iter_events(
                    command,
                    timeout,
                    on_returned=self._dispatch_returned if allow_wake else None,
                    on_accepted=functools.partial(accepted.append, True),
                ):
                    delta = chunk.get("contentDelta")
                    if delta is not None and not (
                        isinstance(delta, dict)
                        and isinstance(delta.get("stdout") or "", str)
                        and isinstance(delta.get("stderr") or "", str)
                    ):
                        raise self._record_platform_failure(
                            generation,
                            reason="command completion is unknown (invalid output event)",
                            trace=trace,
                        )
                    if (
                        guarded
                        and not spoke
                        and isinstance(delta, dict)
                        and not delta.get("stdout")
                        and (delta.get("stderr") or "").strip() == self._fresh_marker
                    ):
                        held = chunk  # the guard's refusal, unless more output follows
                        spoke = True
                        continue
                    if "contentStop" in chunk:
                        if cancel is not None and cancel.is_set():
                            raise _CommandCancelled()
                        stop = chunk["contentStop"]
                        if (
                            completed
                            or not isinstance(stop, dict)
                            or type(stop.get("exitCode")) is not int
                            or stop["exitCode"] < -1
                            or stop.get("status") not in ("COMPLETED", "TIMED_OUT")
                        ):
                            raise self._record_platform_failure(
                                generation,
                                reason="command completion is unknown (invalid terminal event)",
                                trace=trace,
                            )
                        completed = True
                        if held is not None and stop["exitCode"] == _FRESH_SESSION_EXIT:
                            fresh = stop["status"] == "COMPLETED"
                            if fresh:
                                break
                        if stop["exitCode"] == -1 and stop["status"] != "TIMED_OUT":
                            raise self._record_platform_failure(generation, trace=trace)
                    if held is not None:
                        emitted += 1
                        yield held
                        held = None
                    spoke = spoke or delta is not None
                    emitted += 1
                    yield chunk
                if fresh:
                    if refreshed:
                        raise SandboxUnavailableError(
                            f"The sandbox lost its setup again right after it was redone "
                            f"{self._error_context()}. The command did NOT run. Report that the "
                            "sandbox is unstable instead of retrying in a loop."
                        )
                    if on_send is not None:
                        on_send(False)  # recovering: not the command's time
                    self._redo_lost_setup(epoch)
                    refreshed = True
                    continue
                if not completed:
                    if cancel is not None and cancel.is_set():
                        raise _CommandCancelled()
                    raise self._record_platform_failure(
                        generation,
                        reason="closed the output stream without a completion event",
                        trace=trace,
                    )
                self._circuit_open_until = float("-inf")
                return
            except Exception as e:  # noqa: BLE001 — classified immediately below
                if not sent or fresh or isinstance(e, (_CommandCancelled, SandboxExecutionUncertainError)):
                    raise  # never sent, refused by its setup guard, or already classified
                code = _error_code(e)
                if emitted or accepted or not _is_rejection(e):
                    # Fail closed: the command may have run. Only the error's code/type goes in
                    # the diagnostic, never SDK text.
                    detail = str(e) if isinstance(e, _MalformedResponse) else code
                    raise self._record_platform_failure(
                        generation,
                        reason=f"command completion is unknown ({detail})",
                        trace=trace,
                        error=e,
                        response_started=bool(emitted or accepted),
                    ) from None
                if on_send is not None:
                    on_send(False)  # recovering: not the command's time
                if code in _SESSION_GONE_ERROR_CODES and not woken:
                    logger.info(
                        "sandbox session not active; waking and retrying %s", self._error_context()
                    )
                    woken = True
                    # Under the lifecycle lock: this wake must not race a stop/reset, and
                    # bootstrap sends are exempt from the fence only for that lock's holder.
                    with self._ready_lock:
                        if self._platform_error is not None:
                            raise self._platform_error from None
                        self._wake_and_bootstrap()
                    continue
                if code in _RETRYABLE_ERROR_CODES and attempt < SANDBOX_RETRY_MAX_ATTEMPTS:
                    delay = _backoff(attempt)
                    logger.warning(
                        "transient sandbox error %s on attempt %d/%d; retrying in %.1fs %s",
                        code,
                        attempt,
                        SANDBOX_RETRY_MAX_ATTEMPTS,
                        delay,
                        self._error_context(),
                    )
                    _sleep(delay)
                    continue
                if type(e).__name__ in _NEVER_CONNECTED_ERROR_NAMES and not reconnected:
                    # No connection was made, so no byte of the command was written: sending it
                    # again cannot run it twice. Once, at once (the pool opens a new connection).
                    reconnected = True
                    logger.warning(
                        "sandbox command was not sent (%s after %.0f ms, no connection was made); "
                        "sending it once more %s %s",
                        type(e).__name__,
                        (time.monotonic() - trace.started) * 1000,
                        _caller_detail(),
                        self._error_context(),
                    )
                    continue
                raise

    def _redo_lost_setup(self, epoch: int) -> None:
        """Wake and bootstrap again: the container came back without this adapter's setup."""
        with self._ready_lock:
            if self._platform_error is not None:
                raise self._platform_error from None
            if self._setup_epoch != epoch:
                if self._forged_epoch == epoch:
                    raise self._forged_refusal()
                return  # another command already redid it
            logger.warning(
                "sandbox session came back without its setup (stopped and resumed out of band); "
                "redoing it %s",
                self._error_context(),
            )
            self._ready = False
            if self._wake_and_bootstrap():
                # The sentinel was never lost, so the guard cannot have refused: the command itself
                # printed the guard's text and exit status. It may have run; never send it again.
                self._forged_epoch = epoch
                raise self._forged_refusal()
            if self._setup_epoch == epoch:
                self._setup_epoch = 0  # cannot be marked here: stop guarding rather than refuse all
                raise SandboxUnavailableError(
                    f"The sandbox's setup was redone but could not be recorded "
                    f"{self._error_context()}. The command did NOT run; run it again."
                )
        self._add_notice(
            "[strandly-sandbox] the sandbox was restarted outside this session and its setup was "
            "redone. Persistent /mnt/workspace was retained; background processes and files "
            "outside that mount (home, /tmp) were lost."
        )

    def _forged_refusal(self) -> SandboxExecutionUncertainError:
        return SandboxExecutionUncertainError(
            f"The command reported that the sandbox had lost its setup, but the setup was still in "
            f"place {self._error_context()}. The command may have run (it imitated the setup "
            "guard); it was not sent again. Check its effects before re-running it."
        )

    def _check_circuit(self) -> None:
        """Fail fast while a failed restart's circuit is open (see :meth:`_retrying`)."""
        remaining = self._circuit_open_until - time.monotonic()
        if remaining > 0:
            raise SandboxUnavailableError(
                f"The sandbox is unavailable {self._error_context()}: it failed moments ago (a "
                "wake that ran out of retries, a restart that did not help, or a workspace that "
                f"rejects writes). Not retrying for another {remaining:.0f}s. Do not retry in a loop: "
                "finish with what you have, or report that the sandbox is down."
            )

    async def execute_streaming(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        """Execute a shell command in the sandbox runtime, streaming stdout/stderr as it arrives.

        The service enforces ``timeout`` itself (clamped to its 1–3600s range) and reports the
        outcome in the terminal ``contentStop`` event. A ``TIMED_OUT`` status is therefore surfaced
        as a **result**, not an exception: the partial output of a command that ran out of time is
        usually the most useful thing the model can be told, so it keeps the output, gets the exit
        code, and gets an explicit note appended to stderr. The exception path
        (:class:`SandboxCommandTimeoutError`) is reserved for the genuinely pathological case where
        the HTTP/2 stream never terminates at all.

        Args:
            command: The shell command to execute.
            timeout: Service-enforced time limit in seconds. ``None`` uses
                :data:`~strandly.core.constants.SANDBOX_COMMAND_TIMEOUT_DEFAULT`.
            cwd: Working directory. ``None`` uses the persistent workspace volume.
            env: Environment variables, applied via ``export`` (keys validated, values quoted).
            **kwargs: Ignored; present for forward compatibility.

        Yields:
            :class:`~strands.sandbox.types.StreamChunk` per output chunk, then one
            :class:`~strands.sandbox.types.ExecutionResult`.

        Raises:
            ValueError: If the composed command exceeds the API's 64 KB limit, or ``env`` has an
                invalid key.
            SandboxCommandTimeoutError: If the service's output stream never completes.
        """
        budget = _clamp_timeout(timeout)
        guarded = self._setup_epoch > 0
        wrapped = self._wrap(command, cwd=cwd, env=env, guard=guarded)
        self._check_command_size(wrapped)
        # Before the wake, not only inside ``_retrying``: a failed wake leaves ``_ready`` false, and
        # without this every later call would re-wake a dead runtime.
        self._check_circuit()
        # A first-use hook's own commands run without the turn's cancel signal: see _IN_FIRST_USE_HOOK.
        cancel_signal = (
            None
            if _IN_FIRST_USE_HOOK.get()
            else (_COMMAND_CANCEL_SIGNAL.get() or self._cancel_signal)
        )
        try:
            # The generation this command is readied in; a send in any other one is refused.
            while True:
                generation = self._restart_generation
                await self._ensure_ready(cancel_signal)
                if generation == self._restart_generation:
                    break
            # The wake can finish just as the signal is set; a cancelled command is never sent.
            if cancel_signal is not None and cancel_signal.is_set():
                raise _CommandCancelled()
        except _CommandCancelled:
            yield ExecutionResult(exit_code=130, stdout="", stderr=CANCELLED_NOTE.lstrip("\n"))
            return
        # Every command (and every file op, which run as commands) slides the session's TTL, so a
        # long run that wakes once is not deleted mid-run. Non-blocking: see ``_bump_ttl``.
        self._bump_ttl()

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code = 0
        status: str | None = None

        try:
            async for chunk in self._stream_chunks(
                wrapped, budget, cancel_signal, generation, guarded=guarded
            ):
                delta = chunk.get("contentDelta")
                if isinstance(delta, dict):
                    if delta.get("stdout"):
                        stdout_parts.append(delta["stdout"])
                        yield StreamChunk(data=delta["stdout"], stream_type="stdout")
                    # The heartbeat (see `with_heartbeat`) is transport, not output: strip it,
                    # and drop a delta that was nothing but heartbeat.
                    err = strip_heartbeat(str(delta.get("stderr") or ""))
                    if err:
                        stderr_parts.append(err)
                        yield StreamChunk(data=err, stream_type="stderr")
                stop = chunk.get("contentStop")
                if isinstance(stop, dict):
                    exit_code = stop.get("exitCode", exit_code)
                    status = stop.get("status")
        except SandboxExecutionUncertainError as e:
            raise e.with_output(
                stdout="".join(stdout_parts), stderr=strip_heartbeat("".join(stderr_parts))
            ) from None
        except SandboxCommandTimeoutError as e:
            # Hand the SDK `shell` tool what streamed before the cut: it reports `e.stdout` /
            # `e.stderr` to the model as the partial result.
            raise e.with_output(
                stdout="".join(stdout_parts), stderr="".join(stderr_parts)
            ) from e.__cause__
        except _CommandCancelled:
            yield ExecutionResult(
                exit_code=130,
                stdout="".join(stdout_parts),
                stderr="".join(stderr_parts) + CANCELLED_NOTE,
            )
            return

        stderr = "".join(stderr_parts)
        if status == "TIMED_OUT":
            # Say so in the output the model reads, and make sure the exit code is non-zero even if
            # the service reported 0 — a timed-out command has NOT succeeded, and a tool result that
            # looks successful is the failure mode that matters.
            note = (
                f"\n[strandly-sandbox] command timed out after {budget}s and was terminated. "
                "Re-run with a larger timeout, or split it into smaller steps."
            )
            stderr += note
            if exit_code in (0, -1):
                exit_code = 124  # the conventional `timeout(1)` exit status
        if status is None:
            raise self._record_platform_failure(
                self._restart_generation, reason="closed the output stream without a completion event"
            ).with_output(stdout="".join(stdout_parts), stderr=stderr)

        marker = _storage_failure(stderr) if exit_code != 0 else None
        if marker:
            stderr += "\n" + storage_error_advice(marker, self._capacity_kb)
        if marker or self._storage_strikes:
            # Off the loop: at the threshold this prunes, probes and may restart.
            await asyncio.to_thread(self._note_storage_result, bool(marker))

        yield ExecutionResult(exit_code=exit_code, stdout="".join(stdout_parts), stderr=stderr)

    def bind_cancel_signal(self, signal: threading.Event | None) -> None:
        """Commands started from now on stop waiting once ``signal`` is set; ``None`` unbinds.

        A :func:`command_cancel_signal` set in the issuing context takes precedence.
        """
        self._cancel_signal = signal

    async def _stream_chunks(
        self,
        wrapped: str,
        budget: int,
        cancel_signal: threading.Event | None = None,
        seen_generation: int | None = None,
        *,
        guarded: bool = False,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Bridge the blocking boto3 event stream onto the event loop, chunk by chunk.

        boto3's event stream is a *blocking* iterator, so it cannot be consumed on the event loop
        and cannot be handed to ``asyncio.to_thread`` as a unit without giving up streaming (which
        is the whole reason to use this API). So: a worker thread iterates
        and hands each chunk over with ``call_soon_threadsafe``, and this generator drains the queue.
        An exception raised in the thread is passed through the same queue and re-raised here, so the
        caller sees the real error with its real traceback context rather than a dropped task.

        The deadline is ``budget`` + :data:`SANDBOX_TRANSPORT_GRACE_SECONDS`, i.e. deliberately
        *later* than the service's own enforcement, so the service's structured ``TIMED_OUT`` answer
        wins in every normal case and this only fires when the stream itself is stuck. It starts
        when the command is actually sent (and restarts on a re-send), not before: wake, restart
        and retry delays are bounded by their own budgets and must not eat the command's time.
        When this generator exits early for any reason, the pump is told to never send the
        command afterwards. ``cancel_signal`` set ends the wait with :class:`_CommandCancelled`; the
        remote command is left to finish or time out.

        Nothing here blocks the event loop, and that is load-bearing rather than incidental: the queue
        is awaited (``await queue.get()``), never ``get()``-ed; the worker is never ``join()``-ed and
        its future is never ``result()``-ed on the loop thread; and the queue is created *per call* on
        the consumer's own loop, so unlike an attribute on this shared object it can never be touched
        by a second loop. Any of those three would reintroduce the "something to wait on while the
        loop is frozen" ingredient of the v30–v32 deadlock (see :meth:`_prepare_once_sync`).
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any] | BaseException | object | None] = asyncio.Queue()
        cancel = threading.Event()
        # Queue markers: the command was (re)sent — (re)start the deadline; or it failed before any
        # output and recovery is running — stop it.
        sent, recovering = object(), object()

        def pump() -> None:
            try:
                with self._command_slot():
                    for chunk in self._retrying(
                        wrapped,
                        budget,
                        allow_wake=True,
                        cancel=cancel,
                        on_send=lambda flying: loop.call_soon_threadsafe(
                            queue.put_nowait, sent if flying else recovering
                        ),
                        seen_generation=seen_generation,
                        guarded=guarded,
                    ):
                        loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except BaseException as e:  # noqa: BLE001 — forwarded to the consumer, never swallowed
                loop.call_soon_threadsafe(queue.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        # Like ``asyncio.to_thread`` (which copies the caller's context), on the pumps' own pool.
        pump_task = asyncio.ensure_future(
            loop.run_in_executor(self._command_executor(), contextvars.copy_context().run, pump)
        )
        allowance = budget + SANDBOX_TRANSPORT_GRACE_SECONDS
        deadline: float | None = None  # not running until the command is sent
        try:
            while True:
                if cancel_signal is not None and cancel_signal.is_set():
                    raise _CommandCancelled()
                remaining = None if deadline is None else deadline - loop.time()
                if remaining is not None and remaining <= 0:
                    raise SandboxCommandTimeoutError(timeout=allowance, context=self._error_context())
                wait = remaining
                if cancel_signal is not None:
                    wait = _CANCEL_POLL_SECONDS if wait is None else min(wait, _CANCEL_POLL_SECONDS)
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=wait)
                except asyncio.TimeoutError as e:
                    if cancel_signal is not None:
                        continue  # re-checks the cancel and the deadline
                    raise SandboxCommandTimeoutError(
                        timeout=allowance, context=self._error_context()
                    ) from e
                if item is sent or item is recovering:
                    deadline = loop.time() + allowance if item is sent else None
                    continue
                if item is None:
                    return
                if isinstance(item, BaseException):
                    raise item
                if isinstance(item, dict):
                    yield item
        finally:
            cancel.set()
            # The worker cannot be cancelled (a blocking boto3 read), but it must not be left
            # unobserved either: swallow its outcome so an abandoned pump doesn't surface as
            # "exception was never retrieved".
            pump_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    @contextlib.contextmanager
    def _command_slot(self) -> Iterator[None]:
        """Hold one of :data:`SANDBOX_MAX_CONCURRENT_COMMANDS` for as long as the command's pump runs
        (a cancelled caller's command may still be running remotely)."""
        with self._commands_lock:
            if self._commands_running >= SANDBOX_MAX_CONCURRENT_COMMANDS:
                raise SandboxUnavailableError(
                    f"The sandbox already has {SANDBOX_MAX_CONCURRENT_COMMANDS} commands running "
                    f"{self._error_context()}, the most one session may run at once. The command "
                    "was NOT sent. Wait for running commands to finish (or stop background ones), "
                    "then run it again."
                )
            self._commands_running += 1
        try:
            yield
        finally:
            with self._commands_lock:
                self._commands_running -= 1

    def _command_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        """The pumps' thread pool, sized to :data:`SANDBOX_MAX_CONCURRENT_COMMANDS` (plus headroom).

        Threads start lazily and exit when the adapter (and so the pool) is collected.
        """
        with self._commands_lock:
            if self._command_pool is None:
                self._command_pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=SANDBOX_MAX_CONCURRENT_COMMANDS + _COMMAND_POOL_HEADROOM,
                    thread_name_prefix="strandly-sandbox-command",
                )
            return self._command_pool

    def _check_command_size(self, wrapped: str) -> None:
        """Refuse a command over the API's 64 KB limit, with an actionable message.

        Sending it would earn an opaque ``ValidationException``; the model can act on "split it" but
        not on that.
        """
        size = len(wrapped.encode())
        if size > SANDBOX_COMMAND_MAX_BYTES:
            raise ValueError(
                f"Sandbox command is {size} bytes, over the {SANDBOX_COMMAND_MAX_BYTES}-byte limit "
                f"of the AgentCore command API {self._error_context()}. Write the script to a file "
                "in the sandbox (in chunks) and run that file instead."
            )

    # ---- file operations -----------------------------------------------------------------------

    async def read_file(self, path: str, **kwargs: Any) -> bytes:
        """Read raw bytes in checksummed slices, so a large file cannot arrive corrupt.

        The SDK default is one ``base64 < file`` command decoded whole. In production every file
        over ~100 KB intermittently failed with "Failed to decode base64 contents of file" (the SDK's
        largest source files — exactly the ones the agent edits most), under ``/tmp`` as well as the
        mount: a long single-command output stream is what breaks, not the filesystem.

        So: one command reports the file's length and sha256 *and* carries its first
        :data:`SANDBOX_READ_CHUNK_BYTES` (small files — most reads — stay one round trip); each
        further slice is its own command carrying its own sha256, fetched a few at a time
        (:data:`SANDBOX_READ_CONCURRENCY`) and retried on mismatch. The assembled bytes must match
        the whole-file sha256; if the file changed mid-read, the whole read is retried.

        Raises:
            FileNotFoundError: The path does not exist or is not a readable file (SDK semantics).
            OSError: The content could not be read intact after :data:`SANDBOX_READ_MAX_ATTEMPTS`.
        """
        quoted = shlex.quote(path)
        chunk = SANDBOX_READ_CHUNK_BYTES
        last_problem = ""
        for _attempt in range(SANDBOX_READ_MAX_ATTEMPTS):
            head = await self.execute(
                f"set -o pipefail; wc -c < {quoted} && sha256sum < {quoted} | cut -c1-64 "
                f"&& head -c {chunk} < {quoted} | base64 -w0"
            )
            if head.exit_code != 0:
                if head.exit_code > 0 and "closed the output stream" not in head.stderr:
                    raise FileNotFoundError(head.stderr.strip() or f"Failed to read file: {path}")
                last_problem = head.stderr.strip()[-300:]
                continue
            lines = head.stdout.split("\n", 2)
            try:
                size = int(lines[0].strip())
                digest = lines[1].strip()
                first = base64.b64decode("".join((lines[2] if len(lines) > 2 else "").split()))
            except (ValueError, IndexError):
                last_problem = "the size/checksum header was malformed"
                continue
            if size > SANDBOX_READ_MAX_BYTES:
                raise SandboxFileTooLargeError(path, size)
            parts = [first]
            if size > chunk:
                count = (size + chunk - 1) // chunk
                gate = asyncio.Semaphore(SANDBOX_READ_CONCURRENCY)

                async def fetch(index: int, *, total: int = size, gate: asyncio.Semaphore = gate) -> bytes:
                    async with gate:
                        return await self._read_slice(path, index, total)

                parts += await asyncio.gather(*(fetch(i) for i in range(1, count)))
            data = b"".join(parts)
            if len(data) == size and hashlib.sha256(data).hexdigest() == digest:
                return data
            last_problem = (
                f"got {len(data)} bytes for a {size}-byte file, or a checksum mismatch "
                "(was it modified while being read?)"
            )
        raise OSError(
            f"Failed to read {path} intact after {SANDBOX_READ_MAX_ATTEMPTS} attempts "
            f"{self._error_context()}: {last_problem or 'unknown error'}. Read it in pieces with "
            "`sed -n` or `head -c`/`tail -c` instead."
        )

    async def _read_slice(self, path: str, index: int, size: int) -> bytes:
        """Slice ``index`` of ``path`` (see :meth:`read_file`), verified by length and sha256."""
        quoted = shlex.quote(path)
        chunk = SANDBOX_READ_CHUNK_BYTES
        expected = min(chunk, size - index * chunk)
        dd = f"dd if={quoted} bs={chunk} skip={index} count=1 iflag=fullblock status=none"
        problem = ""
        for _attempt in range(SANDBOX_READ_MAX_ATTEMPTS):
            result = await self.execute(
                f"set -o pipefail; {dd} | sha256sum | cut -c1-64 && {dd} | base64 -w0"
            )
            if result.exit_code != 0:
                problem = result.stderr.strip()[-300:] or f"exit code {result.exit_code}"
                continue
            digest, _, encoded = result.stdout.partition("\n")
            try:
                data = base64.b64decode("".join(encoded.split()))
            except ValueError:
                problem = "the slice was not valid base64"
                continue
            if len(data) == expected and hashlib.sha256(data).hexdigest() == digest.strip():
                return data
            problem = f"slice {index} failed its checksum ({len(data)}/{expected} bytes)"
        raise OSError(
            f"Failed to read {path} intact {self._error_context()}: {problem}. Read it in pieces "
            "with `sed -n` or `head -c`/`tail -c` instead."
        )

    async def write_file(self, path: str, content: bytes, **kwargs: Any) -> None:
        """Write raw bytes, chunked so a large file cannot exceed the 64 KB command limit.

        The inherited implementation base64-encodes the whole payload into a single heredoc command,
        which caps writes at ~48 KB and fails with an opaque ``ValidationException`` above it, and
        the agent's ``write`` tool and skill pushes do send larger files.

        So: append the base64 text to a scratch file in
        :data:`SANDBOX_WRITE_CHUNK_CHARS`-sized pieces, then decode it into place atomically-enough
        (``base64 -d`` into a temp file, then ``mv``). Base64 is alphanumeric plus ``+/=``, so each
        chunk is safe to pass as a single-quoted ``printf`` argument.

        Args:
            path: Destination path (absolute paths are ordinary paths here).
            content: The bytes to write.
            **kwargs: Ignored; present for forward compatibility.

        Raises:
            OSError: If any step of the write fails, naming the step and the service's own message.
        """
        encoded = base64.b64encode(content).decode("ascii")
        quoted = shlex.quote(path)
        # Scratch files live beside the destination so the final `mv` is a same-filesystem rename.
        scratch = shlex.quote(f"{path}.strandly-{uuid.uuid4().hex[:12]}.b64")
        staged = shlex.quote(f"{path}.strandly-{uuid.uuid4().hex[:12]}.part")

        chunks = [
            encoded[i : i + SANDBOX_WRITE_CHUNK_CHARS]
            for i in range(0, len(encoded), SANDBOX_WRITE_CHUNK_CHARS)
        ] or [""]

        try:
            await self._write_step(
                path,
                "create the parent directory",
                f'mkdir -p "$(dirname {quoted})" && : > {scratch}',
            )
            for chunk in chunks:
                await self._write_step(
                    path, "append file content", f"printf '%s' {shlex.quote(chunk)} >> {scratch}"
                )
            await self._write_step(
                path,
                "decode file content",
                f"base64 -d < {scratch} > {staged} && mv {staged} {quoted}",
            )
        finally:
            # Never leave scratch behind, even on failure — a half-written .b64 next to a source file
            # would confuse the agent (and any linter it runs).
            #
            # Guarded, because an exception raised in a `finally` REPLACES the exception being
            # propagated. Unguarded, a cleanup failure would swallow the OSError that names the step
            # and the service's own message and substitute its own, possibly with an empty `str()`
            # that the SDK `shell` tool renders as a message-less `Error: RuntimeError - `.
            # A failed cleanup is a stray temp file; a masked write error is an unactionable turn.
            try:
                await self.execute(f"rm -f {scratch} {staged}")
            except Exception as e:  # noqa: BLE001 — must never mask the real failure above
                logger.warning(
                    "could not remove sandbox write scratch files for %s (%s: %s) %s; the write "
                    "outcome above is the one that matters",
                    path,
                    type(e).__name__,
                    e,
                    self._error_context(),
                )

    async def _write_step(self, path: str, what: str, command: str) -> None:
        """Run one step of :meth:`write_file`, raising ``OSError`` with real detail on failure."""
        result = await self.execute(command)
        if result.exit_code != 0:
            raise OSError(
                f"Failed to {what} for {path} {self._error_context()}: "
                f"{result.stderr.strip() or f'exit code {result.exit_code}'}"
            )

    # ---- tools ---------------------------------------------------------------------------------

    def get_tools(self) -> list[AgentTool]:
        """Vend **no** tools: the agent's own ``shell``/``read``/``write``/``edit`` already run here.

        ``Agent.__init__`` registers every sandbox-vended tool, so an agent built with this sandbox
        outside ``create_harness`` (the goal critic, for one) would get duplicate exec and file
        tools. Tools that take the agent's sandbox at call time are how a caller runs things here.
        """
        return []
