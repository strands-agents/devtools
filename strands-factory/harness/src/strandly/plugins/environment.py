"""The harness's environment context, deferred while the lazy sandbox has not woken.

``strands_harness.plugins.EnvironmentContext`` probes the sandbox on the first user turn, which would
wake a lazy sandbox (a minute or more) before a run that may never use a tool. Until the sandbox is
awake, each user turn gets a fixed block instead; from the first user turn after it wakes, the
harness's own block.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import TYPE_CHECKING, Any

from strands.vended_plugins.context_injector import ContextInjector, InjectionContext
from strands_harness.plugins import EnvironmentContext

# Private: the harness exposes no hook to defer its probe (pinned by test_environment.py).
from strands_harness.plugins.environment import _render

from strandly.core.constants import SANDBOX_WORKDIR
from strandly.sandbox.select import is_unwoken

if TYPE_CHECKING:
    from strands import Agent

logger = logging.getLogger(__name__)

#: The sandbox image's OS (Dockerfile.sandbox: ``python:3.13-slim-bookworm``), for the block of a
#: sandbox that has not woken, where ``uname`` cannot be asked.
SANDBOX_SYSTEM_DESCRIPTION = "Linux (Debian 12 container, the sandbox runtime image)"

#: Bound on the harness's one-time probe: its commands carry the sandbox's default timeout, and the
#: probe runs before the model call. On timeout the turn gets the fixed block and the next one retries.
PROBE_TIMEOUT_SECONDS = 30


#: The block's last line while the sandbox sleeps.
UNWOKEN_NOTE = (
    "Sandbox: starts on your first shell or file tool call (that first call can take a minute or two "
    "while it boots); answering without tools never starts it"
)


def fixed_block(sandbox: Any, note: str = UNWOKEN_NOTE) -> str:
    """The environment block built without a sandbox call."""
    workdir = getattr(sandbox, "workdir", None) or SANDBOX_WORKDIR
    lines = [
        f"Platform: {SANDBOX_SYSTEM_DESCRIPTION}",
        f"Date: {datetime.date.today().isoformat()}",
        f"Working directory: {workdir}",
        note,
    ]
    return "<system-reminder>\n<environment>\n" + "\n".join(lines) + "\n</environment>\n</system-reminder>"


class SandboxEnvironment(EnvironmentContext):
    """``EnvironmentContext`` that leaves an unwoken lazy sandbox asleep.

    An instance in ``plugins`` takes the place of the harness's ``environment`` built-in.
    """

    def init_agent(self, agent: Agent) -> None:
        """Register the environment injector, scoped to this agent."""
        memo: dict[str, Any] = {}

        async def render(_context: InjectionContext) -> str | None:
            if is_unwoken(agent.sandbox):
                return fixed_block(agent.sandbox)
            try:
                return await asyncio.wait_for(_render(agent, memo), PROBE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning("reason=<probe timeout> | environment probe | injecting the fixed block")
                return fixed_block(agent.sandbox, "Sandbox: awake")

        ContextInjector(render, name=f"{self.name}:injector", trigger="userTurn").init_agent(agent)
