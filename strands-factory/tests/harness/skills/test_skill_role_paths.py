"""Guard: every role/prompt path or role name a SKILL.md references must actually resolve.

A stale `skills/code-review/<role>.md` path (the layout moved to `assets/roles/`) leaves the agent
with no role file to read and pass to `subagent`, so it quietly runs the pass itself. This test walks every
built-in skill's SKILL.md, extracts each `skills/<name>/...md` reference, and asserts the file
exists on disk. It would have caught that bug.
"""

from __future__ import annotations

import re

from strandly.skills.loader import builtin_skills_dir
from strandly.tools.subagent import load_roles

# Matches a packaged skill prompt reference: skills/<skill>/<...>.md (role files or root-level ones).
_REF = re.compile(r"skills/([a-z0-9-]+)/((?:assets/roles/|references/)?[a-z0-9-]+\.md)")


def test_all_skill_md_role_paths_resolve():
    root = builtin_skills_dir()
    missing: list[str] = []
    checked = 0
    for skill_md in sorted([*root.glob("*/SKILL.md"), *root.glob("*/assets/roles/*.md")]):
        text = skill_md.read_text()
        for m in _REF.finditer(text):
            rel = m.group(0)[len("skills/") :]
            checked += 1
            if not (root / rel).is_file():
                missing.append(f"{skill_md.relative_to(root)} -> {m.group(0)}")
    assert checked > 0, "expected SKILL.md files to reference role paths"
    assert not missing, "SKILL.md references a role/prompt file that does not exist:\n" + "\n".join(
        missing
    )


def test_no_stale_flat_role_paths():
    """No SKILL.md may reference a role file via the pre-`assets/roles/` flat path.

    A flat `skills/code-review/reviewer.md` (vs `skills/code-review/assets/roles/reviewer.md`) is the
    exact stale-path bug. `brief`'s root-level `writer.md` is the one intentional flat prompt, so it
    is allowlisted.
    """
    root = builtin_skills_dir()
    _ALLOWED_FLAT = {("brief", "writer.md")}
    offenders: list[str] = []
    for skill_md in sorted(root.glob("*/SKILL.md")):
        for m in _REF.finditer(skill_md.read_text()):
            skill, tail = m.group(1), m.group(2)
            is_flat = "/" not in tail  # no assets/roles/ or references/ prefix
            if is_flat and (skill, tail) not in _ALLOWED_FLAT:
                offenders.append(f"{skill_md.relative_to(root)} -> {m.group(0)}")
    assert not offenders, (
        "stale flat role path(s) — role files live under assets/roles/:\n" + "\n".join(offenders)
    )


# A role named for `subagent`: "the `<skill>/<role>` role" or `agent_type="<skill>/<role>"`, or a
# row of code-review's routing table.
_ROLE_NAME = re.compile(
    r'(?:the `([a-z0-9-]+/[a-z0-9-]+)` role|agent_type="([a-z0-9-]+/[a-z0-9-]+)"|\| `([a-z0-9-]+/[a-z0-9-]+)` \|)'
)


def test_every_role_name_a_skill_uses_is_an_agent_type():
    root = builtin_skills_dir()
    roles = load_roles(root)
    named: set[str] = set()
    for skill_md in sorted(root.glob("*/SKILL.md")):
        named |= {next(g for g in m.groups() if g) for m in _ROLE_NAME.finditer(skill_md.read_text())}
    assert named, "expected SKILL.md files to name roles"
    assert not named - set(roles), f"SKILL.md names roles that do not exist: {sorted(named - set(roles))}"
