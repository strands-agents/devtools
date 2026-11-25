"""Guard: every role's ``allowed-tools:`` frontmatter is a scope ``subagent`` can enforce.

A role file declares its capability scope in frontmatter (``allowed-tools: shell read …``);
``load_roles`` makes it the ``agent_type`` preset's tool scope and ``build_agent`` builds only those
tools, so a role's declared scope is what it actually gets. That only holds if the names are ones
the harness can gate, and a malformed block fails loading instead of widening the scope.

Also pinned: the roles whose *lack* of a tool is load-bearing — the judging roles get no
``write``/``edit`` for the code they judge, and the evidence roles get no ``use_github`` (the
orchestrator owns every public statement). A capability cut, not a jail: every scope keeps ``shell``.
"""

from __future__ import annotations

import pytest

from strandly.core.constants import (
    CONTEXT_TRUNCATE_THRESHOLD_TOKENS,
    SUBAGENT_SCOPABLE_TOOLS,
    SUBAGENT_UNGATED_TOOLS,
)
from strandly.skills.loader import builtin_skills_dir
from strandly.tools.subagent import load_roles

ROLES = load_roles(builtin_skills_dir())

# Roles that must not be able to mutate the code they judge.
_NO_WRITE_ROLES = [
    "code-review/reviewer",
    "code-review/api-bar-raiser",
    "triage/triage",
    "port/validator",
    "integration-review/auditor",
]

# Roles that must not be able to reach GitHub (the orchestrator owns every public statement).
_NO_GITHUB_ROLES = [
    "bug-verify/reproducer",
    "code-review/adversarial-tester",
    "port/validator",
]


def test_every_role_file_loads_as_a_preset():
    files = sorted(builtin_skills_dir().glob("*/assets/roles/*.md"))
    assert files, "expected built-in role prompts"
    assert sorted(ROLES) == sorted(f"{p.parts[-4]}/{p.stem}" for p in files)


@pytest.mark.parametrize("name", sorted(ROLES))
def test_declared_scope_uses_names_subagent_accepts(name):
    """Same acceptance rule as `subagent` itself: gateable names, plus the ungated plugin names."""
    unknown = set(ROLES[name].tools or ()) - SUBAGENT_SCOPABLE_TOOLS - SUBAGENT_UNGATED_TOOLS
    assert not unknown, f"{name}: allowed-tools names tools subagent does not accept: {sorted(unknown)}"


@pytest.mark.parametrize("name", sorted(ROLES))
def test_the_role_text_is_the_body_under_a_role_heading(name):
    """The scope declaration is metadata — it must not leak into the role's system prompt."""
    text = ROLES[name].instructions or ""
    assert "allowed-tools" not in text
    assert text.startswith("# Role: "), f"{name}: role prompt should start with a `# Role:` heading"


@pytest.mark.parametrize("name", _NO_WRITE_ROLES)
def test_review_roles_have_no_edit_tools(name):
    scope = set(ROLES[name].tools or ())
    assert scope, f"{name}: must declare allowed-tools (its read-only claim depends on it)"
    assert not ({"write", "edit"} & scope), f"{name}: a judging role gets no file-mutation tools"


@pytest.mark.parametrize("name", _NO_GITHUB_ROLES)
def test_evidence_roles_have_no_github_tool(name):
    scope = set(ROLES[name].tools or ())
    assert scope, f"{name}: must declare allowed-tools"
    assert "use_github" not in scope, f"{name}: only the orchestrator gets the GitHub tool"


# An agent that runs a role itself, or passes `brief/writer.md` as instructions, reads the file, so
# it must come back whole: a tool result over CONTEXT_TRUNCATE_THRESHOLD_TOKENS is cut to a
# preview. A conservative ~3.5 chars/token, with headroom for `read`'s line numbers and edits.
_ROLE_TOKEN_BUDGET = 1_100


def _instruction_files():
    root = builtin_skills_dir()
    top_level = {
        path
        for path in root.glob("*/*.md")
        if path.name not in {"SKILL.md", "GOALS.md"} and not path.parent.name.startswith("_")
    }
    return sorted(set(root.glob("*/assets/roles/*.md")) | top_level)


@pytest.mark.parametrize(
    "path", _instruction_files(), ids=lambda p: str(p.relative_to(builtin_skills_dir()))
)
def test_role_files_fit_in_one_untruncated_tool_result(path):
    assert _ROLE_TOKEN_BUDGET < CONTEXT_TRUNCATE_THRESHOLD_TOKENS
    tokens = len(path.read_text()) / 3.5
    assert tokens < _ROLE_TOKEN_BUDGET, (
        f"{path.name}: ~{tokens:.0f} tokens; trim it (or move reference material to references/) "
        f"so a read of it stays under the {CONTEXT_TRUNCATE_THRESHOLD_TOKENS}-token truncation"
    )
