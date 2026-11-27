"""Distribution entry points and packaged resources use the public Python namespace."""

from importlib import metadata, resources, util


def test_distribution_entry_points():
    dist = metadata.distribution("strands-factory")
    assert dist.metadata["Name"] == "strands-factory"
    entry_points = {entry.name: entry for entry in dist.entry_points}
    for command in ("strands-factory", "strandly"):
        assert entry_points[command].value == "strandly.serve.cli.main:main"
        assert callable(entry_points[command].load())
    for command in ("strands-factory-package", "strandly-package"):
        assert entry_points[command].value == "strandly.deployment:main"
    assert util.find_spec("strandly" + "_harness") is None
    thin_entry = next(entry for entry in metadata.distribution("strandly-mcp").entry_points
                      if entry.name == "strandly-mcp")
    assert thin_entry.value == "strandly_mcp.cli:main"


def test_prompt_and_skill_resources():
    root = resources.files("strandly")
    assert root.joinpath("py.typed").is_file()
    assert root.joinpath("core/prompt/global_prompt.md").read_text().strip()
    skills = root.joinpath("skills")
    skill_files = [directory.joinpath("SKILL.md") for directory in skills.iterdir()
                   if directory.is_dir() and directory.joinpath("SKILL.md").is_file()]
    assert skill_files
    assert all(skill.read_text().startswith("---") for skill in skill_files)
