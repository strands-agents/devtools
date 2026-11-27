"""``chat`` against the deployed agent through the dashboard API (the browser login, no AWS)."""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import Callable
from typing import TextIO

from strandly_mcp.auth import AuthError
from strandly_mcp.dashboard import DashboardApiError, DashboardClient, run_dashboard_collect

EXIT_WORDS = frozenset({"exit", "quit", ":q"})


def new_session_id() -> str:
    return f"cli-{uuid.uuid4().hex[:12]}"


async def _turn(client: DashboardClient, session_id: str, prompt: str, err: TextIO) -> str:
    async def on_activity(message: str) -> None:
        print(f"  … {message}", file=err, flush=True)

    return await run_dashboard_collect(client, session_id, prompt, on_activity)


def chat_remote(
    client: DashboardClient,
    *,
    session_id: str | None = None,
    prompt: str | None = None,
    display_name: str = "Strandly",
    read_line: Callable[[str], str] = input,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """One turn with ``prompt``, else a REPL; every turn joins ``session_id`` (new when omitted)."""
    out = out or sys.stdout
    err = err or sys.stderr
    sid = session_id or new_session_id()
    print(f"{display_name} (remote) · session {sid}", file=err)
    prompts = [prompt] if prompt is not None else None
    while True:
        if prompts is not None:
            if not prompts:
                return 0
            text = prompts.pop()
        else:
            try:
                text = read_line("› ").strip()
            except (EOFError, KeyboardInterrupt):
                print(file=err)
                return 0
            if not text:
                continue
            if text.lower() in EXIT_WORDS:
                return 0
        try:
            answer = asyncio.run(_turn(client, sid, text, err))
        except AuthError as exc:
            print(f"chat: {exc}", file=err)
            return 1
        except DashboardApiError as exc:
            print(f"chat: {exc}", file=err)
            if prompts is not None:
                return 1
            continue
        except KeyboardInterrupt:
            print("\n(interrupted; the remote run may continue — `sessions show` reads it back)", file=err)
            if prompts is not None:
                return 130
            continue
        print(answer, file=out, flush=True)
