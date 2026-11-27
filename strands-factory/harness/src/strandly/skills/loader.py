"""Built-in skills as ``Skill`` objects for the harness's ``AgentSkills`` plugin.

Each subdirectory here with a ``SKILL.md`` is a skill. They are parsed from the package once per
process (host reads only), so building an agent does no sandbox I/O. The model sees each skill's
name and description in ``<available_skills>`` and loads the full instructions with the ``skills``
tool.

Every skill folder, whole, plus ``_shared/``, belongs on the sandbox's filesystem. On a lazy sandbox
(one with ``add_first_use_hook``) they are pushed into :data:`SANDBOX_SKILLS_DIR` by a first-use
hook — once per sandbox object, after its wake — and each skill's ``path`` names its folder there. A
failed push is retried once and reported to the model as a sandbox notice. On any other sandbox (the
local one) ``path`` is the package dir itself.
"""

from __future__ import annotations

import contextlib
import functools
import io
import logging
import secrets
import shlex
import tarfile
import weakref
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from strandly.core.constants import SANDBOX_SKILLS_DIR
from strandly.extensions import SHARED_SKILLS_DIR
from strandly.sandbox.select import supports_first_use_hooks

if TYPE_CHECKING:
    from strands.vended_plugins.skills import AgentSkills, Skill

logger = logging.getLogger(__name__)


class _DropReinjectWarning(logging.Filter):
    """Drops AgentSkills' "unable to find previously injected skills XML" warning.

    It fires once whenever an agent is rebuilt for a saved session: the restored plugin state names
    a skills block the freshly composed prompt doesn't hold, and the plugin re-appends it correctly.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "previously injected skills XML" not in record.getMessage()


logging.getLogger("strands.vended_plugins.skills.agent_skills").addFilter(_DropReinjectWarning())

#: Snippets several skills reference (``skills/_shared/...``); pushed alongside them.
_SHARED_DIR = SHARED_SKILLS_DIR
_EXCLUDED_DIR_NAMES = frozenset({"__pycache__"})
# Read at call time (tests point it at a temp dir).
_SANDBOX_SKILLS_DIR = SANDBOX_SKILLS_DIR

#: Extra push attempts after a failed one (each runs as a new first-use hook).
_PUSH_RETRIES = 1

# Sandboxes whose push is already registered: subagents and the critic share their parent's.
_PUSH_REGISTERED: weakref.WeakKeyDictionary[Any, Path] = weakref.WeakKeyDictionary()


def builtin_skills_dir() -> Path:
    """Absolute path to the packaged built-in skills directory (this package's own dir)."""
    from strandly.extensions import selected_skill_packs, skills_source_dir
    return skills_source_dir(Path(__file__).resolve().parent, selected_skill_packs())


def _files_under(root: Path) -> list[Path]:
    return [
        f
        for f in sorted(root.rglob("*"))
        if f.is_file() and not _EXCLUDED_DIR_NAMES & set(f.relative_to(root).parts)
    ]


def _packaged_skills(root: Path | None = None) -> tuple[Skill, ...]:
    """Normalize the selected root before caching; environment-selected packs cannot share None."""
    return _read_packaged_skills((root or builtin_skills_dir()).resolve())


@functools.cache
def _read_packaged_skills(root: Path) -> tuple[Skill, ...]:
    """Every skill in this specific catalog, parsed from its own directory."""
    from strands.vended_plugins.skills import Skill

    skills: list[Skill] = []
    for skill_dir in sorted((root or builtin_skills_dir()).iterdir()):
        if not (skill_dir / "SKILL.md").is_file():
            continue
        try:
            skills.append(Skill.from_file(skill_dir))
        except Exception as e:  # noqa: BLE001 — one bad skill must not take the others down
            logger.warning("skill_dir=<%s>, error=<%s> | skipping unreadable built-in skill", skill_dir, e)
    return tuple(skills)


def builtin_skills(sandbox: Any, root: Path | None = None) -> list[Skill]:
    """The built-in skills for an agent over ``sandbox``, with ``path`` set for that sandbox.

    Registers the skill-folder push on a lazy sandbox (once per sandbox object). No sandbox I/O.
    """
    root = (root or builtin_skills_dir()).resolve()
    lazy = supports_first_use_hooks(sandbox)
    packaged = _packaged_skills(root)
    skills = [
        replace(skill, path=Path(_SANDBOX_SKILLS_DIR) / skill.name if lazy else skill.path) for skill in packaged
    ]
    if lazy and packaged:
        _register_push(sandbox, root=root)
    return skills


def build_skills_plugin(sandbox: Any, root: Path | None = None) -> AgentSkills:
    """The harness's ``AgentSkills`` plugin over :func:`builtin_skills` for ``sandbox``."""
    from strands.vended_plugins.skills import AgentSkills

    sources: list[str | Path | Skill] = list(builtin_skills(sandbox, root))
    return AgentSkills(skills=sources)


def _register_push(sandbox: Any, *, root: Path | None = None, retries_left: int = _PUSH_RETRIES) -> None:
    try:
        selected = (root or builtin_skills_dir()).resolve()
        previous = _PUSH_REGISTERED.get(sandbox)
        if previous is not None:
            if previous != selected:
                raise ValueError("A sandbox cannot share skill catalogs across profiles; use a fresh sandbox")
            return
        _PUSH_REGISTERED[sandbox] = selected
    except TypeError:  # not weak-referenceable: register per build (a repeat push is harmless)
        pass

    async def _push_skill_files() -> None:
        try:
            await push_skill_files(sandbox, root=root)
        except Exception:
            retry = retries_left > 0
            _notify(
                sandbox,
                f"[strandly-skills] Copying the built-in skill files to {_SANDBOX_SKILLS_DIR} failed, "
                "so scripts and references under a skill's path may be missing"
                + ("; retrying once before the next command." if retry else "."),
            )
            if retry:
                with contextlib.suppress(TypeError):
                    _PUSH_REGISTERED.pop(sandbox, None)
                _register_push(sandbox, root=root, retries_left=retries_left - 1)
            raise
        if retries_left < _PUSH_RETRIES:
            _notify(sandbox, f"[strandly-skills] The retry worked: the skill files are in {_SANDBOX_SKILLS_DIR}.")

    sandbox.add_first_use_hook(_push_skill_files)


def _notify(sandbox: Any, notice: str) -> None:
    add_notice = getattr(sandbox, "_add_notice", None)
    if callable(add_notice):
        add_notice(notice)


def _push_payload(root: Path | None = None) -> list[tuple[str, bytes]]:
    """``(posix path relative to the skills root, bytes)`` for every folder the push carries."""
    root = (root or builtin_skills_dir()).resolve()
    dirs = [s.path for s in _packaged_skills(root) if s.path is not None]
    if (root / _SHARED_DIR).is_dir():
        dirs.append(root / _SHARED_DIR)
    return [(f.relative_to(root).as_posix(), f.read_bytes()) for d in dirs for f in _files_under(d)]


def _archive(files: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for rel, content in files:
            info = tarfile.TarInfo(name=rel)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


async def push_skill_files(sandbox: Any, dest: str | None = None, *, root: Path | None = None) -> None:
    """Unpack every skill folder into ``dest`` in the sandbox (one write + one command)."""
    dest = dest or _SANDBOX_SKILLS_DIR
    files = _push_payload(root)
    if not files:
        return
    archive = f"{dest.rsplit('/', 1)[0]}/skills-{secrets.token_hex(6)}.tgz"
    await sandbox.write_file(archive, _archive(files))
    quoted_dest, quoted_archive = shlex.quote(dest), shlex.quote(archive)
    # Replace each pushed folder whole, so files removed from the package do not linger on a kept volume.
    stale = " ".join(shlex.quote(f"{dest}/{top}") for top in sorted({rel.split("/", 1)[0] for rel, _ in files}))
    result = await sandbox.execute(
        f"rm -rf {stale} && mkdir -p {quoted_dest} && tar -xzf {quoted_archive} -C {quoted_dest}; "
        f"rc=$?; rm -f {quoted_archive}; exit $rc"
    )
    if result.exit_code != 0:
        raise RuntimeError(
            f"unpacking skill files into {dest} failed (exit {result.exit_code}): "
            f"{(result.stderr or result.stdout or '').strip()[:500]}"
        )
    logger.info("dest=<%s>, files=<%d> | pushed skill files to the sandbox", dest, len(files))
