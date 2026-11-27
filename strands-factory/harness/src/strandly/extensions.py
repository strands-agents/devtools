"""Two explicit, operator-selected hooks. No automatic addon discovery.

Skill providers return a directory containing skill folders. A merged catalog always
carries the built-in ``_shared`` assets unchanged; a provider can neither supply that
directory nor replace a built-in skill. Deployment callbacks
receive the CDK app and Naming object, and must attach resources to their owning
stack without changing construct IDs. Missing configured extensions are fatal.
"""
from __future__ import annotations

import atexit
import functools
import importlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

#: Assets several built-in skills reference (``skills/_shared/...``); it holds no ``SKILL.md``.
SHARED_SKILLS_DIR = "_shared"


def load_extension(reference: str) -> Any:
    module, separator, attribute = reference.partition(":")
    if not separator or not module or not attribute:
        raise ValueError(f"Extension must be module:attribute: {reference!r}")
    try:
        return getattr(importlib.import_module(module), attribute)
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(f"Configured extension {reference!r} is unavailable; install its provider") from exc


def selected_skill_packs() -> tuple[str, ...]:
    defaults = json.loads(os.environ.get("STRANDLY_PROFILE_DEFAULTS", "{}"))
    path = os.environ.get("STRANDLY_PROFILE")
    if path:
        from strandly.profiles import Profile
        defaults.update(Profile.load(path).runtime_values())
    value = os.environ.get("STRANDLY_SKILL_PACKS", defaults.get("STRANDLY_SKILL_PACKS", ""))
    return tuple(item.strip() for item in value.split(",") if item.strip())


@functools.cache
def skills_source_dir(builtin: Path, providers: tuple[str, ...]) -> Path:
    if not providers:
        return builtin
    root = Path(tempfile.mkdtemp(prefix="strandly-skills-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    names: set[str] = set()
    try:
        for source in (builtin, *(Path(load_extension(ref)()) for ref in providers)):
            if not source.is_dir():
                raise ValueError(f"Configured skill directory is missing: {source}")
            for skill in sorted(source.iterdir()):
                if skill.name == SHARED_SKILLS_DIR:
                    # Built-in skills reference these files; only the built-in catalog owns them.
                    if source is not builtin:
                        raise ValueError(f"Skill pack cannot provide {SHARED_SKILLS_DIR}: it is reserved for built-in shared assets")
                elif not (skill / "SKILL.md").is_file():
                    continue
                destination = root / skill.name
                if destination.exists():
                    raise ValueError(f"Skill pack cannot shadow existing skill: {skill.name}")
                if any(p.is_symlink() for p in skill.rglob("*")) or skill.is_symlink():
                    raise ValueError(f"Skill pack must not contain symlinks: {skill.name}")
                if skill.name != SHARED_SKILLS_DIR:
                    # The plugin resolves skills (and uploads their files) by front-matter name.
                    declared = _declared_name(skill, strict=source is not builtin)
                    if source is not builtin and declared != skill.name:
                        raise ValueError(f"Skill pack skill {skill.name} declares name {declared!r}; it must match its directory")
                    if declared in names:
                        raise ValueError(f"Skill pack cannot shadow existing skill: {declared}")
                    names.add(declared)
                shutil.copytree(skill, destination)
        return root
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _declared_name(skill: Path, *, strict: bool) -> str:
    try:
        return _front_matter_name(skill / "SKILL.md")
    except Exception:  # noqa: BLE001 — any parse failure is an unreadable skill
        if strict:
            raise ValueError(f"Skill pack skill {skill.name} has an unreadable SKILL.md") from None
        return skill.name  # the loader skips an unreadable built-in


def _front_matter_name(path: Path) -> str:
    """The ``name`` in a SKILL.md front matter block, without the Strands SDK.

    Lambdas (the Schedules API lists skill names) ship without the SDK, so this reads the block
    directly: the leading ``---`` fence through the next one, first ``name:`` key.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("no front matter")
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() == "name":
            name = value.strip().strip("\"'")
            if name:
                return name
    raise ValueError("no name in front matter")


def apply_deployment_extensions(references: tuple[str, ...], app: Any, naming: Any) -> None:
    for reference in references:
        load_extension(reference)(app, naming)
