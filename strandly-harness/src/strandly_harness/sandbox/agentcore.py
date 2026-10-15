"""Amazon Bedrock AgentCore sandbox backed by a managed Code Interpreter session.

Implements the Strands :class:`~strands.sandbox.base.Sandbox` interface by mapping
every operation to a *native* AgentCore Code Interpreter tool
(``executeCommand``, ``executeCode``, ``readFiles``, ``writeFiles``,
``removeFiles``, ``listFiles``) instead of faking file and code I/O through the
shell. This makes file transfers binary-safe and lets code execution return rich
output artifacts (images, charts) as :class:`~strands.sandbox.types.OutputFile`.

Python port of the TypeScript reference
`aws/bedrock-agentcore-sdk-typescript#193 <https://github.com/aws/bedrock-agentcore-sdk-typescript/pull/193>`_.

Idiomatic divergences from the TS oracle (and why):

- **Session lifecycle.** The TS ``AgentCoreSandbox`` is pure I/O: the *caller*
  owns the session (start it, pass ``identifier`` + ``sessionId``, stop it). In
  this harness the *harness* owns the sandbox (``build_sandbox(config)`` injects
  one shared instance into every tool), so requiring users to pre-create a
  session would be awkward. This port supports **both**: pass an existing
  ``session_id`` to attach (faithful to TS, and we never stop a session we did
  not start), or omit it to **lazily start** a managed session on first use.
  :meth:`close` stops a session only if we started it.
- **Sync client in an async interface.** The ``bedrock-agentcore`` Code
  Interpreter client is synchronous (boto3). The :class:`Sandbox` primitives are
  async generators, so each blocking ``invoke`` is run via
  :func:`asyncio.to_thread` to avoid blocking the event loop. AgentCore returns
  *result events* (not a live byte stream), so output is collected in the worker
  thread and then yielded as chunks \u2014 matching the TS note that chunks arrive
  "typically after the operation completes".
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from typing import TYPE_CHECKING, Any

from strands.sandbox.base import Sandbox
from strands.sandbox.posix_shell import build_shell_env_prefix
from strands.sandbox.types import ExecutionResult, FileInfo, OutputFile, StreamChunk

from strandly_harness.core.constants import (
    SANDBOX_BOOTSTRAP_PACKAGES,
    SANDBOX_GIT_BIN,
    SANDBOX_GIT_COMMIT_EMAIL,
    SANDBOX_GIT_COMMIT_NAME,
    SANDBOX_GIT_PAGER_ENV,
    SANDBOX_GIT_PREFIX,
    SANDBOX_MICROMAMBA_URL,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

    from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter
    from strands.types.tools import AgentTool

logger = logging.getLogger(__name__)

#: The system Code Interpreter identifier used when none is configured.
DEFAULT_IDENTIFIER = "aws.codeinterpreter.v1"

#: Maps common interpreter names to the three languages AgentCore accepts.
#: The :class:`Sandbox` interface takes a free-form ``language`` (the shell
#: backends treat it as an interpreter binary like ``python3``/``node``), so
#: aliases are normalized to keep code execution portable across backends.
_LANGUAGE_ALIASES = {
    "python": "python",
    "python3": "python",
    "py": "python",
    "javascript": "javascript",
    "js": "javascript",
    "node": "javascript",
    "nodejs": "javascript",
    "typescript": "typescript",
    "ts": "typescript",
}


def _to_programming_language(language: str) -> str:
    """Resolve a free-form language to an AgentCore programming language, or raise."""
    mapped = _LANGUAGE_ALIASES.get(language.lower())
    if mapped is None:
        raise ValueError(
            f'AgentCore code interpreter does not support language "{language}" '
            "(supported: python, javascript, typescript)"
        )
    return mapped


def _resolve_region(region: str | None) -> str:
    """Resolve an AWS region from the argument, env, or boto3 session config."""
    if region:
        return region
    env_region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if env_region:
        return env_region
    try:
        import boto3

        session_region = boto3.Session().region_name
        if session_region:
            return session_region
    except Exception:  # pragma: no cover - boto3 import/session edge cases
        pass
    return "us-west-2"


def _as_bytes(value: Any) -> bytes:
    """Coerce a blob/text value (bytes, bytearray, str) to ``bytes``."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode()
    return bytes(value)


def _to_output_file(block: dict[str, Any]) -> OutputFile | None:
    """Map a non-text content block (image or binary resource) to an :class:`OutputFile`."""
    btype = block.get("type")
    if btype == "image" and block.get("data") is not None:
        return OutputFile(
            name=block.get("name") or "image",
            content=_as_bytes(block["data"]),
            mime_type=block.get("mimeType") or "application/octet-stream",
        )
    resource = block.get("resource") or {}
    if btype == "resource" and resource.get("blob") is not None:
        return OutputFile(
            name=block.get("name") or resource.get("uri") or "output",
            content=_as_bytes(resource["blob"]),
            mime_type=resource.get("mimeType") or "application/octet-stream",
        )
    return None


def _collect_text(content: list[dict[str, Any]]) -> str:
    """Concatenate the text content blocks of a result, used for surfacing errors."""
    return "\n".join(b["text"] for b in content if b.get("type") == "text" and b.get("text"))


def _git_bootstrap_script(packages: tuple[str, ...] = SANDBOX_BOOTSTRAP_PACKAGES) -> str:
    """A rootless, idempotent shell script that installs ``packages`` (git) into ``$HOME``.

    The Code Interpreter image ships no ``git`` and runs as a non-root user with no passwordless
    sudo, so ``dnf install`` is unavailable. Instead we fetch a static ``micromamba`` binary and use
    it to create a conda-forge env under :data:`SANDBOX_GIT_PREFIX` — real, full git (push works),
    with all its shared-library deps, no root required.

    Idempotent: if every package's binary already resolves on ``PATH`` the script exits early, so an
    instance that already bootstrapped (or an adopted session whose ``$HOME`` still holds the env)
    pays nothing. The script writes only under ``$HOME`` and ``/tmp`` and is safe to re-run.
    """
    # Probe each package's primary binary; if all present, skip. (We only install git today, but
    # keep this general so adding a package doesn't silently skip the install.)
    checks = " && ".join(f"command -v {shlex.quote(p)} >/dev/null 2>&1" for p in packages)
    pkg_args = " ".join(shlex.quote(p) for p in packages)
    return f"""
set -e
if {checks}; then echo "strandly-bootstrap: tools already present"; exit 0; fi
cd /tmp
curl -fsSL {shlex.quote(SANDBOX_MICROMAMBA_URL)} -o /tmp/_mm.tar.bz2
python3 -c "import tarfile; t=tarfile.open('/tmp/_mm.tar.bz2'); m=[x for x in t.getmembers() if x.name.endswith('/micromamba')][0]; t.extract(m,'/tmp/_mmx')"
chmod +x /tmp/_mmx/bin/micromamba
export MAMBA_ROOT_PREFIX=$HOME/.mm
/tmp/_mmx/bin/micromamba create -y -p {SANDBOX_GIT_PREFIX} -c conda-forge {pkg_args} >/dev/null 2>&1
echo "strandly-bootstrap: installed {pkg_args} -> {SANDBOX_GIT_PREFIX}"
"""


def _git_credentials_script(token: str) -> str:
    """A shell script that bootstraps the sandbox's git client with ``token`` for github.com.

    Writes the standard git credential store (``~/.git-credentials`` + ``credential.helper store``)
    so every native ``git clone``/``fetch``/``push`` against github.com authenticates as the same
    identity the ``use_github`` tool writes with — no per-command token plumbing, and the agent can
    use git exactly like a developer would (private clones, pushes, rebases). Also sets a commit
    identity, without which ``git commit`` fails on the bare CI image.

    Deliberate choices:

    - **Credential store, not ``url.insteadOf``** — an embedded-token URL rewrite leaks the token
      into every ``git config -l`` / error message; the credentials file is a single ``0600`` file
      that a future rotation just overwrites.
    - **``x-access-token`` basic-auth username** — works uniformly for classic PATs, fine-grained
      PATs, and GitHub App installation tokens, so swapping the token type later (the plan is
      GitHub App short-lived tokens) needs no change here.
    - **The token never appears in the script's output** — only in the heredoc-quoted body sent to
      ``executeCommand``. Callers must not log this script (see :meth:`_bootstrap_session`).
    - **Idempotent** — rewrites the credential file and config unconditionally; safe to re-run and
      naturally handles token rotation on a fresh session.
    """
    return f"""
set -e
export PATH={SANDBOX_GIT_BIN}:$PATH
if ! command -v git >/dev/null 2>&1; then echo "strandly-bootstrap: git missing; skipping credentials"; exit 0; fi
umask 077
printf 'https://x-access-token:%s@github.com\\n' {shlex.quote(token)} > "$HOME/.git-credentials"
git config --global credential.helper store
git config --global user.name {shlex.quote(SANDBOX_GIT_COMMIT_NAME)}
git config --global user.email {shlex.quote(SANDBOX_GIT_COMMIT_EMAIL)}
echo "strandly-bootstrap: git credentials configured"
"""


def _block_is_dir(block: dict[str, Any]) -> bool | None:
    """Resolve whether a ``listFiles`` resource block is a directory, or ``None`` if unknown.

    AgentCore marks directory entries with ``description: "Directory"`` (and file entries carry a
    ``mimeType``); it does not use a trailing slash. We key off the explicit description and treat a
    present ``mimeType`` as a strong file signal, returning ``None`` only when neither is present so
    the name-based fallback in :func:`_to_file_info` can still apply.
    """
    description = (block.get("description") or "").strip().lower()
    if description == "directory":
        return True
    resource = block.get("resource") or {}
    if description in ("file", "regular file") or resource.get("mimeType") or block.get("mimeType"):
        return False
    return None


def _to_file_info(
    raw_name: str, size: int | None = None, *, is_dir: bool | None = None
) -> FileInfo:
    """Build a :class:`FileInfo`, normalizing the name and resolving ``is_dir``.

    ``is_dir`` is taken from the caller when known (AgentCore's ``listFiles`` marks a directory
    entry explicitly — ``description: "Directory"`` — rather than with a trailing slash, so the
    caller passes that through). When the caller can't tell, we fall back to the ``ls -ap``
    trailing-slash convention the shell backends use. A trailing slash is always stripped from the
    reported name, and a ``file://`` scheme (resource URIs) is stripped so the name is a plain path.

    Historically this only used the trailing-slash heuristic; since ``listFiles`` never emits one,
    every directory was misreported as a file — which silently broke directory-tree consumers like
    the skills loader (it filters on ``is_dir``). The explicit flag is the fix.
    """
    name = raw_name
    if name.startswith("file://"):
        name = name[len("file://") :]
    if name.endswith("/"):
        name = name[:-1]
        if is_dir is None:
            is_dir = True
    return FileInfo(name=name, is_dir=is_dir, size=size)


class AgentCoreSandbox(Sandbox):
    """Execute commands and code in an Amazon Bedrock AgentCore Code Interpreter session.

    ``env`` and ``cwd`` are applied to :meth:`execute_streaming` by prepending a
    shell ``cd`` / ``export`` prefix; they are **not** applied to
    :meth:`execute_code_streaming`, which runs in a language kernel with no
    surrounding shell (set them from within the code itself, e.g. ``os.environ`` /
    ``os.chdir`` in Python).

    Example:
        Lazily start a managed session (harness-owned lifecycle)::

            sandbox = AgentCoreSandbox(region="us-west-2")
            result = await sandbox.execute("echo hello")
            print(result.stdout)
            await sandbox.close()

        Attach to a session the caller already started (faithful to the TS port)::

            sandbox = AgentCoreSandbox(
                identifier="aws.codeinterpreter.v1",
                session_id="<existing-session-id>",
                region="us-west-2",
            )
    """

    def __init__(
        self,
        *,
        region: str | None = None,
        identifier: str = DEFAULT_IDENTIFIER,
        session_id: str | None = None,
        session_timeout_seconds: int | None = None,
        bootstrap_git: bool = True,
        github_token: str | None = None,
        client: CodeInterpreter | None = None,
    ) -> None:
        """Initialize the AgentCore sandbox.

        Args:
            region: AWS region. Resolved from the argument, then ``AWS_REGION`` /
                ``AWS_DEFAULT_REGION``, then the boto3 session, then ``us-west-2``.
                Used only when ``client`` is not provided.
            identifier: The Code Interpreter identifier (system ``aws.codeinterpreter.v1``
                or a custom interpreter id).
            session_id: An existing session id to attach to. When provided, the
                sandbox never starts or stops the session (the caller owns it).
                When omitted, a managed session is started lazily on first use and
                stopped by :meth:`close`.
            session_timeout_seconds: Session timeout passed when starting a managed
                session. ``None`` uses the service default (15 minutes).
            bootstrap_git: When ``True`` (default), a fresh managed session installs git into
                ``$HOME`` on start (the image has none — see :func:`_git_bootstrap_script`) and
                every command gets ``$HOME/.gitenv/bin`` prepended to ``PATH``. Fail-open: a failed
                install logs a warning and leaves the sandbox usable without git. Set ``False`` to
                skip (e.g. a caller-owned session, or a backend that already ships git).
            github_token: When set (and ``bootstrap_git`` is on), a fresh managed session also
                bootstraps the git client with this token for github.com (credential store +
                commit identity — see :func:`_git_credentials_script`), so native
                ``git clone``/``push`` and any tool that shells out to git authenticate without
                per-command plumbing. This deliberately places the token *inside* the sandbox:
                agent-executed code can read it, and native pushes bypass the ``use_github``
                guardrails — the token's own scope becomes the effective write policy, so prefer
                a short-lived, repo-scoped token (e.g. a GitHub App installation token) over a
                broad PAT. ``None`` (default) keeps the sandbox credential-free.
            client: A pre-built ``CodeInterpreter`` client. When omitted, one is
                constructed lazily from ``region``.
        """
        self.identifier = identifier
        self.session_timeout_seconds = session_timeout_seconds
        self.bootstrap_git = bootstrap_git
        self._github_token = github_token
        self._region = _resolve_region(region)
        self._client = client
        # We own (and therefore stop) only sessions we start ourselves: no explicit
        # session_id AND (no injected client, or an injected client with no live session).
        self._owns_session = session_id is None and (client is None or client.session_id is None)
        if client is not None:
            client.identifier = identifier
            if session_id is not None:
                client.session_id = session_id
        self._pending_session_id = session_id
        # Set when a managed session id is restored from a previous invocation (via
        # :meth:`adopt_session`) but has not yet been confirmed live. A session may have
        # expired between invocations (default 15 min idle timeout), so the first invoke on
        # an unverified adopted session transparently recovers by starting a fresh one.
        self._adopted_unverified = False
        # Serializes access to the synchronous, session-stateful client so concurrent
        # tool calls (e.g. parallel `spawn`/bash) can't interleave invokes or double-start.
        self._lock = asyncio.Lock()
        # Background session-start + git-bootstrap task (see :meth:`warm_up`). When set, every
        # invoke awaits it first, so the first sandbox tool call overlaps the ~30-60s bootstrap with
        # the agent's earlier non-sandbox work (model calls, GitHub reads) instead of blocking on it.
        self._warmup_task: asyncio.Task[None] | None = None

    # ---- client / session lifecycle ----

    def _ensure_client(self) -> CodeInterpreter:
        """Lazily construct the Code Interpreter client (deferred import + boto3 client)."""
        if self._client is None:
            from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

            self._client = CodeInterpreter(self._region, integration_source="meta-harness")
            self._client.identifier = self.identifier
            if self._pending_session_id is not None:
                self._client.session_id = self._pending_session_id
        return self._client

    @property
    def session_id(self) -> str | None:
        """The active session id, or ``None`` if no session has started yet."""
        if self._client is None:
            return self._pending_session_id
        return self._client.session_id

    @property
    def owns_session(self) -> bool:
        """Whether this sandbox manages (and will :meth:`close`) its own session.

        ``True`` in lazy-start mode (the harness owns the session); ``False`` when
        attached to a caller-owned session via ``session_id``. Session persistence
        across invocations only applies to owned sessions.
        """
        return self._owns_session

    def adopt_session(self, session_id: str) -> bool:
        """Reattach to a managed session started in a previous invocation.

        This is how session persistence works across invocations: a plugin records the
        live ``session_id`` in ``agent.state`` after one run and replays it here at the
        start of the next, so the same Code Interpreter session (and its warm
        filesystem/kernel) is reused instead of paying a cold start.

        Unlike passing ``session_id`` to the constructor (caller-owned, never stopped),
        an *adopted* session stays **owned** by this sandbox — we started it originally,
        so :meth:`close` still stops it.

        Adoption is a no-op (returns ``False``) when the sandbox is attached to a
        caller-owned session, when a session is already live, or when ``session_id`` is
        falsy. The adopted session is treated as *unverified*: it may have expired
        between invocations, so the first invoke transparently falls back to a fresh
        session if it is gone (see :meth:`_invoke_collect`).

        Args:
            session_id: The previously-started managed session id to reattach to.

        Returns:
            ``True`` if the session id was adopted, ``False`` otherwise.
        """
        if not self._owns_session or not session_id:
            return False
        # Don't clobber a session that is already live this process.
        if self.session_id is not None:
            return False
        self._pending_session_id = session_id
        self._adopted_unverified = True
        if self._client is not None:
            self._client.session_id = session_id
        return True

    # ---- invocation ----

    def _start_managed_session(self, client: CodeInterpreter) -> None:
        """Start a managed session on ``client`` using the configured timeout/identifier.

        This runs only for a *fresh* managed session (initial start or a stale-adopted-session
        cold-start recovery) — never on plain :meth:`adopt_session`, which reattaches to a session
        that was already bootstrapped. So bootstrapping here gives "install once per real session"
        for free, with no extra state to track.
        """
        # Only pass session_timeout_seconds when set, so None doesn't override
        # the client/service default (15 min).
        start_kwargs: dict[str, Any] = {"identifier": self.identifier}
        if self.session_timeout_seconds is not None:
            start_kwargs["session_timeout_seconds"] = self.session_timeout_seconds
        client.start(**start_kwargs)
        if self.bootstrap_git:
            self._bootstrap_session(client)

    def _bootstrap_session(self, client: CodeInterpreter) -> None:
        """Install git into the fresh session's ``$HOME`` (best-effort; never raises).

        The Code Interpreter image ships no git and can't ``dnf install`` (non-root); we bootstrap
        it rootlessly via micromamba (see :func:`_git_bootstrap_script`). Fail-open: any failure
        (network hiccup, timeout, service error) logs a warning and leaves the session usable
        without git — the same philosophy as best-effort session persistence. ``PATH`` is injected
        per-command in :meth:`execute_streaming`, so a partial/failed install just means ``git``
        isn't found, not a broken shell. Runs in the worker thread under the client lock (we're
        already inside :meth:`_invoke_collect`), so it can call the sync client directly.

        When a ``github_token`` was provided, a second best-effort step bootstraps the git client
        with it (see :func:`_git_credentials_script`) so native git authenticates for the session's
        life. Ordered after the install because it needs the ``git`` binary; each step fails open
        independently.
        """
        try:
            self._invoke_drain(client, "executeCommand", {"command": _git_bootstrap_script()})
        except Exception as e:  # noqa: BLE001 — bootstrap is best-effort; never fail the turn
            logger.warning("sandbox git bootstrap failed (%s); continuing without git", e)
        if self._github_token:
            # Separate step + separate failure domain: a credential hiccup must not be conflated
            # with a git-install failure, and vice versa. SECURITY: the script embeds the token, so
            # never log the command; on failure log only the exception *type* — service validation
            # errors can echo the offending input back in their message.
            try:
                self._invoke_drain(
                    client,
                    "executeCommand",
                    {"command": _git_credentials_script(self._github_token)},
                )
            except Exception as e:  # noqa: BLE001 — best-effort, like the install above
                logger.warning(
                    "sandbox git credential bootstrap failed (%s); git will be unauthenticated",
                    type(e).__name__,
                )

    def warm_up(self) -> None:
        """Start the session + git bootstrap in the background so it overlaps the agent's first
        non-sandbox work (model calls, GitHub reads) instead of blocking the first tool call.

        Fire-and-forget: schedules a task that starts a fresh managed session (which bootstraps git)
        under the shared lock. The first real invoke awaits this task (see :meth:`_invoke`), so it
        naturally waits only for whatever bootstrap time hasn't already elapsed. A no-op when there's
        nothing to warm (a caller-owned or already-live session, or one already warming). Best-effort
        throughout — a failure is logged and the normal lazy path still runs on first invoke.

        Only worth calling for a fresh, owned session; safe (a no-op) otherwise. Requires a running
        event loop; if there is none, warm-up is skipped and the session starts lazily on first use.
        """
        if self._warmup_task is not None or not self._owns_session or self.session_id is not None:
            return

        async def _run() -> None:
            try:
                async with self._lock:
                    client = self._ensure_client()
                    if self._owns_session and client.session_id is None:
                        # to_thread: start + bootstrap are blocking boto3 calls.
                        await asyncio.to_thread(self._start_managed_session, client)
            except Exception as e:  # noqa: BLE001 — warm-up is best-effort; lazy path covers failure
                logger.warning("sandbox warm-up failed (%s); will start lazily on first use", e)

        try:
            self._warmup_task = asyncio.ensure_future(_run())
        except RuntimeError:
            # No running event loop (e.g. called from a sync context) — skip warm-up; the first
            # invoke will start + bootstrap the session lazily as before.
            self._warmup_task = None

    def _invoke_collect(self, name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        """Invoke a native tool and drain its result-event stream (runs in a worker thread).

        Starting a managed session (if needed) happens here too, since the
        underlying client's ``invoke`` auto-starts one and that is a blocking call.

        Stale-session recovery: when a session id was *adopted* from a previous
        invocation (``_adopted_unverified``), it may have expired (sessions idle out,
        default 15 min). The adopted session is a best-effort warm-start optimization,
        so if the first invoke against it fails for *any* reason we transparently drop
        it, start a fresh managed session, and retry once. Once an adopted session has
        served one successful invoke it is considered verified and recovery is disabled.
        """
        client = self._ensure_client()
        if self._owns_session and client.session_id is None:
            self._start_managed_session(client)
        try:
            return self._invoke_drain(client, name, arguments)
        except Exception:
            # Only an unverified, adopted, owned session is eligible for recovery: the id
            # came from a prior run and may simply be gone. Any other failure is real.
            if not (self._adopted_unverified and self._owns_session):
                raise
            # The adopted session is unusable — drop it and cold-start a fresh one.
            self._adopted_unverified = False
            client.session_id = None
            self._pending_session_id = None
            self._start_managed_session(client)
            return self._invoke_drain(client, name, arguments)
        finally:
            # First invoke completed (success or non-recoverable failure): the adopted
            # session is no longer "unverified" — either it worked, or we already
            # recovered above and cleared the flag.
            self._adopted_unverified = False

    def _invoke_drain(
        self, client: CodeInterpreter, name: str, arguments: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Invoke a native tool and fully drain its result-event stream."""
        response = client.invoke(name, arguments)
        stream = response.get("stream") if isinstance(response, dict) else None
        if stream is None:
            raise RuntimeError("AgentCore code interpreter returned no result stream")
        results: list[dict[str, Any]] = []
        for event in stream:
            results.extend(self._handle_event(event))
        return results

    @staticmethod
    def _handle_event(event: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Yield the ``result`` of a stream event, or raise on a service exception event."""
        if "result" in event:
            yield event["result"]
            return
        exception = (
            event.get("accessDeniedException")
            or event.get("conflictException")
            or event.get("internalServerException")
            or event.get("resourceNotFoundException")
            or event.get("serviceQuotaExceededException")
            or event.get("throttlingException")
            or event.get("validationException")
        )
        if exception:
            message = exception.get("message") if isinstance(exception, dict) else None
            raise RuntimeError(message or "AgentCore code interpreter returned an error")

    async def _invoke(
        self, name: str, arguments: dict[str, Any], *, timeout: float | None = None
    ) -> list[dict[str, Any]]:
        """Run a blocking invoke in a thread, honoring ``timeout`` best-effort.

        Access is serialized by :attr:`_lock` because the underlying sync client
        and its ``session_id`` are shared mutable state — this prevents concurrent
        tool calls (e.g. parallel ``spawn``/bash) from interleaving invokes or
        double-starting a session.

        ``timeout`` bounds how long the awaiting coroutine waits; the underlying
        boto3 call **cannot be hard-cancelled**, so on timeout the worker thread is
        abandoned (it keeps running until the service responds) and a
        :class:`TimeoutError` is raised. Use generous timeouts: a too-short one
        wastes a session slot on an abandoned thread.
        """
        # Await any in-flight warm-up first (session start + git bootstrap), so a warmed sandbox's
        # first invoke waits only for whatever bootstrap time hasn't already overlapped the agent's
        # earlier work. The warm-up task never raises (it's best-effort), so this can't fail here.
        if self._warmup_task is not None:
            await self._warmup_task
        async with self._lock:
            coro = asyncio.to_thread(self._invoke_collect, name, arguments)
            if timeout is not None:
                return await asyncio.wait_for(coro, timeout=timeout)
            return await coro

    async def _stream(
        self, name: str, arguments: dict[str, Any], *, timeout: float | None = None
    ) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        """Invoke a tool and yield its output as :class:`StreamChunk`\\ s then an :class:`ExecutionResult`."""
        results = await self._invoke(name, arguments, timeout=timeout)

        text_stdout = ""
        structured_stdout = ""
        stderr = ""
        exit_code = 0
        emitted_stdout = False
        output_files: list[OutputFile] = []

        for result in results:
            for block in result.get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    text_stdout += block["text"]
                    emitted_stdout = True
                    yield StreamChunk(data=block["text"], stream_type="stdout")
                else:
                    output_file = _to_output_file(block)
                    if output_file is not None:
                        output_files.append(output_file)

            structured = result.get("structuredContent")
            if structured:
                if structured.get("stdout"):
                    structured_stdout += structured["stdout"]
                if structured.get("stderr"):
                    stderr += structured["stderr"]
                    yield StreamChunk(data=structured["stderr"], stream_type="stderr")
                if structured.get("exitCode") is not None:
                    exit_code = structured["exitCode"]
            if result.get("isError") and exit_code == 0:
                exit_code = 1

        # Prefer textual content blocks; fall back to structured stdout when the
        # backend reports output only there.
        stdout = text_stdout
        if not emitted_stdout and structured_stdout:
            stdout = structured_stdout
            yield StreamChunk(data=structured_stdout, stream_type="stdout")

        yield ExecutionResult(exit_code=exit_code, stdout=stdout, stderr=stderr, output_files=output_files)

    async def _collect(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], bool]:
        """Invoke a tool and aggregate its content blocks + error flag."""
        results = await self._invoke(name, arguments)
        content: list[dict[str, Any]] = []
        is_error = False
        for result in results:
            content.extend(result.get("content") or [])
            if result.get("isError"):
                is_error = True
        return content, is_error

    # ---- Sandbox primitives ----

    async def execute_streaming(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        """Execute a shell command via AgentCore's native ``executeCommand`` tool.

        ``cwd`` and ``env`` are applied through the shell (``cd`` + ``export``),
        since ``executeCommand`` has no native cwd/env arguments. Env keys are
        validated and values are shell-quoted (via the SDK's
        :func:`~strands.sandbox.posix_shell.build_shell_env_prefix`).

        When git bootstrapping is enabled, ``$HOME/.gitenv/bin`` is prepended to ``PATH`` on every
        command (and ``GIT_PAGER``/``PAGER`` pinned to ``cat`` — the image has no ``less``, so a bare
        ``git log``/``diff`` would otherwise fail spawning a pager): each ``executeCommand`` is a
        fresh non-login shell, so the bootstrapped install dir is not otherwise on ``PATH`` (the
        session filesystem persists across commands, the shell environment does not). Prepending a
        not-yet-existent dir is a harmless no-op, so this is safe even before/without a successful
        bootstrap. It comes *before* the caller's ``env`` prefix, so an explicit ``PATH`` in ``env``
        still wins.
        """
        cd_prefix = f"cd {shlex.quote(cwd)} && " if cwd else ""
        # Unquoted on purpose so $PATH expands in the shell (build_shell_env_prefix quotes values,
        # which would defeat the expansion). SANDBOX_GIT_BIN/PAGER are fixed constants, not user input.
        path_prefix = (
            f'export PATH="{SANDBOX_GIT_BIN}:$PATH" {SANDBOX_GIT_PAGER_ENV} && '
            if self.bootstrap_git
            else ""
        )
        full_command = cd_prefix + path_prefix + build_shell_env_prefix(env) + command
        async for chunk in self._stream("executeCommand", {"command": full_command}, timeout=timeout):
            yield chunk

    async def execute_code_streaming(
        self,
        code: str,
        language: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        """Execute code via AgentCore's native ``executeCode`` tool.

        Runs in a persistent language kernel that can return image/chart
        :class:`OutputFile`\\ s. Because the kernel has no surrounding shell,
        ``env`` and ``cwd`` are not applied here \u2014 set them from within the code
        (e.g. ``os.environ`` / ``os.chdir`` in Python).
        """
        args = {"code": code, "language": _to_programming_language(language)}
        async for chunk in self._stream("executeCode", args, timeout=timeout):
            yield chunk

    async def read_file(self, path: str, **kwargs: Any) -> bytes:
        """Read a file from the session filesystem as raw bytes."""
        content, is_error = await self._collect("readFiles", {"paths": [path]})
        if is_error:
            raise FileNotFoundError(_collect_text(content) or f"Failed to read file: {path}")
        for block in content:
            resource = block.get("resource") or {}
            if resource.get("blob") is not None:
                return _as_bytes(resource["blob"])
            if resource.get("text") is not None:
                return _as_bytes(resource["text"])
            if block.get("type") == "text" and block.get("text") is not None:
                return _as_bytes(block["text"])
        # No content blocks on a non-error response means an empty (0-byte) file,
        # matching how the shell-backed sandboxes decode empty base64 output.
        return b""

    async def write_file(self, path: str, content: bytes, **kwargs: Any) -> None:
        """Write raw bytes to a file in the session filesystem."""
        result_content, is_error = await self._collect(
            "writeFiles", {"content": [{"path": path, "blob": content}]}
        )
        if is_error:
            raise OSError(_collect_text(result_content) or f"Failed to write file: {path}")

    async def remove_file(self, path: str, **kwargs: Any) -> None:
        """Remove a file from the session filesystem."""
        result_content, is_error = await self._collect("removeFiles", {"paths": [path]})
        if is_error:
            raise FileNotFoundError(_collect_text(result_content) or f"Failed to remove file: {path}")

    async def list_files(self, path: str, **kwargs: Any) -> list[FileInfo]:
        """List files in a session directory."""
        content, is_error = await self._collect("listFiles", {"directoryPath": path})
        if is_error:
            raise FileNotFoundError(_collect_text(content) or f"Failed to list directory: {path}")

        entries: list[FileInfo] = []
        for block in content:
            btype = block.get("type")
            if btype in ("resource_link", "resource"):
                resource = block.get("resource") or {}
                name = block.get("name") or resource.get("uri")
                if name:
                    entries.append(
                        _to_file_info(name, block.get("size"), is_dir=_block_is_dir(block))
                    )
            elif btype == "text" and block.get("text"):
                for line in block["text"].split("\n"):
                    name = line.strip()
                    if name:
                        entries.append(_to_file_info(name))
        return entries

    # ---- tools + lifecycle ----

    def get_tools(self) -> list[AgentTool]:
        """Default sandbox-compatible tools auto-registered with this sandbox."""
        from strands.vended_tools.bash import make_bash
        from strands.vended_tools.bash.types import SANDBOX_BASH_DESCRIPTION
        from strands.vended_tools.file_editor import make_file_editor
        from strands.vended_tools.file_editor.file_editor import DEFAULT_FILE_EDITOR_DESCRIPTION

        return [
            make_file_editor(
                sandbox=self,
                name="sandbox_file_editor",
                description=(
                    f"{DEFAULT_FILE_EDITOR_DESCRIPTION} Files are in an AgentCore Code "
                    f'Interpreter session ("{self.identifier}").'
                ),
            ),
            make_bash(
                sandbox=self,
                name="sandbox_bash",
                description=(
                    f'{SANDBOX_BASH_DESCRIPTION} Runs in an AgentCore Code Interpreter '
                    f'session ("{self.identifier}").'
                ),
            ),
        ]

    async def close(self) -> None:
        """Stop the managed session, if this sandbox started one.

        A no-op when attached to a caller-owned session (``session_id`` was
        provided) or when no session has started yet. Safe to call multiple times.
        """
        # Let any in-flight warm-up finish first, so we don't leak a session it started (or the task
        # itself) by racing close against it. It's best-effort and never raises.
        if self._warmup_task is not None:
            await self._warmup_task
            self._warmup_task = None
        if not self._owns_session or self._client is None or self._client.session_id is None:
            return
        async with self._lock:
            if self._client.session_id is None:  # re-check under lock
                return
            await asyncio.to_thread(self._client.stop)
