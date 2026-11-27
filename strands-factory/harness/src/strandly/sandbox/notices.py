"""Deliver the sandbox's model-facing notices on the agent's next tool result.

:class:`~strandly.sandbox.runtime.RuntimeSandbox` records notices the model must see (what a prune
deleted, that a restart stopped background processes, that the workspace is full). Most arise at
wake, before the harness's own sandbox calls whose output the model never sees, so they ride the
next tool result instead. Each agent (parent and subagents share one sandbox) gets its own copy, and
each tool call's sandbox commands are tagged with the call id and agent depth for the logs.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent
from strands.plugins import Plugin

from strandly.sandbox.runtime import (
    SandboxCommandTimeoutError,
    SandboxExecutionUncertainError,
    tag_command_caller,
)

if TYPE_CHECKING:
    from strands.sandbox.base import Sandbox

#: Late, so other AfterToolCall hooks have already rewritten the result.
_DELIVER_ORDER = 80


class SandboxNotices(Plugin):
    """Append undelivered sandbox notices to the agent's next tool result."""

    name = "sandbox_notices"

    def __init__(self, sandbox: Sandbox, *, depth: int = 0) -> None:
        self._sandbox = sandbox
        self._depth = depth
        self._seen = 0
        super().__init__()

    def init_agent(self, agent: Any) -> None:
        super().init_agent(agent)
        agent.add_hook(self._deliver, AfterToolCallEvent, order=_DELIVER_ORDER)
        agent.add_hook(self._tag, BeforeToolCallEvent)

    def _tag(self, event: BeforeToolCallEvent) -> None:
        # Synchronous, in the context the tool then runs in (and its subagents inherit).
        tag_command_caller(event.tool_use.get("toolUseId"), self._depth)

    async def _deliver(self, event: AfterToolCallEvent) -> None:
        source = getattr(self._sandbox, "notices_since", None)
        if source is None:  # a sandbox that records no notices (local, tests)
            return
        result = event.result
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
            return
        content = list(result["content"])
        error = getattr(event, "exception", None)
        for _ in range(8):
            if isinstance(error, SandboxExecutionUncertainError):
                event.retry = False
                if error.stdout or error.stderr:
                    # The SDK shell wraps exceptions and drops their output attributes. Return a
                    # bounded preview to this call only; never put arbitrary output in diagnostics.
                    content.append({"text": json.dumps({
                        "output": error.stdout[:16000],
                        "error": error.stderr[:16000],
                        "exit_code": None,
                        "completion": "unknown",
                        "truncated": len(error.stdout) > 16000 or len(error.stderr) > 16000,
                    })})
                break
            if isinstance(error, SandboxCommandTimeoutError):
                # Abandoned, not finished: it may still be running. Its output rides in the error.
                event.retry = False
                break
            if error is None:
                break
            error = error.__cause__
        notices, self._seen = source(self._seen)
        if notices:
            content.append({"text": "\n".join(notices)})
        if content != result["content"]:
            event.result = {**result, "content": content}
