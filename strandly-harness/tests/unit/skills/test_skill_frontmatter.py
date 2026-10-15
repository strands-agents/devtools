"""Guard: every built-in SKILL.md's frontmatter complies with the Agent Skills spec.

The Agent Skills specification (https://agentskills.io/specification) constrains the
frontmatter: `name` (required, 1-64 lowercase alphanumerics/hyphens, must match the parent
directory), `description` (required, 1-1024 chars), optional
`compatibility` (max 500 chars), and optional `allowed-tools` as a *space-separated string*.
On top of the spec, strandly has house conventions: every description carries explicit
TRIGGER / SKIP guidance (the activate-vs-skip contract the agent routes on), and
`allowed-tools` only names tools the harness actually builds. The spec also recommends keeping SKILL.md under 500 lines
(progressive disclosure) — enforced here so a growing skill gets split into references/
instead of bloating the resident prompt block.
"""

from __future__ import annotations

import pytest
from strands.vended_plugins.skills import Skill

from strandly_harness.skills.loader import builtin_skills_dir

# Every tool name the harness can hand a skill (tools/build_tools + plugin-delivered).
_KNOWN_TOOLS = {"bash", "file_editor", "use_github", "think", "spawn", "skill", "todo"}

_MAX_DESCRIPTION = 1024  # spec: description field hard limit
_MAX_COMPATIBILITY = 500  # spec: compatibility field hard limit
_MAX_BODY_LINES = 500  # spec recommendation: keep SKILL.md under 500 lines


def _skill_dirs():
    root = builtin_skills_dir()
    dirs = sorted(p.parent for p in root.glob("*/SKILL.md"))
    assert dirs, "expected built-in skills"
    return dirs


@pytest.fixture(params=_skill_dirs(), ids=lambda p: p.name)
def skill_dir(request):
    return request.param


def test_frontmatter_parses_strict(skill_dir):
    """Strict spec parse: name format, name==directory, required fields present."""
    skill = Skill.from_file(skill_dir, strict=True)
    assert skill.name == skill_dir.name


def test_description_within_spec_limit_and_has_trigger_skip(skill_dir):
    skill = Skill.from_file(skill_dir)
    assert 0 < len(skill.description) <= _MAX_DESCRIPTION, (
        f"{skill.name}: description is {len(skill.description)} chars (spec max {_MAX_DESCRIPTION})"
    )
    # House convention: the description is all the agent sees before activating, so it must
    # carry the explicit activate/skip contract.
    assert "TRIGGER" in skill.description, f"{skill.name}: description missing TRIGGER guidance"
    assert "SKIP" in skill.description, f"{skill.name}: description missing SKIP guidance"


def test_compatibility_within_spec_limit(skill_dir):
    skill = Skill.from_file(skill_dir)
    if skill.compatibility is not None:
        assert 0 < len(skill.compatibility.strip()) <= _MAX_COMPATIBILITY, (
            f"{skill.name}: compatibility is {len(skill.compatibility)} chars "
            f"(spec max {_MAX_COMPATIBILITY})"
        )


def test_allowed_tools_are_known(skill_dir):
    """allowed-tools must be present and only name tools the harness actually builds."""
    skill = Skill.from_file(skill_dir)
    assert skill.allowed_tools, f"{skill.name}: allowed-tools missing or empty"
    unknown = set(skill.allowed_tools) - _KNOWN_TOOLS
    assert not unknown, f"{skill.name}: allowed-tools references unknown tools: {sorted(unknown)}"


def test_allowed_tools_is_space_separated_string(skill_dir):
    """The spec defines allowed-tools as a space-separated string, not a YAML list."""
    raw = (skill_dir / "SKILL.md").read_text()
    for line in raw.splitlines():
        if line.startswith("allowed-tools:"):
            value = line.split(":", 1)[1].strip()
            assert value and not value.startswith("["), (
                f"{skill_dir.name}: allowed-tools should be a space-separated string per the spec"
            )
            return
    pytest.fail(f"{skill_dir.name}: no allowed-tools line found")


def test_body_within_recommended_length(skill_dir):
    skill = Skill.from_file(skill_dir)
    n_lines = len(skill.instructions.splitlines())
    assert n_lines <= _MAX_BODY_LINES, (
        f"{skill.name}: SKILL.md body is {n_lines} lines (recommended max {_MAX_BODY_LINES}); "
        "move detail into references/"
    )


def test_e2e_test_declares_compatibility():
    """e2e-test hard-requires the CI Bedrock role — the spec's exact compatibility use case."""
    skill = Skill.from_file(builtin_skills_dir() / "e2e-test")
    assert skill.compatibility and "Bedrock" in skill.compatibility
