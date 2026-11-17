"""Local CLI REPL + one-shot run.

The CLI runs **autonomously by default** (no approval prompts). Pass ``--hitl`` to opt into
human-in-the-loop: ``HumanInTheLoop(ask="stdio")`` then prompts for approval inline before each
tool call.
"""

from __future__ import annotations

import asyncio
import sys

from strandly_harness.core.config import Config
from strandly_harness.core.context import RuntimeContext
from strandly_harness.core.events import HarnessEvent
from strandly_harness.serve.turn import run_turn


def render(ev: HarnessEvent) -> None:
    if ev.kind == "text":
        sys.stdout.write(ev.text or "")
        sys.stdout.flush()
    elif ev.kind == "reasoning":
        sys.stderr.write(f"\033[2m{ev.text or ''}\033[0m")
        sys.stderr.flush()
    elif ev.kind == "tool_start":
        sys.stderr.write(f"\n\033[36m→ {ev.tool}\033[0m\n")
    elif ev.kind == "tool_result":
        status = (ev.data or {}).get("status")
        sys.stderr.write(f"\033[36m← {ev.tool} [{status}]\033[0m\n")
    elif ev.kind == "error":
        sys.stderr.write(f"\n\033[31merror: {ev.text}\033[0m\n")
    elif ev.kind == "done":
        sys.stdout.write("\n")
        sys.stdout.flush()


async def _run_once(config: Config, prompt: str, session_id: str, *, hitl: bool) -> None:
    ctx = RuntimeContext(session_id=session_id)
    async for ev in run_turn(config, prompt, ctx, hitl=hitl):
        render(ev)


def run_oneshot(
    config: Config, prompt: str, *, session_id: str = "strandly-run", hitl: bool = False
) -> None:
    """Run a single prompt and stream to the terminal (the ``run`` subcommand)."""
    asyncio.run(_run_once(config, prompt, session_id, hitl=hitl))


def chat(config: Config, *, session_id: str = "strandly-chat", hitl: bool = False) -> None:
    """Interactive REPL (the ``chat`` subcommand)."""

    async def _loop() -> None:
        sys.stderr.write("strandly-harness — Ctrl-D to exit\n")
        while True:
            try:
                line = input("\n› ").strip()
            except EOFError:
                sys.stderr.write("\nbye\n")
                return
            if line:
                await _run_once(config, line, session_id, hitl=hitl)

    asyncio.run(_loop())


def render_event_dict(ev: dict) -> None:
    """Render a streamed event dict (from the deployed runtime's SSE) like :func:`render`."""
    kind = ev.get("kind")
    if kind == "text":
        sys.stdout.write(ev.get("text") or "")
        sys.stdout.flush()
    elif kind == "reasoning":
        sys.stderr.write(f"\033[2m{ev.get('text') or ''}\033[0m")
        sys.stderr.flush()
    elif kind == "tool_start":
        sys.stderr.write(f"\n\033[36m→ {ev.get('tool')}\033[0m\n")
    elif kind == "tool_result":
        status = (ev.get("data") or {}).get("status")
        sys.stderr.write(f"\033[36m← {ev.get('tool')} [{status}]\033[0m\n")
    elif kind == "error":
        sys.stderr.write(f"\n\033[31merror: {ev.get('text')}\033[0m\n")
    elif kind == "done":
        sys.stdout.write("\n")
        sys.stdout.flush()


def chat_agentcore(runtime_arn: str, region: str, session_id: str) -> None:
    """Interactive REPL streamed against the *deployed* AgentCore runtime (``chat --agentcore``).

    Each line is sent in streaming mode and the runtime's events are rendered inline — no GitHub
    context, no polling. Conversation continuity is the deployed Memory session (same ``session_id``
    every turn), so the deployed agent rehydrates prior turns.
    """
    from strandly_harness.ops.runtime_client import stream_run

    sys.stderr.write(f"strandly-harness (agentcore: {session_id}) — Ctrl-D to exit\n")
    while True:
        try:
            line = input("\n› ").strip()
        except EOFError:
            sys.stderr.write("\nbye\n")
            return
        if not line:
            continue
        try:
            for ev in stream_run(runtime_arn, region, session_id, line):
                render_event_dict(ev)
        except Exception as e:  # noqa: BLE001 — surface invoke/stream errors without killing the REPL
            sys.stderr.write(f"\n\033[31merror: {type(e).__name__}: {e}\033[0m\n")
