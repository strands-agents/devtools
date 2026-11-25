"""The built-in skills as harness ``Skill`` objects: paths per sandbox, the first-use push, and the
``skills`` tool returning a skill's whole instructions through the agent's context manager."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from strands.sandbox.not_a_sandbox_local_environment import NotASandboxLocalEnvironment

from scripted_model import ScriptedModel, text_events, tool_use_events
from strandly.core.config import Config
from strandly.core.context import RuntimeContext
from strandly.extensions import skills_source_dir
from strandly.skills import loader
from strandly.skills.loader import builtin_skills, builtin_skills_dir, push_skill_files


def _skill_dirs() -> list[Path]:
    return sorted(p for p in builtin_skills_dir().iterdir() if (p / "SKILL.md").is_file())


class LazyHostSandbox:
    """A lazy sandbox (``add_first_use_hook``) whose commands run in a host shell."""

    def __init__(self) -> None:
        self.hooks: list[Any] = []
        self.commands: list[str] = []
        self.notices: list[str] = []

    def _add_notice(self, notice: str) -> None:
        self.notices.append(notice)

    def add_first_use_hook(self, hook: Any) -> None:
        self.hooks.append(hook)

    async def write_file(self, path: str, content: bytes) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(content)

    async def execute(self, command: str) -> Any:
        self.commands.append(command)
        done = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
        return SimpleNamespace(exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"building must not touch the sandbox ({name})")


@pytest.fixture
def sandbox_dest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    dest = tmp_path / "workspace" / ".strandly" / "skills"
    monkeypatch.setattr(loader, "_SANDBOX_SKILLS_DIR", str(dest))
    return dest


def test_every_built_in_skill_is_loaded():
    names = {s.name for s in builtin_skills(NotASandboxLocalEnvironment())}
    assert names == {d.name for d in _skill_dirs()}


def test_local_sandbox_paths_are_the_package_and_nothing_is_registered():
    by_name = {s.name: s for s in builtin_skills(NotASandboxLocalEnvironment())}
    for skill_dir in _skill_dirs():
        assert by_name[skill_dir.name].path == skill_dir, skill_dir.name


def test_lazy_sandbox_paths_point_into_the_sandbox_and_one_push_is_registered(sandbox_dest):
    sandbox = LazyHostSandbox()
    first = {s.name: s for s in builtin_skills(sandbox)}
    builtin_skills(sandbox)  # a subagent over the parent's sandbox
    assert len(sandbox.hooks) == 1
    assert sandbox.commands == []
    for skill_dir in _skill_dirs():
        assert first[skill_dir.name].path == sandbox_dest / skill_dir.name, skill_dir.name
    # The packaged Skill objects are not mutated by a per-sandbox build.
    local = {s.name: s for s in builtin_skills(NotASandboxLocalEnvironment())}
    assert local["bug-verify"].path == builtin_skills_dir() / "bug-verify"


@pytest.mark.asyncio
async def test_the_first_use_hook_pushes_every_skill_folder_whole(sandbox_dest):
    sandbox = LazyHostSandbox()
    builtin_skills(sandbox)
    await sandbox.hooks[0]()

    assert (sandbox_dest / "bug-verify" / "assets" / "roles" / "reproducer.md").read_bytes() == (
        builtin_skills_dir() / "bug-verify" / "assets" / "roles" / "reproducer.md"
    ).read_bytes()
    assert (sandbox_dest / "_shared" / "work-product-emit.md").is_file()
    for skill_dir in _skill_dirs():
        for f in skill_dir.rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts:
                pushed = sandbox_dest / f.relative_to(builtin_skills_dir())
                assert pushed.read_bytes() == f.read_bytes(), pushed
    assert not list(sandbox_dest.parent.glob("skills-*.tgz"))  # the archive is cleaned up
    assert len(sandbox.commands) == 1


@pytest.mark.asyncio
async def test_a_failed_unpack_raises_for_the_hook_runner_to_log(sandbox_dest):
    sandbox = LazyHostSandbox()
    sandbox_dest.parent.mkdir(parents=True)
    sandbox_dest.write_text("a file where the directory should be")
    with pytest.raises(RuntimeError, match="unpacking skill files"):
        await push_skill_files(sandbox)


@pytest.mark.asyncio
async def test_the_push_replaces_pushed_folders_and_leaves_others(sandbox_dest):
    (sandbox_dest / "bug-verify").mkdir(parents=True)
    (sandbox_dest / "bug-verify" / "removed-from-package.py").write_text("stale")
    (sandbox_dest / "_shared").mkdir()
    (sandbox_dest / "_shared" / "old.md").write_text("stale")
    (sandbox_dest / "not-ours").mkdir()
    (sandbox_dest / "not-ours" / "keep.txt").write_text("kept")

    await push_skill_files(LazyHostSandbox())

    assert not (sandbox_dest / "bug-verify" / "removed-from-package.py").exists()
    assert not (sandbox_dest / "_shared" / "old.md").exists()
    assert (sandbox_dest / "bug-verify" / "assets" / "roles" / "reproducer.md").is_file()
    assert (sandbox_dest / "not-ours" / "keep.txt").read_text() == "kept"


def test_the_push_carries_only_skill_folders_and_shared():
    tops = {rel.split("/", 1)[0] for rel, _ in loader._push_payload()}
    assert tops == {d.name for d in _skill_dirs()} | {"_shared"}
    assert not any("/" not in rel or rel.endswith(".pyc") for rel, _ in loader._push_payload())


def test_the_push_carries_every_skill_md_and_goals_md():
    payload = dict(loader._push_payload())
    for skill_dir in _skill_dirs():
        assert payload[f"{skill_dir.name}/SKILL.md"] == (skill_dir / "SKILL.md").read_bytes()
        if (skill_dir / "GOALS.md").is_file():
            assert f"{skill_dir.name}/GOALS.md" in payload


_PUBLIC_SKILLS = Path(loader.__file__).resolve().parent
_SHARED_CONTRACT = "_shared/work-product-emit.md"


def _skill_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, tuple[str, ...]]:
    """A synthetic operator-selected pack with one file-shipping skill, as an importable provider."""
    pack = tmp_path / "pack"
    (pack / "addon-skill" / "scripts").mkdir(parents=True)
    (pack / "addon-skill" / "SKILL.md").write_text(
        "---\nname: addon-skill\ndescription: Test skill\n---\nInstructions\n"
    )
    (pack / "addon-skill" / "scripts" / "run.py").write_text("print('addon')\n")
    # One module per test: skills_source_dir caches on the provider reference.
    module = ModuleType(f"synthetic_skill_pack_{tmp_path.name}")
    module.skills_dir = lambda: pack  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return pack, (f"{module.__name__}:skills_dir",)


def test_a_skill_pack_catalog_keeps_every_built_in_pushed_file(tmp_path, monkeypatch):
    _, providers = _skill_pack(tmp_path, monkeypatch)
    built_in = dict(loader._push_payload(_PUBLIC_SKILLS))
    merged = dict(loader._push_payload(skills_source_dir(_PUBLIC_SKILLS, providers)))

    assert _SHARED_CONTRACT in built_in
    assert sorted(set(built_in) - set(merged)) == []
    assert [rel for rel, content in built_in.items() if merged[rel] != content] == []
    assert merged["addon-skill/scripts/run.py"] == b"print('addon')\n"


@pytest.mark.asyncio
async def test_a_fresh_push_from_a_skill_pack_catalog_delivers_the_shared_contract(
    sandbox_dest, tmp_path, monkeypatch
):
    _, providers = _skill_pack(tmp_path, monkeypatch)
    sandbox = LazyHostSandbox()
    builtin_skills(sandbox, skills_source_dir(_PUBLIC_SKILLS, providers))
    await sandbox.hooks[0]()

    assert (sandbox_dest / _SHARED_CONTRACT).read_bytes() == (
        _PUBLIC_SKILLS / _SHARED_CONTRACT
    ).read_bytes()
    assert (sandbox_dest / "addon-skill" / "scripts" / "run.py").read_text() == "print('addon')\n"


@pytest.mark.parametrize("with_skill_file", [False, True])
def test_a_skill_pack_cannot_provide_the_reserved_shared_directory(
    with_skill_file, tmp_path, monkeypatch
):
    pack, providers = _skill_pack(tmp_path, monkeypatch)
    (pack / "_shared").mkdir()
    (pack / "_shared" / "work-product-emit.md").write_text("replaced contract")
    if with_skill_file:
        (pack / "_shared" / "SKILL.md").write_text(
            "---\nname: _shared\ndescription: Test skill\n---\nInstructions\n"
        )
    with pytest.raises(ValueError, match="reserved for built-in shared assets"):
        skills_source_dir(_PUBLIC_SKILLS, providers)


def test_a_skill_pack_cannot_link_the_reserved_shared_directory(tmp_path, monkeypatch):
    pack, providers = _skill_pack(tmp_path, monkeypatch)
    (pack / "_shared").symlink_to(pack / "addon-skill", target_is_directory=True)
    with pytest.raises(ValueError, match="reserved for built-in shared assets"):
        skills_source_dir(_PUBLIC_SKILLS, providers)


def test_a_skill_pack_cannot_shadow_a_built_in_skill(tmp_path, monkeypatch):
    pack, providers = _skill_pack(tmp_path, monkeypatch)
    (pack / "code-review").mkdir()
    (pack / "code-review" / "SKILL.md").write_text(
        "---\nname: code-review\ndescription: Test skill\n---\nReplaced\n"
    )
    with pytest.raises(ValueError, match="cannot shadow existing skill: code-review"):
        skills_source_dir(_PUBLIC_SKILLS, providers)


@pytest.mark.parametrize("linked", ["file", "directory", "skill"])
def test_a_skill_pack_cannot_contain_symlinks(linked, tmp_path, monkeypatch):
    pack, providers = _skill_pack(tmp_path, monkeypatch)
    shared = _PUBLIC_SKILLS / "_shared"
    if linked == "file":
        (pack / "addon-skill" / "emit.md").symlink_to(shared / "work-product-emit.md")
    elif linked == "directory":
        (pack / "addon-skill" / "shared").symlink_to(shared, target_is_directory=True)
    else:
        (pack / "linked-skill").symlink_to(pack / "addon-skill", target_is_directory=True)
    with pytest.raises(ValueError, match="must not contain symlinks"):
        skills_source_dir(_PUBLIC_SKILLS, providers)


@pytest.mark.asyncio
async def test_a_failed_push_is_reported_and_retried_once(sandbox_dest):
    sandbox = LazyHostSandbox()
    sandbox_dest.parent.mkdir(parents=True)
    sandbox_dest.write_text("a file where the directory should be")
    builtin_skills(sandbox)

    with pytest.raises(RuntimeError, match="unpacking skill files"):
        await sandbox.hooks[0]()
    assert len(sandbox.hooks) == 2, "one retry is armed"
    assert "retrying once" in sandbox.notices[-1]

    with pytest.raises(RuntimeError, match="unpacking skill files"):
        await sandbox.hooks[1]()
    assert len(sandbox.hooks) == 2, "no second retry"
    assert len(sandbox.notices) == 2
    assert "may be missing." in sandbox.notices[-1]
    builtin_skills(sandbox)  # a later build over the same sandbox does not re-register
    assert len(sandbox.hooks) == 2


@pytest.mark.asyncio
async def test_a_successful_retry_is_reported(sandbox_dest):
    sandbox = LazyHostSandbox()
    sandbox_dest.parent.mkdir(parents=True)
    sandbox_dest.write_text("a file where the directory should be")
    builtin_skills(sandbox)
    with pytest.raises(RuntimeError):
        await sandbox.hooks[0]()

    sandbox_dest.unlink()
    await sandbox.hooks[1]()
    assert (sandbox_dest / "bug-verify" / "assets" / "roles" / "reproducer.md").is_file()
    assert "retry worked" in sandbox.notices[-1]


def _largest_skill() -> tuple[str, str]:
    from strands.vended_plugins.skills import Skill

    skills = [Skill.from_file(d) for d in _skill_dirs()]
    largest = max(skills, key=lambda s: len(s.instructions))
    return largest.name, largest.instructions


def _tool_results(agent: Any) -> list[str]:
    return [
        json.dumps(block["toolResult"]["content"])
        for message in agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]


@pytest.mark.asyncio
async def test_activating_the_largest_skill_returns_its_full_text(tmp_path):
    from strandly.core.agent import build_agent

    name, instructions = _largest_skill()
    assert len(instructions) > 4 * 1_500  # over the context manager's truncation threshold
    big = "x" * len(instructions)
    model = ScriptedModel(
        tool_use_events("skills", "tu-skill", json.dumps({"skill_name": name})),
        tool_use_events("shell", "tu-shell", json.dumps({"command": f"printf '{big}'"})),
        text_events("done"),
    )
    agent = await build_agent(
        Config(values={}),
        RuntimeContext(cwd=str(tmp_path)),
        model=model,
        sandbox=NotASandboxLocalEnvironment(),
        spawn_depth=1,  # no goal-loop critic to script
        mcp_clients=[],
    )
    await agent.invoke_async("go")

    skill_result, shell_result = _tool_results(agent)
    assert json.dumps(instructions)[1:-1] in skill_result
    # The exemption is specific to `skills`: an equally large shell result is cut to a preview.
    assert big not in shell_result


@pytest.mark.asyncio
async def test_goals_md_stays_out_of_the_actors_prompt_and_skill_results(tmp_path):
    from strandly.core.agent import build_agent

    with_goals = [d for d in _skill_dirs() if (d / "GOALS.md").is_file()]
    assert with_goals
    model = ScriptedModel(
        *(
            tool_use_events("skills", f"tu-{d.name}", json.dumps({"skill_name": d.name}))
            for d in with_goals
        ),
        text_events("done"),
    )
    agent = await build_agent(
        Config(values={}),
        RuntimeContext(cwd=str(tmp_path)),
        model=model,
        sandbox=NotASandboxLocalEnvironment(),
        spawn_depth=1,  # no goal-loop critic to script
        mcp_clients=[],
    )
    await agent.invoke_async("go")

    results = _tool_results(agent)
    assert len(results) == len(with_goals)
    seen = "\n".join([str(agent.system_prompt), *results])
    assert "GOALS" not in seen
    for d in with_goals:
        first_line = next(line for line in (d / "GOALS.md").read_text().splitlines() if line.strip())
        assert first_line not in seen, d.name


def test_only_the_reinject_warning_is_dropped(caplog):
    import logging

    import strandly.skills.loader  # noqa: F401  (installs the filter)

    plugin_logger = logging.getLogger("strands.vended_plugins.skills.agent_skills")
    with caplog.at_level(logging.WARNING, logger=plugin_logger.name):
        plugin_logger.warning("unable to find previously injected skills XML in system prompt, re-appending")
        plugin_logger.warning("path=<%s> | failed to load skill: %s", "/x", "boom")
    messages = [r.getMessage() for r in caplog.records if r.name == plugin_logger.name]
    assert messages == ["path=</x> | failed to load skill: boom"]


@pytest.mark.parametrize(("directory", "declared", "error"), [
    ("zz-alias", "code-review", "declares name 'code-review'; it must match its directory"),
    ("zz-other", "addon-skill", "declares name 'addon-skill'; it must match its directory"),
])
def test_a_skill_pack_skill_is_named_by_its_directory(directory, declared, error, tmp_path, monkeypatch):
    pack, providers = _skill_pack(tmp_path, monkeypatch)
    (pack / directory).mkdir()
    (pack / directory / "SKILL.md").write_text(f"---\nname: {declared}\ndescription: Test skill\n---\nReplaced\n")
    (pack / directory / "tool.py").write_text("print('alias')\n")
    with pytest.raises(ValueError, match=error):
        skills_source_dir(_PUBLIC_SKILLS, providers)


def test_a_skill_pack_skill_with_an_unreadable_skill_md_is_rejected(tmp_path, monkeypatch):
    pack, providers = _skill_pack(tmp_path, monkeypatch)
    (pack / "broken").mkdir()
    (pack / "broken" / "SKILL.md").write_text("no front matter\n")
    with pytest.raises(ValueError, match="broken has an unreadable SKILL.md"):
        skills_source_dir(_PUBLIC_SKILLS, providers)


def test_skill_names_are_read_without_the_strands_sdk(monkeypatch, tmp_path):
    """The Schedules API Lambda lists skill names and ships without the SDK."""
    import builtins
    from importlib.resources import files

    from strandly.extensions import _front_matter_name

    real_import = builtins.__import__

    def no_strands(name, *args, **kwargs):
        if name == "strands" or name.startswith("strands."):
            raise ModuleNotFoundError("No module named 'strands'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_strands)
    builtin = Path(str(files("strandly").joinpath("skills")))
    for skill in sorted(p for p in builtin.iterdir() if (p / "SKILL.md").is_file()):
        assert _front_matter_name(skill / "SKILL.md") == skill.name
    (tmp_path / "SKILL.md").write_text("---\nname: 'quoted-name'\ndescription: x\n---\nbody\n")
    assert _front_matter_name(tmp_path / "SKILL.md") == "quoted-name"
