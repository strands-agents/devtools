"""Built-in harness tools — all sandbox-routed.

Every builtin performs its filesystem / execution access **through the active
:class:`~strands.sandbox.base.Sandbox`**, never through host ``pathlib`` / ``subprocess``
directly. This keeps the harness's promise: the configured isolation boundary (local / docker /
ssh / agentcore) is the *only* view a tool has of files and execution. A tool that read the host
filesystem while the agent ran against, say, a Docker or SSH sandbox would both return wrong
results (host view, not sandbox view) and leak host data across the boundary.

The set is deliberately small — ``bash`` + ``file_editor``, both from the SDK's sandbox-aware
factories (``strands.vended_tools``):

- ``file_editor`` covers reading (its ``view`` command renders line-numbered output and accepts
  line ranges, and lists directories) as well as ``create`` / ``str_replace`` / ``insert``. So a
  separate ``read`` tool is redundant.
- ``bash`` runs commands *inside* the sandbox, so finding files (``find``, ``ls``) and searching
  content (``rg`` / ``grep``) is just a shell call where the files live — no need for dedicated
  ``glob`` / ``grep`` tools.

``todo`` is NOT here: it pairs a tool with a re-surfacing hook, so it lives in ``TodoPlugin``
(``todo.py``) and is injected by ``build_agent``. ``spawn`` is likewise injected (needs config).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from strands.sandbox.base import Sandbox


def make_builtins(sandbox: Sandbox) -> dict[str, Any]:
    """Return the registry of built-in tools by short name, all bound to ``sandbox``.

    Every builtin routes file/exec access through ``sandbox`` — the single isolation boundary the
    harness configures. ``todo`` is not here (it lives in ``TodoPlugin`` — it pairs a tool with a
    re-surfacing hook) and ``spawn`` is injected by ``build_agent`` (needs config/ctx).
    """
    from strands.vended_tools.bash import make_bash
    from strands.vended_tools.file_editor import make_file_editor

    return {
        "bash": make_bash(sandbox=sandbox),
        "file_editor": make_file_editor(sandbox=sandbox),
    }
