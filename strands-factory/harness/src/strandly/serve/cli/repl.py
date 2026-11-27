"""``strandly chat --agentcore``: a terminal REPL streamed from the deployed runtime."""

from __future__ import annotations

import sys


def render_event_dict(ev: dict) -> None:
    """Render one streamed event dict from the deployed runtime."""
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
    elif kind == "retry":
        sys.stderr.write("\n\033[33mtransient model error: retrying the call\033[0m\n")
    elif kind == "error":
        sys.stderr.write(f"\n\033[31merror: {ev.get('text')}\033[0m\n")
    elif kind == "cancelled":
        sys.stderr.write("\n\033[33mcancelled: superseded by a newer message, or stopped\033[0m\n")
    elif kind == "done":
        sys.stdout.write("\n")
        sys.stdout.flush()


def chat_agentcore(runtime_arn: str, region: str, session_id: str) -> None:
    """Interactive REPL streamed against the *deployed* AgentCore runtime (``chat --agentcore``).

    Each line is sent in streaming mode and the runtime's events are rendered inline — no GitHub
    context, no polling. Conversation continuity is the deployed session store (same ``session_id``
    every turn), so the deployed agent restores prior turns from its snapshot.
    """
    from strandly.ops.runtime_client import stream_run

    sys.stderr.write(f"strandly (agentcore: {session_id}) — Ctrl-D to exit\n")
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
