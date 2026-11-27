"""Keep the runtime and build tooling above the compromised-CA release floors."""

from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
FLOORS = {"uv": "0.13.4", "certifi": "2026.10.19"}


def test_locked_packages_exclude_compromised_ca_releases():
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    versions = {p["name"]: p["version"] for p in lock["package"]}
    for name, floor in FLOORS.items():
        assert Version(versions[name]) >= Version(floor), name


def test_runtime_requirements_reject_vulnerable_versions():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    declared = project["dependencies"] + project["optional-dependencies"]["mcp"]
    requirements = {r.name: r for r in map(Requirement, declared)}
    for name, vulnerable in {"uv": "0.13.3", "certifi": "2026.10.18"}.items():
        assert vulnerable not in requirements[name].specifier, name
        assert FLOORS[name] in requirements[name].specifier, name


def test_the_thin_client_dependency_is_pinned_to_the_workspace_release():
    """An index can never satisfy ``strandly-mcp`` with another distribution of that name."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    thin = tomllib.loads((ROOT / "cli" / "strandly-mcp" / "pyproject.toml").read_text())["project"]
    requirement = next(r for r in map(Requirement, project["dependencies"]) if r.name == "strandly-mcp")
    assert str(requirement.specifier) == f"=={thin['version']}"


def test_sandbox_image_uses_patched_trust_stores():
    dockerfile = (ROOT / "sandbox" / "Dockerfile.sandbox").read_text()
    uv_copy = next(
        line for line in dockerfile.splitlines()
        if line.startswith("COPY --from=ghcr.io/astral-sh/uv:")
    )
    version = uv_copy.split("uv:", 1)[1].split()[0]
    assert Version(version) >= Version(FLOORS["uv"])
    assert "pip install --no-cache-dir -r /app/requirements-sandbox.txt" in dockerfile
    requirements = {
        r.name: r for r in map(Requirement, (
            line for line in (ROOT / "sandbox" / "requirements-sandbox.txt").read_text().splitlines()
            if line and not line.startswith("#")
        ))
    }
    assert "2026.10.18" not in requirements["certifi"].specifier
    assert FLOORS["certifi"] in requirements["certifi"].specifier


def test_sandbox_base_uses_official_upstream_at_reviewed_digest():
    dockerfile = (ROOT / "sandbox" / "Dockerfile.sandbox").read_text()
    assert (
        "FROM docker.io/library/python:3.13-slim-bookworm@sha256:"
        "480c75f114c46f661eea2f337ef40672160f9989f7de0c385aa719e51c6e44da"
    ) in dockerfile


def test_sandbox_image_pip_installs_are_pinned():
    """Each `RUN pip install` names exact versions or a requirements file, never a bare package."""
    dockerfile = (ROOT / "sandbox" / "Dockerfile.sandbox").read_text()
    installs = [
        line.split("pip install", 1)[1].split()
        for line in dockerfile.splitlines()
        if line.startswith("RUN pip install")
    ]
    assert installs
    for args in installs:
        if "-r" in args:
            continue
        for package in (arg for arg in args if not arg.startswith("-")):
            specifier = str(Requirement(package).specifier)
            assert specifier.startswith("==") and "," not in specifier, package
