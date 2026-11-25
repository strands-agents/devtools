"""Shared test fixtures. All tests run with no AWS and no network."""

from __future__ import annotations

import atexit
import os
import pwd
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Callable  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402

# Importing the package runs sanitize_otel_env() before any strands/opentelemetry import, so the
# suite doesn't need OTEL_PROPAGATORS set on the command line. The strands import must stay AFTER
# it, so keep isort off here.
# isort: off
import strandly  # noqa: F401
from strands.models import Model  # noqa: E402

# isort: on


#: The developer's real home, from the password database rather than ``$HOME`` (which the fixture
#: below replaces). Only ever read, never written: the guards compare it before and after.
REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir

#: Files in the real home a test has historically rewritten (the sandbox git bootstrap runs
#: ``git config --global`` in a real shell). The session guard fails the run if any of them changes.
_GUARDED_HOME_FILES = (".gitconfig", ".git-credentials", ".config/git/config")


def _real_uv_dirs() -> dict[str, str]:
    """uv's cache and managed-Python dirs as resolved against the REAL home, keyed by their path
    relative to a home directory (``.cache/uv``, ``.local/share/uv/python``).

    A test that drives the docs MCP's ``uv tool run`` fallback starts uv with only an allow-listed
    environment, not ``UV_CACHE_DIR``, so with a fresh temp ``$HOME`` uv would re-resolve and
    re-download the server on every test (seconds each, over the network).
    :func:`_hermetic_home` symlinks these two dirs into the temp home to keep that as it was. They
    are package caches, not the identity/credential files the guard protects.
    """
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        return {}
    out: dict[str, str] = {}
    for rel, args in (
        (".cache/uv", ["cache", "dir"]),
        (".local/share/uv/python", ["python", "dir"]),
    ):
        try:
            done = subprocess.run(
                [uv, *args], capture_output=True, text=True, timeout=10, check=True
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if done.stdout.strip() and os.path.isdir(done.stdout.strip()):
            out[rel] = done.stdout.strip()
    return out


_UV_DIRS = _real_uv_dirs()


def _point_home_at(home: str, setenv: Callable[[str, str], None]) -> None:
    """Make ``home`` the only home and global git config any process can see.

    ``GIT_CONFIG_GLOBAL`` and ``XDG_CONFIG_HOME`` are set as well as ``HOME``, so an ambient
    ``GIT_CONFIG_GLOBAL`` (or an XDG git config) cannot redirect a ``git config --global`` write back
    to a real file; ``GIT_CONFIG_NOSYSTEM`` keeps the machine's ``/etc/gitconfig`` out of every git
    child. With no ``~/.aws`` in the temp home, botocore would otherwise probe the EC2
    instance-metadata endpoint (seconds of timeouts per client) for credentials and region.
    """
    setenv("HOME", home)
    setenv("GIT_CONFIG_GLOBAL", os.path.join(home, ".gitconfig"))
    setenv("XDG_CONFIG_HOME", os.path.join(home, ".config"))
    setenv("GIT_CONFIG_NOSYSTEM", "1")
    setenv("AWS_EC2_METADATA_DISABLED", "true")
    for rel, real in _UV_DIRS.items():
        link = os.path.join(home, rel)
        os.makedirs(os.path.dirname(link), exist_ok=True)
        if not os.path.lexists(link):
            os.symlink(real, link, target_is_directory=True)


def _set_process_env(name: str, value: str) -> None:
    os.environ[name] = value


#: A temp home for the whole pytest process, installed at import (before any test, fixture or
#: background thread exists) and never restored. :func:`_hermetic_home` gives each test its own home
#: on top, but ``monkeypatch`` undoes that at teardown; a thread a test left running (a cancelled
#: turn's sandbox wake, whose git bootstrap runs ``git config --global`` in a host shell) that
#: spawns a child after its test ended, between two tests, or after the last one inherits whatever
#: ``$HOME`` is current then. Without this baseline that is the developer's (or CI runner's) real
#: home, which ``_real_home_is_never_written`` caught intermittently on CI.
_SESSION_HOME = tempfile.mkdtemp(prefix="strandly-tests-home-")
_point_home_at(_SESSION_HOME, _set_process_env)
atexit.register(shutil.rmtree, _SESSION_HOME, ignore_errors=True)


def _home_fingerprint() -> dict[str, tuple[int, int] | None]:
    out: dict[str, tuple[int, int] | None] = {}
    for name in _GUARDED_HOME_FILES:
        try:
            st = os.stat(os.path.join(REAL_HOME, name))
        except OSError:
            out[name] = None
        else:
            out[name] = (st.st_mtime_ns, st.st_size)
    return out


@pytest.fixture(autouse=True, scope="session")
def _real_home_is_never_written():
    """Fail the session if any test rewrote the developer's git identity or credential files."""
    before = _home_fingerprint()
    yield
    after = _home_fingerprint()
    changed = sorted(name for name in before if before[name] != after[name])
    assert not changed, f"a test modified {changed} in the real home directory {REAL_HOME}"


@pytest.fixture(autouse=True)
def _hermetic_home(monkeypatch, tmp_path_factory):
    """Point ``$HOME`` and git's global config at a temp dir for every test.

    The sandbox tests run the real git bootstrap (``git config --global user.name …`` and
    ``credential.helper store``) in a host shell; without this they rewrite the developer's real
    ``~/.gitconfig``. Each test gets its own home (see :func:`_point_home_at` for every variable it
    sets), so one test's git identity or credentials never leak into the next; the process-wide
    ``_SESSION_HOME`` covers anything that runs outside a test. It also means no test reads the
    developer's ``~/.aws``.
    """
    _point_home_at(str(tmp_path_factory.mktemp("home")), monkeypatch.setenv)


@pytest.fixture(autouse=True)
def _hermetic_github_context(monkeypatch):
    """Drop any ambient ``GITHUB_CONTEXT`` so the suite is hermetic regardless of the environment.

    Several seams (e.g. ``serving.agentcore._github_context`` and the mention poller) consult
    ``GITHUB_CONTEXT`` as a fallback; an ambient value (set by GitHub Actions) would otherwise leak
    into tests. Removing it for every test keeps results identical with ``GITHUB_CONTEXT`` set or
    unset.
    """
    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)


@pytest.fixture(autouse=True)
def _not_a_deployed_runtime(monkeypatch):
    """Drop the AgentCore-runtime env markers, so the suite is hermetic wherever it runs.

    ``memory/session_store.build_storage`` refuses its local fallback when one is set, and GitHub
    Actions runners with AgentCore observability export ``AGENT_OBSERVABILITY_ENABLED``. Tests that
    want the deployed behaviour set the marker themselves with ``monkeypatch.setenv``.
    """
    from strandly.core.constants import AGENTCORE_RUNTIME_MARKERS

    for name in AGENTCORE_RUNTIME_MARKERS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _local_sandbox_without_a_runtime(monkeypatch):
    """Agents built with no sandbox runtime configured get a host sandbox in tests.

    The product refuses that build (``sandbox/select.build_sandbox``); tests that exercise the real
    selection import it from ``strandly.sandbox.select`` directly.
    """
    from strands.sandbox.not_a_sandbox_local_environment import NotASandboxLocalEnvironment

    import strandly.core.agent as agent_module

    real = agent_module.build_sandbox

    def build_sandbox(config, ctx=None):
        return real(config, ctx) if config.sandbox_runtime_arn else NotASandboxLocalEnvironment()

    monkeypatch.setattr(agent_module, "build_sandbox", build_sandbox)


@pytest.fixture(autouse=True)
def _isolated_session_store(tmp_path_factory, monkeypatch):
    """Point the local session store at a per-test tmp dir.

    Without a bucket the session store is ``LocalFileStorage(LOCAL_SESSION_STORE_DIR)`` — a path
    relative to the cwd, shared by every test in the run. Snapshot restore is an overwrite, so two
    tests that reuse a session id (``"s-1"`` …) would restore each other's conversation. Each test
    gets its own empty store instead (and nothing lands in the repo checkout).
    """
    import strandly.memory.session_store as session_store

    monkeypatch.setattr(
        session_store, "LOCAL_SESSION_STORE_DIR", str(tmp_path_factory.mktemp("session-store"))
    )


@pytest.fixture(autouse=True)
def _hermetic_self_login(monkeypatch):
    """Stub the self-identity resolver's viewer lookup (a live GraphQL call) so the suite stays
    network-free, and clear its per-process cache so a resolved login can't leak between tests.

    ``build_agent`` resolves the agent's own GitHub login off its token for the prompt's identity
    section — a real ``viewer { login }`` API call whenever a token is configured. Tests that
    exercise the lookup itself monkeypatch ``_query_viewer_login`` with their own fake (overriding
    this stub).
    """
    from strandly.tools import github_client

    monkeypatch.setattr(github_client, "_query_viewer_login", lambda token: None)
    monkeypatch.setattr(github_client, "_self_login_cache", {})


def text_response_events(text: str, stop_reason: str = "end_turn") -> list[dict[str, Any]]:
    """Raw model wire-format stream for a plain text response (what the event loop consumes)."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {}}},
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": stop_reason}},
    ]


class FakeModel(Model):
    """Minimal stand-in for a Strands model — replays canned wire-format events, no provider call.

    Subclasses ``strands.models.Model`` (not just duck-typing) because the Strands harness's ``resolve_model``
    dispatches on ``isinstance(model, Model)``: an instance is used verbatim, anything else is
    treated as a ``"provider/name"`` string.
    """

    stateful = False
    context_window_limit = 200_000

    def __init__(self, events: list[dict[str, Any]] | None = None):
        self.events = events or []
        self.config: dict[str, Any] = {}

    def get_config(self) -> dict[str, Any]:
        return self.config

    def update_config(self, **kwargs: Any) -> None:
        self.config.update(kwargs)

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        for ev in self.events:
            yield ev

    async def structured_output(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def count_tokens(self, *args: Any, **kwargs: Any) -> int:
        # Async to match the real ``Model.count_tokens``.
        return 0


@pytest.fixture
def fake_model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def text_model():
    def _make(text: str) -> FakeModel:
        return FakeModel(events=text_response_events(text))

    return _make


@pytest.fixture(autouse=True)
def _hermetic_sandbox_cache_root(monkeypatch, tmp_path):
    """Point the workspace prune's cache root at a temp dir.

    The hermetic fake sandbox runs the real bootstrap scripts in the host shell, and a prune empties
    ``SANDBOX_CACHE_ROOT`` (``/mnt/workspace/.cache`` in production) with ``rm -rf``. A test must
    never do that to a path outside its own temp dir.
    """
    from strandly.sandbox import runtime as sandbox_runtime
    from strandly.sandbox import scripts as sandbox_scripts

    for module in (sandbox_runtime, sandbox_scripts):
        monkeypatch.setattr(module, "SANDBOX_CACHE_ROOT", str(tmp_path / "sandbox-cache"))
