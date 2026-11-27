"""Sandbox selection: the sandbox AgentCore Runtime (``STRANDLY_SANDBOX_RUNTIME_ARN``), always.

One instance is injected into every file and exec tool, so all execution shares one isolation
boundary. There is no local fallback: Strandly runs deployed only.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from strandly.core.config import (
    SANDBOX_CAPACITY_PROVIDER_ID,
    SANDBOX_TTL_ROLE_ARN,
    SANDBOX_TTL_SCHEDULE_GROUP,
    Config,
)
from strandly.core.constants import (
    AGENTCORE_SANDBOX_RUNTIME_ENV,
)
from strandly.core.context import RuntimeContext

if TYPE_CHECKING:
    from strands.sandbox.base import Sandbox

    from strandly.sandbox.ttl import SessionTtl

logger = logging.getLogger(__name__)


class SandboxNotConfiguredError(RuntimeError):
    """Raised when no sandbox runtime is configured.

    Without one, model-authored ``shell`` would execute inside the agent's own container, beside
    its secrets and the full-privilege ``use_github`` token. That state is reachable by omission
    alone, so it fails closed rather than warning.
    """


#: Warn about the TTL wiring once per process (a 14-day process builds a sandbox per run).
_ttl_warned = False
_ttl_warned_lock = threading.Lock()


def _warn_ttl_wiring_once(message: str, *args: object) -> None:
    """Log ``message`` (WARNING) and count ``SandboxTtlMisconfigured`` — once per process."""
    global _ttl_warned
    with _ttl_warned_lock:
        if _ttl_warned:
            return
        _ttl_warned = True
    logger.warning(message, *args)
    from strandly.ops import metrics

    metrics.emit({metrics.SANDBOX_TTL_MISCONFIGURED: 1}, surface=metrics.SURFACE_AGENTCORE)


def _session_ttl(config: Config) -> SessionTtl | None:
    """The sandbox sessions' sliding TTL, or ``None`` when the deploy wired none (all three ids).

    Never fails a run, but never silent either: without the TTL nothing ever deletes a sandbox
    session's EBS volume, so a partial wiring (1–2 of the 3 values: a typo, a half-applied deploy)
    and a runtime sandbox with no wiring at all both log a WARNING and count
    ``SandboxTtlMisconfigured``, once per process.
    """
    from strandly.sandbox.ttl import SessionTtl, SessionTtlConfig

    values = {
        SANDBOX_CAPACITY_PROVIDER_ID: config.sandbox_capacity_provider_id,
        SANDBOX_TTL_SCHEDULE_GROUP: config.sandbox_ttl_schedule_group,
        SANDBOX_TTL_ROLE_ARN: config.sandbox_ttl_role_arn,
    }
    ttl_config = SessionTtlConfig.from_values(*values.values())
    if ttl_config is None:
        missing = [name for name, value in values.items() if not value]
        if len(missing) < len(values):
            _warn_ttl_wiring_once(
                "sandbox session TTL is OFF: it needs all of %s, but %s %s unset — no sandbox "
                "session (or its EBS volume) will ever be deleted. Fix the deploy wiring.",
                ", ".join(values),
                ", ".join(missing),
                "is" if len(missing) == 1 else "are",
            )
        else:
            _warn_ttl_wiring_once(
                "sandbox session TTL is OFF: a runtime sandbox is configured but none of %s is set, "
                "so no sandbox session (or its EBS volume) will ever be deleted. Fix the deploy wiring.",
                ", ".join(values),
            )
        return None
    return SessionTtl(ttl_config, region=config.aws_region)


def _reset_ttl_warning() -> None:
    """Forget the once-per-process TTL warning (tests)."""
    global _ttl_warned
    with _ttl_warned_lock:
        _ttl_warned = False


def build_sandbox(config: Config, ctx: RuntimeContext | None = None) -> Sandbox:
    """Build the sandbox every file/exec tool will run in.

    Args:
        config: Resolved deployment config. ``STRANDLY_SANDBOX_RUNTIME_ARN`` is the gate.
        ctx: The per-invocation context. Only its session key is used — to *derive* the sandbox
            runtime session id, which is what makes the sandbox filesystem follow a conversation
            across invocations (see
            :func:`~strandly.sandbox.runtime.sandbox_session_id`). ``None`` (or a
            sessionless invoke) gets a fresh, unshared sandbox session.

    Raises:
        SandboxNotConfiguredError: No sandbox runtime is configured (see the class docstring).
    """
    if config.sandbox_runtime_arn:
        from strandly.sandbox.runtime import RuntimeSandbox

        return RuntimeSandbox(
            runtime_arn=config.sandbox_runtime_arn,
            # The session key, not the raw session id: it is what identifies "this conversation"
            # everywhere else in the harness (serve/cache.session_key), so the sandbox filesystem
            # follows exactly the same boundary as the conversation history does.
            session_key=(ctx.session_key or ctx.session_id) if ctx else None,
            region=config.aws_region,
            qualifier=config.sandbox_runtime_qualifier,
            # The sandbox's OWN git credential (STRANDLY_SANDBOX_GIT_TOKEN) — deliberately NOT the
            # `use_github` token. Anything the agent runs in the sandbox can read this credential
            # (`git credential fill` was observed in prod), so it is scoped to `Contents` only: it
            # can clone/fetch/push, and it cannot comment, review, or approve as the agent.
            # Everything that speaks as the agent goes through `use_github` host-side instead.
            #
            # No fallback to config.github_token on purpose — see Config.sandbox_git_token. Unset ⇒
            # no credential in the sandbox: public clones still work anonymously, pushes don't.
            github_token=config.sandbox_git_token,
            git_author_name=config.get("STRANDLY_GIT_AUTHOR_NAME") or "Strandly",
            git_author_email=config.get("STRANDLY_GIT_AUTHOR_EMAIL") or "strandly@localhost",
            # The sliding session TTL (deploy wiring): every wake and command re-schedules the
            # session's deletion — its instance and EBS volume — 5 days out (a sessionless invoke:
            # once, past the longest possible run).
            ttl=_session_ttl(config),
        )
    raise SandboxNotConfiguredError(
        f"{AGENTCORE_SANDBOX_RUNTIME_ENV} is not set: refusing to execute the model's shell "
        "commands inside the agent's own container, beside its secrets. Set it to the sandbox "
        "runtime's ARN (the Sandbox stack's SandboxRuntimeArn output) and redeploy."
    )



# Build-time code (the skills loader, the environment block) asks these instead of touching the
# sandbox, so a run that never uses a tool wakes no sandbox session. Duck-typed: importing
# ``runtime`` would pull in boto3, and test doubles count as always awake.
def supports_first_use_hooks(sandbox: Any) -> bool:
    """True for a sandbox that can defer setup work to its first use (``add_first_use_hook``)."""
    return callable(getattr(sandbox, "add_first_use_hook", None))


def is_unwoken(sandbox: Any) -> bool:
    """True for a lazy sandbox whose session has not been woken yet — touching it would wake it.

    A plain attribute read: never an API call. ``False`` for any sandbox without the lazy contract.
    """
    if not supports_first_use_hooks(sandbox):
        return False
    try:
        return getattr(sandbox, "ready", True) is False
    except Exception:  # noqa: BLE001 — an exotic property must never break a build
        return False


__all__ = ["SandboxNotConfiguredError", "build_sandbox", "is_unwoken", "supports_first_use_hooks"]
