"""Built-in skill *content* + the skills-plugin builder for the agent factory.

Strandly is opinionated: skills are **always** the built-in set shipped with the harness — there
is no host/sandbox/push_to configuration. Each built-in skill is a subdirectory here
(``code-review/``, ``triage/``, ...) holding a ``SKILL.md`` plus the system-prompt files those
skills hand to ``spawn``.

Delivery is **system-prompt injection** via :class:`~strandly_harness.plugins.SystemPromptSkills`
(in ``plugins/``), not the SDK's progressive ``AgentSkills`` tool-result mode: an *active* skill's
full instructions stay resident in the system prompt every turn (so they don't drift out of the
model's attention on long runs), while inactive skills show only name+description. The agent
toggles skills via the ``skill`` tool.

Skills are read **through the agent's sandbox** (``sandbox.list_files`` / ``sandbox.read_text``),
not the host. A ``local`` sandbox sees the packaged dir directly. A non-local sandbox (e.g.
AgentCore) cannot see the host package, so :func:`build_skills_plugin` pushes the built-in skills
into the sandbox first via the sandbox file API, then points the plugin at that location.
"""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from strands.sandbox.base import Sandbox

logger = logging.getLogger(__name__)

# Where built-in skills are pushed inside a non-local sandbox. RELATIVE on purpose: the AgentCore
# Code Interpreter rejects absolute paths ("/...") as path traversal and resolves relative paths
# against its session working directory, so a leading "/" here breaks the skills push entirely.
_SANDBOX_SKILLS_DIR = ".strandly/skills"


def builtin_skills_dir() -> Path:
    """Absolute path to the packaged built-in skills directory (this package's own dir)."""
    return Path(__file__).resolve().parent


def _is_local_sandbox(sandbox: Sandbox) -> bool:
    """True for the no-isolation local sandbox, whose filesystem == the host's."""
    return type(sandbox).__name__ == "NotASandboxLocalEnvironment"


async def push_skills_to_sandbox(sandbox: Sandbox, src_root: Path) -> str:
    """Copy the built-in skills dir into the sandbox under ``_SANDBOX_SKILLS_DIR``.

    Walks ``src_root`` and writes every file (binary-safe, so ``assets/`` and each skill's
    optional ``GOALS.md`` survive) to the destination via ``sandbox.write_file`` (which creates
    parent dirs). Returns the destination root. Must run *before* the skills plugin loads.
    """
    count = 0
    for file in sorted(src_root.rglob("*")):
        if not file.is_file():
            continue
        rel = file.relative_to(src_root)
        dest = str(PurePosixPath(_SANDBOX_SKILLS_DIR) / PurePosixPath(*rel.parts))
        await sandbox.write_file(dest, file.read_bytes())
        count += 1
    # INFO, not DEBUG: this is the deploy-critical step (a deployed runtime can't see the host
    # package, so skills only exist if this push lands). A zero count here means no skills will load.
    logger.info("src=<%s>, dest=<%s>, files=<%d> | pushed built-in skills to sandbox",
                src_root, _SANDBOX_SKILLS_DIR, count)
    return _SANDBOX_SKILLS_DIR


async def build_skills_plugin(sandbox: Sandbox) -> Any:
    """Build the ``SystemPromptSkills`` plugin over the built-in skills.

    For a local sandbox the packaged dir is used as-is; for a non-local sandbox the skills are
    pushed in first (the plugin reads them through the sandbox, where the host path doesn't exist).
    """
    from strandly_harness.plugins.system_prompt_skills import SystemPromptSkills

    src = builtin_skills_dir()
    if _is_local_sandbox(sandbox):
        skills_dir = str(src)
    else:
        skills_dir = await push_skills_to_sandbox(sandbox, src)
    return SystemPromptSkills([skills_dir])
