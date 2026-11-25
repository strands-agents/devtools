"""ACP stdio task delegation to Strandly's authenticated native API."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from acp import Agent, Client, RequestError, run_agent
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AuthenticateResponse,
    AuthMethodAgent,
    ClientCapabilities,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    TextContentBlock,
)
from acp.stdio import stdio_streams

from strandly_mcp import auth, identity

logger = logging.getLogger(__name__)
POLL_SECONDS = 0.25
RUN_TIMEOUT_SECONDS = 1800
CONTROL_ATTEMPTS = 3
CLEANUP_TIMEOUT_SECONDS = 5
# A cold remote session answers control calls "starting" (HTTP 504) for about a minute. The
# same idempotent call is repeated for this long (override in seconds via the env var).
START_TIMEOUT_ENV = "STRANDLY_ACP_START_TIMEOUT"
START_TIMEOUT_SECONDS = 180.0
START_BACKOFF_SECONDS = (0.25, 5.0)  # first delay, doubling up to the cap
# Login stores the bare API origin (the portal config's ``apiBase``); the deployed route is
# ``/api/chat``, the same convention as ``strandly_mcp.dashboard``.
CHAT_PATH = "/api/chat"
LOGIN_COMMAND = "strandly login"


def policy(login_command: str = LOGIN_COMMAND) -> str:
    return (
        "Delegates tasks to the deployed Strandly workspace under its operator policy. "
        "No local filesystem access/sync, client MCP, or per-tool permission enforcement. "
        f"Run {login_command} separately."
    )


POLICY = policy()


def _no_overrides(values: dict[str, Any]) -> None:
    # ACP metadata is opaque, not configuration or caller identity.
    if any(
        value is not None for key, value in values.items() if key not in {"_meta", "field_meta"}
    ):
        raise RequestError.invalid_params({"reason": "Unsupported overrides"})


def _start_timeout() -> float:
    try:
        value = float(os.environ.get(START_TIMEOUT_ENV, ""))
    except ValueError:
        return START_TIMEOUT_SECONDS
    return value if 0 <= value < float("inf") else START_TIMEOUT_SECONDS


def _flagged(exc: RequestError, flag: str) -> bool:
    return isinstance(exc.data, dict) and exc.data.get(flag) is True


class NativeAPI:
    """Use a single login identity and origin for the lifetime of the bridge."""

    def __init__(self, auth_file: Path | None = None, login_command: str = LOGIN_COMMAND) -> None:
        self.http = httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
        # ``None`` is the shared ``strandly login`` cache; a generated CLI passes its own.
        self.auth_file = auth_file
        self.login_command = login_command
        self.login: dict[str, Any] | None = None
        # The advisory person from the CLI's own identity cache, never from ACP metadata.
        self.person: str | None = None
        self.prefix: str | None = None
        self.lock = asyncio.Lock()

    async def request(self, method: str, values: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self.lock:
                if self.login is None:
                    self.login = auth.load_auth(self.auth_file)
                    self.person = identity.person_header(
                        identity.load_identity(identity.identity_path(self.auth_file))
                    )
                if not self.login:
                    raise auth.NotLoggedInError(f"run `{self.login_command}`")
                base = str(self.login.get("api_url", "")).rstrip("/")
                parsed = urlsplit(base)
                if (
                    (
                        parsed.scheme != "https"
                        and not (
                            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
                        )
                    )
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                ):
                    raise RequestError.invalid_params({"reason": "Login API must use HTTPS"})
                token, self.login, changed = await asyncio.to_thread(
                    auth.ensure_access_token, self.login
                )
                if changed:
                    await asyncio.to_thread(auth.save_auth, self.login, self.auth_file)
            headers = {"Authorization": "Bearer " + token}
            if self.person:
                headers[identity.PERSON_HEADER] = self.person
            response = await self.http.request(
                method,
                base + CHAT_PATH,
                headers=headers,
                params=values if method == "GET" else None,
                json=values if method != "GET" else None,
            )
            if response.status_code in {401, 403}:
                raise RequestError.auth_required({"reason": f"Run {self.login_command} separately"})
            if response.status_code == 504 and self._starting(response):
                # Safe to repeat for a poll/cancel/probe only; see control().
                raise RequestError.internal_error(
                    {"reason": "Remote runtime starting", "retryable": True, "starting": True}
                )
            if response.status_code >= 500:
                raise RequestError.internal_error(
                    {"reason": "Native API unavailable", "retryable": True}
                )
            if response.status_code != 200:
                raise RequestError(
                    -32603, "Native API request failed", {"status": response.status_code}
                )
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("non-object response")
            return result
        except auth.AuthError:
            raise RequestError.auth_required({"reason": f"Run {self.login_command} separately"}) from None
        except httpx.HTTPError:
            raise RequestError.internal_error(
                {"reason": "Native API unavailable", "retryable": True}
            ) from None
        except (OSError, ValueError, KeyError, TypeError):
            # Never forward backend errors, response bodies, tokens, or exception URLs.
            raise RequestError.internal_error(
                {"reason": "Native API unavailable or invalid response"}
            ) from None

    @staticmethod
    def _starting(response: httpx.Response) -> bool:
        try:
            body = response.json()
        except ValueError:
            return False
        return (
            isinstance(body, dict)
            and body.get("status") == "starting"
            and body.get("retryable") is True
        )

    async def control(self, method: str, values: dict[str, Any]) -> dict[str, Any]:
        """Repeat one idempotent call (probe, poll, cancel) while its session cold-starts.

        Never a launch: that is sent once and followed by exact-run polling.
        """
        budget = _start_timeout()
        loop = asyncio.get_running_loop()
        began, delay = loop.time(), START_BACKOFF_SECONDS[0]
        while True:
            try:
                return await self.request(method, values)
            except RequestError as exc:
                if not _flagged(exc, "starting"):
                    raise
                waited = loop.time() - began
                if waited + delay > budget:
                    raise RequestError.internal_error(
                        {
                            "reason": f"Remote runtime did not start within {budget:g}s",
                            "retryable": True,
                            "startTimeout": True,
                        }
                    ) from None
                logger.warning("Remote runtime starting; retrying (%.0fs of %gs)", waited, budget)
                await asyncio.sleep(delay)
                delay = min(delay * 2, START_BACKOFF_SECONDS[1])

    async def connect(self) -> str:
        if self.prefix is None:
            # The probe must reach the live runtime (fail closed); a cold one is waited for.
            result = await self.control("GET", {"capabilities": "acp"})
            prefix = result.get("sessionPrefix")
            if (
                result.get("runReceipts") != 1
                or not isinstance(prefix, str)
                or not re.fullmatch(r"acp-[0-9a-f]{24}-", prefix)
            ):
                raise RequestError(
                    -32603, "Backend lacks exact-run receipt support; update the native API/runtime"
                )
            self.prefix = prefix
        return self.prefix


@dataclass
class Run:
    result_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cancel_requested: bool = False
    cancel_acknowledged: bool = False
    cancel_status: str | None = None
    task_id: str | None = None

    def observe_task(self, result: dict[str, Any]) -> None:
        task_id = result.get("taskId")
        if isinstance(task_id, str) and task_id != self.task_id:
            self.task_id = task_id
            # Reapply exact-result intent when a cold-start task becomes visible.
            self.cancel_acknowledged = False


class RemoteAgent:
    """Text-only remote tasks; never implements client filesystem or permission tools."""

    def __init__(self, auth_file: Path | None = None, login_command: str = LOGIN_COMMAND) -> None:
        self.api = NativeAPI(auth_file, login_command)
        self.client: Client
        self.initialized = False
        self.sessions: set[str] = set()
        self.active: dict[str, Run] = {}

    def on_connect(self, conn: Client) -> None:
        self.client = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        _no_overrides(kwargs)
        self.initialized = True
        # Version 1 is the only supported stable protocol, including when negotiation differs.
        return InitializeResponse(
            protocol_version=1,
            agent_capabilities=AgentCapabilities(load_session=False),
            agent_info=Implementation(
                name="strandly-remote", version="0.1.0", title="Strandly remote tasks"
            ),
            auth_methods=[
                AuthMethodAgent(
                    id="strandly-login",
                    name=self.api.login_command,
                    description=policy(self.api.login_command),
                )
            ],
        )

    def _ready(self) -> None:
        if not self.initialized:
            raise RequestError.invalid_request({"reason": "Initialize first"})

    async def authenticate(self, method_id: str, **kwargs: Any) -> AuthenticateResponse:
        self._ready()
        _no_overrides(kwargs)
        if method_id != "strandly-login":
            raise RequestError.invalid_params({"reason": "Unknown authentication method"})
        await self.api.connect()
        return AuthenticateResponse()

    async def new_session(
        self,
        cwd: str,
        mcp_servers: list[Any] | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        self._ready()
        _no_overrides(kwargs)
        if mcp_servers or additional_directories:
            raise RequestError.invalid_params(
                {"reason": "Client MCP and workspace mounts are unsupported"}
            )
        # cwd is required by ACP but is deliberately not forwarded to the remote runtime.
        prefix = await self.api.connect()
        sid = prefix + uuid.uuid4().hex
        self.sessions.add(sid)
        return NewSessionResponse(session_id=sid)

    async def load_session(self, cwd: str, session_id: str, **kwargs: Any) -> None:
        raise RequestError.method_not_found("session/load")

    async def set_session_mode(self, session_id: str, mode_id: str, **kwargs: Any) -> None:
        raise RequestError.method_not_found("session/set_mode")

    async def set_session_config_option(
        self, session_id: str, config_id: str, value: Any, **kwargs: Any
    ) -> None:
        raise RequestError.method_not_found("session/set_config_option")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None

    async def _stop(self, session_id: str, run: Run, *, wait: bool = False) -> None:
        run.cancel_requested = True
        task_id = run.task_id
        result = await (self.api.control if wait else self.api.request)(
            "POST",
            {
                "session_id": session_id,
                "result_id": run.result_id,
                "action": "cancel",
            },
        )
        if result.get("status") not in {"cancel_requested", "cancelled", "completed", "failed"}:
            raise RequestError.internal_error(
                {"reason": "Remote cancellation was not acknowledged"}
            )
        run.cancel_acknowledged = task_id == run.task_id
        run.cancel_status = result["status"]

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        _no_overrides(kwargs)
        run = self.active.get(session_id)
        if run is not None:
            # The result id exists before launch is awaited; the server persists early intent.
            try:
                await self._stop(session_id, run)
            except RequestError:
                # Notifications have no error response; the prompt retains and follows intent.
                logger.warning("Remote cancellation pending; prompt will follow exact-run intent")

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        self._ready()
        _no_overrides(kwargs)
        if session_id not in self.sessions:
            raise RequestError.resource_not_found()
        if session_id in self.active:
            raise RequestError.invalid_request({"reason": "Session already has an active prompt"})
        if not prompt or any(not isinstance(block, TextContentBlock) for block in prompt):
            raise RequestError.invalid_params({"reason": "Only text content is supported"})
        for block in prompt:
            _no_overrides(block.model_extra or {})
        text = "\n".join(block.text for block in prompt)
        if not text.strip():
            raise RequestError.invalid_params({"reason": "Nonempty text required"})
        run = Run()
        self.active[session_id] = run
        try:
            return await asyncio.wait_for(self._run(session_id, run, text), RUN_TIMEOUT_SECONDS)
        except (RequestError, asyncio.TimeoutError, asyncio.CancelledError) as exc:
            await self._cleanup(session_id, run)
            if isinstance(exc, asyncio.CancelledError):
                raise
            data = dict(exc.data) if isinstance(exc, RequestError) and isinstance(exc.data, dict) else {}
            data.update(
                retryable=False,
                cancellation=run.cancel_status if run.cancel_status in {"cancelled", "completed", "failed"}
                else "stopping unconfirmed; remote task may still be running",
            )
            if isinstance(exc, asyncio.TimeoutError):
                data["reason"] = "Remote task timed out; completion/delivery uncertain; do not repeat task"
                raise RequestError.internal_error(data) from None
            # Once launch may have executed, HTTP retry hints must never escape to a caller.
            raise RequestError(exc.code, str(exc), data) from None
        finally:
            self.active.pop(session_id, None)

    async def _run(self, sid: str, run: Run, text: str) -> PromptResponse:
        values = {"session_id": sid, "result_id": run.result_id}
        try:
            launch = await self.api.request("POST", {**values, "message": text})
        except RequestError as exc:
            if isinstance(exc.data, dict) and exc.data.get("retryable") is True:
                # The HTTP retry hint is safe for polls/cancel, not a new prompt after acceptance.
                raise RequestError.internal_error(
                    {"reason": "Remote launch acceptance unknown; launch was not retried"}
                ) from None
            raise
        if launch.get("status") not in {
            "accepted",
            "starting",
            "running",
            "completed",
            "cancelled",
        }:
            raise RequestError.internal_error({"reason": "Remote task launch was not accepted"})
        run.observe_task(launch)
        seq = 0
        emitted = ""
        delivery_gap = False
        cancel_failures = poll_failures = 0
        while True:
            if run.cancel_requested and not run.cancel_acknowledged:
                try:
                    await self._stop(sid, run, wait=True)
                    cancel_failures = 0
                except RequestError as exc:
                    cancel_failures += 1
                    if not self._transient(exc) or cancel_failures >= CONTROL_ATTEMPTS:
                        raise self._uncertain(exc, "Cancellation unconfirmed") from None
                    # Still read the receipt: completion may have won the cancellation race.
            try:
                result = await self.api.control("GET", {**values, "since": seq})
                poll_failures = 0
            except RequestError as exc:
                poll_failures += 1
                if self._transient(exc) and poll_failures < CONTROL_ATTEMPTS:
                    await asyncio.sleep(POLL_SECONDS)
                    continue
                raise self._uncertain(exc, "Remote receipt unavailable") from None
            run.observe_task(result)
            next_seq, first_seq = result.get("nextSeq"), result.get("firstSeq")
            events = result.get("events")
            # Validate the whole batch BEFORE sending any text. An absent floor (older runtime
            # or another instance), stale cursor or malformed batch suspends streaming; only a
            # final exact-run answer can then prove the missing suffix without duplication.
            valid = (
                type(first_seq) is int and type(next_seq) is int
                and 0 <= first_seq <= seq <= next_seq and isinstance(events, list)
            )
            previous = seq - 1
            chunks = []
            if valid:
                assert isinstance(events, list) and isinstance(next_seq, int)
                for event in events:
                    if not isinstance(event, dict):
                        valid = False
                        break
                    event_seq = event.get("seq")
                    if type(event_seq) is not int or not previous < event_seq < next_seq:
                        valid = False
                        break
                    previous = event_seq
                    # A second allowlist excludes thought/tool content even from a bad backend.
                    if event.get("kind") == "text":
                        if not isinstance(event.get("text"), str):
                            valid = False
                            break
                        chunks.append(event["text"])
            delivery_gap = delivery_gap or not valid
            if not delivery_gap:
                assert isinstance(next_seq, int)
                for chunk in chunks:
                    await self._emit(sid, chunk)
                    emitted += chunk
                seq = next_seq
            status = result.get("status")
            if status == "cancelled" and not delivery_gap:
                return PromptResponse(stop_reason="cancelled")
            if status == "completed":
                answer = result.get("result")
                if not isinstance(answer, str) or not answer.startswith(emitted):
                    raise self._incomplete()
                # Compare exact Unicode strings, not byte offsets or a guessed overlap. Even a
                # gapless live stream must match the durable answer before successful end_turn.
                await self._emit(sid, answer[len(emitted):])
                return PromptResponse(stop_reason="end_turn")
            if status not in {"running", "unknown"}:
                raise self._incomplete() if delivery_gap else RequestError.internal_error(
                    {"reason": "Remote task failed; do not repeat task"}
                )
            await asyncio.sleep(POLL_SECONDS)

    @staticmethod
    def _transient(exc: RequestError) -> bool:
        # An exhausted cold-start wait already used its whole budget; it is not repeated.
        return _flagged(exc, "retryable") and not _flagged(exc, "startTimeout")

    @staticmethod
    def _uncertain(exc: RequestError, what: str) -> RequestError:
        if _flagged(exc, "startTimeout"):
            assert isinstance(exc.data, dict)
            what = exc.data["reason"]
        return RequestError.internal_error(
            {"reason": what + "; completion/delivery uncertain; do not repeat task"}
        )

    @staticmethod
    def _incomplete() -> RequestError:
        return RequestError.internal_error({
            "reason": "Remote output delivery incomplete/uncertain; do not repeat task",
            "retryable": False,
        })

    async def _emit(self, sid: str, text: str) -> None:
        if text:
            await self.client.session_update(
                sid, AgentMessageChunk(content=TextContentBlock(type="text", text=text))
            )

    async def _cleanup(self, sid: str, run: Run) -> None:
        async def stop() -> None:
            for attempt in range(CONTROL_ATTEMPTS):
                try:
                    await self._stop(sid, run)
                    return
                except RequestError as exc:
                    if not self._transient(exc) or attempt == CONTROL_ATTEMPTS - 1:
                        raise
                    await asyncio.sleep(POLL_SECONDS)
        try:
            await asyncio.wait_for(stop(), CLEANUP_TIMEOUT_SECONDS)
        except (RequestError, asyncio.TimeoutError):
            logger.warning("Remote cancellation unconfirmed; task may outlive the ACP connection")

    async def close(self) -> None:
        for sid, run in list(self.active.items()):
            await self._cleanup(sid, run)
        await self.api.http.aclose()


async def serve(auth_file: Path | None = None, login_command: str = LOGIN_COMMAND) -> None:
    agent = RemoteAgent(auth_file, login_command)
    reader, writer = await stdio_streams()
    with contextlib.redirect_stdout(sys.stderr):
        try:
            await run_agent(cast(Agent, agent), input_stream=writer, output_stream=reader)
        finally:
            await agent.close()


def main(auth_file: Path | None = None, login_command: str = LOGIN_COMMAND) -> int:
    try:
        asyncio.run(serve(auth_file, login_command))
    except (KeyboardInterrupt, BrokenPipeError):
        return 0
    return 0
