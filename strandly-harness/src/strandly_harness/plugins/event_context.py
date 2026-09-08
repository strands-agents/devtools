"""Event-context injector — adds the agent's *actual* runtime context per turn.

Like the SDK MemoryManager injects recalled memories into the latest user message, this injects
*environment* context once (platform, date, working dir + git status) and re-surfaces the current
todo list as a ``<system-reminder>`` on later turns. It hooks ``BeforeInvocationEvent`` and prepends
to the latest user message, which the agent loop reads back.

**The environment is the agent's, not the host's.** The agent's `bash`/`file_editor` run inside the
sandbox, which on AgentCore is a remote Code Interpreter — a different OS, cwd, and git state than
the process hosting the harness. So we query the **sandbox** for that block (one `bash` call); we
never report `platform`/`os.getcwd()`/host git, which would describe a machine the agent can't see.
The query is async and best-effort: if it fails we inject only the date and skip the rest rather
than lie.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from strands.hooks import BeforeInvocationEvent
from strands.plugins import Plugin, hook

from strandly_harness.tools.todo import TODO_STATE_KEY, render_todos

if TYPE_CHECKING:
    from strands.sandbox.base import Sandbox

# One shell snippet so the environment is described from inside the sandbox, in a single round trip.
_ENV_PROBE = (
    'printf "cwd=%s\\n" "$(pwd)"; '
    'printf "uname=%s\\n" "$(uname -srm 2>/dev/null)"; '
    'if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then '
    'printf "branch=%s\\n" "$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"; '
    'printf "status<<EOF\\n%s\\nEOF\\n" "$(git status --porcelain 2>/dev/null)"; '
    'printf "log<<EOF\\n%s\\nEOF\\n" "$(git log --oneline -5 2>/dev/null)"; '
    "fi"
)


async def _sandbox_environment_block(sandbox: Sandbox) -> str:
    """Build the '# Environment' block by probing the sandbox the agent's tools run in."""
    today = datetime.date.today().isoformat()
    lines = [f"- Date: {today}"]
    try:
        result = await sandbox.execute(_ENV_PROBE, timeout=15)
        out = result.stdout if result.exit_code == 0 else ""
    except Exception:  # noqa: BLE001 — best-effort: a probe failure must not break the turn
        out = ""

    fields = _parse_probe(out)
    if fields.get("cwd"):
        lines.append(f"- Working directory: {fields['cwd']}")
    if fields.get("uname"):
        lines.append(f"- System: {fields['uname']}")
    block = "# Environment (inside your sandbox)\n" + "\n".join(lines)
    if fields.get("branch"):
        status = fields.get("status") or "(clean)"
        git = f"Branch: {fields['branch']}\nStatus:\n{status}"
        if fields.get("log"):
            git += f"\nRecent commits:\n{fields['log']}"
        block += f"\n\ngitStatus (snapshot at conversation start):\n{git}"
    return block


def _parse_probe(out: str) -> dict[str, str]:
    """Parse the probe output: ``key=value`` lines plus ``key<<EOF … EOF`` heredoc blocks."""
    fields: dict[str, str] = {}
    lines = out.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<EOF" in line:
            key = line.split("<<EOF", 1)[0]
            body: list[str] = []
            i += 1
            while i < len(lines) and lines[i] != "EOF":
                body.append(lines[i])
                i += 1
            fields[key] = "\n".join(body).strip()
        elif "=" in line:
            key, _, value = line.partition("=")
            fields[key] = value.strip()
        i += 1
    return fields


class EventContext(Plugin):
    """Injects the sandbox environment block once, then re-surfaces todos on later turns."""

    name = "event_context"

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox
        self._env_injected = False
        super().__init__()

    @hook
    async def _on_invocation(self, event: BeforeInvocationEvent) -> None:
        if not event.messages:
            return
        last = event.messages[-1]
        if last.get("role") != "user":
            return

        additions: list[str] = []
        if not self._env_injected:
            additions.append(await _sandbox_environment_block(self._sandbox))
            self._env_injected = True

        todos = event.agent.state.get(TODO_STATE_KEY)
        if todos:
            additions.append(
                "<system-reminder>\nYour current todo list (keep it updated with the todo tool):\n"
                f"{render_todos(todos)}\n</system-reminder>"
            )

        if not additions:
            return
        prefix = "\n\n".join(additions)
        new_content = [{"text": prefix}, *last.get("content", [])]
        event.messages = [*event.messages[:-1], {**last, "content": new_content}]
