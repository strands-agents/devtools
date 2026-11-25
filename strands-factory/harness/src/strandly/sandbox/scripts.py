"""Shell programs the adapter runs inside the sandbox, and the parser for their output.

The git bootstrap, the stderr heartbeat wrapper, the cache environment, and the workspace health
probe with its prune of regenerable directories. Everything here returns text; nothing calls AWS.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field

from strandly.core.constants import (
    SANDBOX_CACHE_ENV,
    SANDBOX_CACHE_ROOT,
    SANDBOX_GIT_COMMIT_EMAIL,
    SANDBOX_GIT_COMMIT_NAME,
    SANDBOX_GIT_CREDENTIAL_MISSING_MARKER,
    SANDBOX_HEARTBEAT_INTERVAL_SECONDS,
    SANDBOX_HEARTBEAT_MARKER,
    SANDBOX_WORKSPACE_PRUNE_FRACTION,
    SANDBOX_WORKSPACE_REGENERABLE_DIRS,
)


def _is_full(detail: str) -> bool:
    """True when a storage failure means "out of space" (as opposed to an I/O fault)."""
    lowered = detail.lower()
    return any(m in lowered for m in ("no space", "quota"))


def git_bootstrap_script(*, workdir: str, expect_credential: bool, author_name: str = SANDBOX_GIT_COMMIT_NAME, author_email: str = SANDBOX_GIT_COMMIT_EMAIL) -> str:
    """An idempotent shell script configuring the sandbox's git client. **Takes no secret.**

    The signature is the security property: this string becomes the body of an
    ``InvokeAgentRuntimeCommand``, and the service *writes every command it runs to the sandbox's
    CloudWatch log group* ("AgentCore Runtime sends the request ID and the input command to your
    agent's CloudWatch Logs log group"). A token embedded here would therefore be logged in plaintext,
    permanently, for every session. So the credential file is written by the sandbox entrypoint from
    the wake payload instead (:meth:`RuntimeSandbox._wake`, ``sandbox_main.py``), and this function
    cannot leak a token because it is never given one.

    What it still does, all of it safe to log:

    - **A commit identity**, always — without one ``git commit`` fails ("Please tell me who you are")
      on a bare image.
    - **``credential.helper store``** when a credential is expected, so every native
      ``clone``/``fetch``/``push`` authenticates from ``~/.git-credentials`` with no per-command
      plumbing. Still the credential *store* and not ``url.insteadOf``: a token-bearing URL rewrite
      would leak into every ``git config -l`` and every error message, whereas the store is one
      ``0600`` file a rotation overwrites.
    - **Asserts the workspace is writable**, which the Dockerfile cannot guarantee for a
      service-mounted volume.
    - **Probes for the credential file** when one is expected, printing
      :data:`~strandly.core.constants.SANDBOX_GIT_CREDENTIAL_MISSING_MARKER` if it is absent.
      This catches the one thing the wake response cannot see: a command shell whose ``$HOME`` differs
      from the entrypoint's, which would leave a perfectly-written credential in the wrong place. It
      prints a path and a username, never a secret.

    Args:
        workdir: The workspace to create and check — the caller's own, not the module default, so the
            script tests the directory commands will actually run in.
        expect_credential: Whether the wake payload carried a git credential for this session.

    Returns:
        A shell script safe to run repeatedly, and safe to log.
    """
    lines = [
        "set -e",
        "umask 077",
        f"git config --global user.name {shlex.quote(author_name)}",
        f"git config --global user.email {shlex.quote(author_email)}",
        # The workspace volume is created and mounted by the service, so whether it ends up
        # writable by this image's non-root user is not something the Dockerfile can guarantee.
        # Assert it here so a permissions problem announces itself in one clear line instead of
        # surfacing later as a bare "cd: permission denied" on the agent's first real command.
        f"mkdir -p {shlex.quote(workdir)}",
        f'test -w {shlex.quote(workdir)} || {{ echo "strandly-sandbox: workspace "'
        f'{shlex.quote(workdir)}" is not writable by $(id -un)" >&2; exit 1; }}',
    ]
    if expect_credential:
        lines += [
            "git config --global credential.helper store",
            # The credential was written by the entrypoint process. If it is not visible here, the
            # two sides disagree about $HOME — say so once, by name, instead of leaving every push to
            # fail with an opaque auth error.
            f'test -s "$HOME/.git-credentials" || echo '
            f'"{SANDBOX_GIT_CREDENTIAL_MISSING_MARKER} from $HOME (user $(id -un))" >&2',
        ]
    lines.append('echo "strandly-sandbox: git configured"')
    return "\n".join(lines)


def with_heartbeat(body: str, interval: int = SANDBOX_HEARTBEAT_INTERVAL_SECONDS) -> str:
    """``body`` run under a stderr heartbeat that ends, and is reaped, with it.

    A command that prints nothing for minutes (``sleep 500``, a quiet test suite) leaves the
    HTTP/2 output stream idle, and production lost such streams with no completion event (they then
    surfaced as transport timeouts, 16 minutes later). The heartbeat writes one
    :data:`SANDBOX_HEARTBEAT_MARKER` byte to stderr every ``interval`` seconds; the adapter strips it.

    Shape, and why each piece is there:

    * The heartbeat is a **child of the wrapper that the wrapper kills and waits for**, and it
      kills and waits for its own ``sleep``. Nothing the wrapper started is alive or unreaped once
      it returns: the service keeps a command's slot (197 per session container) until every
      process it started is gone, zombies included, and an orphan is only reaped by PID 1.
    * ``body`` runs in a **subshell**, where the heartbeat is not a child: a model-written bare
      ``wait`` does not wait on it. The heartbeat is stopped explicitly when the subshell returns,
      with the body's exit status passed through. Nothing ``body`` does to traps, ``exec`` or
      ``exit`` can skip that. An EXIT trap is kept as a backstop for the wrapper being signalled.
    * The loop's ``sleep`` has its own output on ``/dev/null``, so it never holds the stream open;
      and the loop also exits once its shell is gone (a killed wrapper cannot reap it: that is the
      init's job, see ``Dockerfile.sandbox``).
    * The loop also ends the **whole command when the service abandons it**. On a timeout the
      service kills only the ``/bin/sh -c`` it started (the leader of the command's process group,
      and the wrapper's parent); the wrapper, the body and everything the body started were
      reparented to PID 1 and kept running, each such command holding its slot until they ended
      (197 timed-out commands made every later command exit -1). So once a second the heartbeat
      reads the wrapper's parent and group from ``/proc``: if the parent that led the group is
      gone, it sends TERM to the group, waits 2 seconds and sends KILL. It does nothing when the
      wrapper leads its own group (the parent never led it) or where ``/proc`` is unreadable.
    * ``body`` is closed by a newline before ``)``, so a trailing heredoc or comment stays valid.
      Its ``$!`` starts as an already-reaped no-op job, not the heartbeat.
    * A shell started with TERM ignored cannot trap it, so it gets no heartbeat (and cannot hang
      waiting for one).
    """
    marker = f"\\{ord(SANDBOX_HEARTBEAT_MARKER):03o}"
    # Kill AND wait: an unreaped heartbeat (or its sleep) keeps this command's slot forever.
    stop = "kill $__strandly_hb 2>/dev/null; wait $__strandly_hb 2>/dev/null"
    return (
        # Only TERM stops the heartbeat: a shell that inherited TERM ignored (it cannot trap it)
        # runs the body without one rather than waiting on it forever.
        "__strandly_t=; __strandly_hb=; __strandly_pp=$PPID; "
        "trap '__strandly_t=1' TERM; kill -TERM $$; trap - TERM; "
        'if [ -n "$__strandly_t" ]; then '
        # SIGKILL: a TERM can land in the forked `sleep` before it execs and be swallowed there.
        "{ trap 'kill -9 $! 2>/dev/null; wait $! 2>/dev/null; exit 0' TERM; __strandly_i=0; "
        "while sleep 1 >/dev/null 2>&1 & wait $!; do "
        "kill -0 $$ 2>/dev/null || exit 0; "
        # Abandoned by the service (its `/bin/sh`, our parent and group leader, is gone): end the
        # command's whole process group, TERM first, so nothing it started keeps holding its slot.
        "__strandly_p=; __strandly_g=; "
        "read -r _ _ _ __strandly_p __strandly_g _ 2>/dev/null </proc/$$/stat; "
        'if [ -n "$__strandly_p" ] && [ "$__strandly_p" != "$__strandly_pp" ] '
        '&& [ "$__strandly_g" = "$__strandly_pp" ] && [ "$__strandly_g" -gt 1 ]; then '
        "trap '' TERM; kill -TERM -$__strandly_g 2>/dev/null; sleep 2 >/dev/null 2>&1; "
        "kill -KILL -$__strandly_g 2>/dev/null; exit 0; fi; "
        "__strandly_i=$((__strandly_i+1)); "
        f'if [ "$__strandly_i" -ge {int(interval)} ]; then printf "{marker}" >&2; '
        "__strandly_i=0; fi; done; } </dev/null >/dev/null & __strandly_hb=$!; fi; "
        f"trap '{stop}' EXIT; (\n"
        # The body's `$!` must not be the heartbeat.
        ": & wait $!\n"
        f"{body}\n"
        f"); __strandly_rc=$?; {stop}; trap - EXIT; exit $__strandly_rc"
    )


def strip_heartbeat(text: str) -> str:
    """``text`` without heartbeat markers (see :func:`with_heartbeat`)."""
    return text.replace(SANDBOX_HEARTBEAT_MARKER, "")


def cache_env_prefix() -> str:
    """``export`` of :data:`SANDBOX_CACHE_ENV`: package caches and build output under
    :data:`SANDBOX_CACHE_ROOT`, on the workspace volume (warm across invocations, pruned whole)."""
    assignments = " ".join(f"{k}={shlex.quote(v)}" for k, v in SANDBOX_CACHE_ENV.items())
    return f"export {assignments} && "


#: Markers the workspace health script prints (parsed by :func:`parse_workspace_health`).
HEALTH_USED_MARKER = "strandly-sandbox: workspace-kb="
_HEALTH_CAPACITY = "strandly-sandbox: workspace-capacity-kb="
_HEALTH_PRUNED = "strandly-sandbox: pruned-kb="
_HEALTH_WRITE = "strandly-sandbox: workspace-write="


#: The prune, as a Python program (``python3`` is in the image; the health probe already uses it).
#: Written in Python rather than ``find | while read`` so that no file name — a pull request controls
#: every name in a checkout, including ones with newlines — is ever split, re-parsed or matched by
#: anything but exact string equality. argv: workspace, JSON list of directory names, max age (s),
#: and optionally a directory the walk must skip (the cache root, which the script deletes whole
#: before the walk — walking its huge cargo/Go/pnpm trees would only burn the walk's time limit).
_PRUNE_PROGRAM = r"""
import json, os, re, shutil, subprocess, sys, time

root = os.path.realpath(sys.argv[1])
names = frozenset(json.loads(sys.argv[2]))
cutoff = time.time() - int(sys.argv[3])
skip = None
if len(sys.argv) > 4 and sys.argv[4]:
    # Resolve the parent only: the skipped path itself may be a symlink, never followed anyway.
    skip = os.path.join(
        os.path.realpath(os.path.dirname(os.path.abspath(sys.argv[4]))),
        os.path.basename(os.path.abspath(sys.argv[4])),
    )
# Exactly the scratch names RuntimeSandbox.write_file and the skills push create.
scratch = re.compile(r"\A[^/]*\.strandly-[0-9a-f]{12}\.(?:b64|part)\Z|\Askills-[0-9a-f]+\.tgz\Z")
root_dev = os.lstat(root).st_dev


def strictly_inside(path):
    # No symlink anywhere on the way down (realpath == the path we walked), same filesystem,
    # and never the workspace itself.
    real = os.path.realpath(path)
    return (
        real == os.path.abspath(path)
        and real.startswith(root + os.sep)
        and os.lstat(path).st_dev == root_dev
    )


def holds_git(path):
    for _dir, dirs, files in os.walk(path):
        if ".git" in dirs or ".git" in files:
            return True
    return False


def tracked(path):
    # Inside a git checkout, refuse anything git tracks (a vendored node_modules is source).
    top = os.path.dirname(path)
    while not os.path.lexists(os.path.join(top, ".git")):
        if top == root:
            return False
        top = os.path.dirname(top)
    try:
        done = subprocess.run(
            ["git", "--literal-pathspecs", "-C", top, "-c", "core.fsmonitor=false",
             "ls-files", "-z", "--", os.path.relpath(path, top)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
        )
    except Exception:
        return True  # cannot tell: keep it
    return done.returncode != 0 or bool(done.stdout)


removed = []
for here, dirs, files in os.walk(root):
    keep = []
    for name in dirs:
        path = os.path.join(here, name)
        if name == ".git" or os.path.islink(path) or path == skip:
            continue  # never descend into .git, through a symlink, or into the cache root
        if name not in names:
            keep.append(name)
            continue
        # A candidate is never descended into, whether or not it is deleted.
        if strictly_inside(path) and not holds_git(path) and not tracked(path):
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.lexists(path):
                removed.append(path)
    dirs[:] = keep
    for name in files:
        path = os.path.join(here, name)
        if not scratch.match(name) or os.path.islink(path):
            continue
        try:
            old = os.lstat(path).st_mtime < cutoff
        except OSError:
            continue
        if old and strictly_inside(path) and not tracked(path):
            try:
                os.unlink(path)
            except OSError:
                pass
            else:
                removed.append(path)
for path in removed[:PRUNE_REPORT_LIMIT]:
    print("PRUNED_MARKER" + json.dumps(os.path.relpath(path, root)))
if len(removed) > PRUNE_REPORT_LIMIT:
    print("PRUNED_MARKER" + json.dumps("... and %d more" % (len(removed) - PRUNE_REPORT_LIMIT)))
"""
#: How many deleted paths the prune reports by name (the rest are counted).
_PRUNE_REPORT_LIMIT = 20
_HEALTH_PRUNED_PATH = "strandly-sandbox: pruned-path="
#: Scratch files younger than this are left alone: a write may still be in flight.
_SCRATCH_MAX_AGE_SECONDS = 3600
#: Time limits on the prune's steps, all inside the bootstrap's 180 s command budget (which also
#: covers the git setup): making Go's read-only module cache deletable, deleting the cache root, and
#: walking the workspace for regenerable directories. The cache goes first — it is the cheapest
#: space to reclaim and the one thing always safe to delete.
_PRUNE_CHMOD_SECONDS = 15
_PRUNE_CACHE_RM_SECONDS = 45
_PRUNE_WALK_SECONDS = 60


def workspace_prune_script(workdir: str, *, skip: str | None = None) -> str:
    """Delete regenerable directories under ``workdir`` (and only there). Safe to log.

    Deletes only directories whose name is *exactly* one of
    :data:`SANDBOX_WORKSPACE_REGENERABLE_DIRS`, plus the scratch files an interrupted
    :meth:`RuntimeSandbox.write_file` or skills push left more than an hour ago. A candidate is kept
    when it is a symlink, sits behind one, is on another filesystem, contains a ``.git`` (a clone,
    or a repository root), or is tracked by the git checkout it lives in. Never descends into
    ``.git``; never touches ``workdir`` itself. Prints one ``pruned-path=`` marker (a JSON string,
    so a name with a newline cannot forge a marker) per deleted path. ``skip`` (the cache root, which
    the health script deletes separately) is never walked.
    """
    program = _PRUNE_PROGRAM.replace("PRUNE_REPORT_LIMIT", str(_PRUNE_REPORT_LIMIT)).replace(
        "PRUNED_MARKER", _HEALTH_PRUNED_PATH
    )
    args = " ".join(
        shlex.quote(a)
        for a in (
            workdir,
            json.dumps(list(SANDBOX_WORKSPACE_REGENERABLE_DIRS)),
            str(_SCRATCH_MAX_AGE_SECONDS),
            skip or "",
        )
    )
    return f"__strandly_t {_PRUNE_WALK_SECONDS} python3 - {args} <<'STRANDLY_PRUNE'\n{program.strip()}\nSTRANDLY_PRUNE"


#: ``timeout`` when the image has it (it does: coreutils), else run unbounded — keeps the scripts
#: runnable on a developer's macOS shell, which is where the hermetic tests execute them.
_TIMEBOX_FUNCTION = (
    '__strandly_t() { if command -v timeout >/dev/null 2>&1; then timeout "$@"; '
    'else shift; "$@"; fi; }'
)


def workspace_health_script(
    workdir: str, *, force_prune: bool = False, cache_root: str | None = None
) -> str:
    """Measure the workspace, prune it when nearly full, and probe that it accepts a write.

    The workspace is the session's EBS volume, so ``df`` is real: usage and capacity both come from
    it (no slow ``du`` over a 40 GiB tree). Above :data:`SANDBOX_WORKSPACE_PRUNE_FRACTION` of the
    volume's real size — or always, with ``force_prune`` — :data:`SANDBOX_CACHE_ROOT` is deleted
    first (``rm -rf`` on the path itself, so a symlink planted there is removed, not followed; any
    directory Go left read-only is made writable first, without following symlinks, or ``rm`` could
    not empty it), then regenerable directories are pruned (:func:`workspace_prune_script`, which
    skips the cache root). Every step is time-limited. With no ``df`` reading nothing is pruned on
    a guess. ``cache_root`` defaults to :data:`SANDBOX_CACHE_ROOT` (where
    :data:`SANDBOX_CACHE_ENV` points). The probe writes and **fsyncs** 4 KiB, so a deferred write error
    (ENOSPC, EIO) surfaces here rather than in the model's next command. Never exits non-zero;
    prints only the ``strandly-sandbox:`` markers. Safe to log.
    """
    ws = shlex.quote(workdir)
    cache = shlex.quote(cache_root or SANDBOX_CACHE_ROOT)
    percent = int(round(SANDBOX_WORKSPACE_PRUNE_FRACTION * 100))
    prune_test = "true" if force_prune else '[ -n "$__used" ] && [ "$__used" -ge "$__threshold_kb" ]'
    df_line = f"__strandly_t 20 df -Pk {ws} 2>/dev/null | awk 'NR==2 {{print $2\" \"$3}}'"
    return "\n".join(
        [
            "set +e",
            _TIMEBOX_FUNCTION,
            f"__df=$({df_line})",
            '__cap_kb="${__df%% *}"; __used="${__df##* }"',
            'case "$__cap_kb" in ""|*[!0-9]*) __cap_kb=0 ;; esac',
            'case "$__used" in ""|*[!0-9]*) __used="" ;; esac',
            f"__threshold_kb=$(( __cap_kb * {percent} / 100 ))",
            # No df reading: never prune on a guess (a 0 threshold would prune on every wake).
            '[ "$__cap_kb" -gt 0 ] || __threshold_kb=999999999999',
            f'echo "{_HEALTH_CAPACITY}$__cap_kb"',
            f'echo "{HEALTH_USED_MARKER}${{__used:-unknown}}"',
            f"if {prune_test}; then",
            # Go writes its module cache read-only (0444 files in 0555 dirs): without u+w on the
            # dirs, `rm` (uid 1000, no CAP_DAC_OVERRIDE) cannot unlink anything in them. `find`
            # does not follow symlinks (-P), and a symlinked cache root is not entered at all.
            f"  [ -L {cache} ] || __strandly_t {_PRUNE_CHMOD_SECONDS} find {cache} -type d "
            "! -perm -u+w -exec chmod u+w {} + 2>/dev/null",
            f"  __strandly_t {_PRUNE_CACHE_RM_SECONDS} rm -rf -- {cache} 2>/dev/null",
            workspace_prune_script(workdir, skip=cache_root or SANDBOX_CACHE_ROOT),
            f"  __after=$(__strandly_t 20 df -Pk {ws} 2>/dev/null | awk 'NR==2 {{print $3}}')",
            f'  echo "{_HEALTH_PRUNED}$(( ${{__used:-0}} - ${{__after:-${{__used:-0}}}} ))"',
            "fi",
            f"python3 - {ws} <<'STRANDLY_PROBE'",
            "import os, sys",
            "path = os.path.join(sys.argv[1], '.strandly-probe-%d' % os.getpid())",
            "try:",
            "    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)",
            "    try:",
            "        os.write(fd, b'x' * 4096)",
            "        os.fsync(fd)",
            "    finally:",
            "        os.close(fd)",
            "    os.unlink(path)",
            f"    print('{_HEALTH_WRITE}ok')",
            "except OSError as e:",
            f"    print('{_HEALTH_WRITE}failed: %s' % (e.strerror or e))",
            "STRANDLY_PROBE",
            "true",
        ]
    )


@dataclass
class WorkspaceHealth:
    """What :func:`workspace_health_script` reported. ``None`` fields mean "not reported"."""

    used_kb: int | None = None
    #: The workspace volume's real size from ``df`` (``None`` when ``df`` gave no reading).
    capacity_kb: int | None = None
    pruned_kb: int | None = None
    writable: bool | None = None
    detail: str = ""
    #: Workspace-relative paths the prune deleted (capped; the last entry may be "... and N more").
    pruned_paths: list[str] = field(default_factory=list)

    @property
    def full(self) -> bool:
        """True when the failed probe means "out of space" rather than an I/O fault."""
        return self.writable is False and _is_full(self.detail)


def parse_workspace_health(text: str) -> WorkspaceHealth:
    """Parse the markers :func:`workspace_health_script` prints."""
    health = WorkspaceHealth()
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(HEALTH_USED_MARKER):
            value = line[len(HEALTH_USED_MARKER) :]
            health.used_kb = int(value) if value.isdigit() else None
        elif line.startswith(_HEALTH_PRUNED):
            value = line[len(_HEALTH_PRUNED) :]
            health.pruned_kb = int(value) if value.lstrip("-").isdigit() else None
        elif line.startswith(_HEALTH_PRUNED_PATH):
            try:
                path = json.loads(line[len(_HEALTH_PRUNED_PATH) :])
            except ValueError:
                continue
            if isinstance(path, str):
                health.pruned_paths.append(path)
        elif line.startswith(_HEALTH_CAPACITY):
            value = line[len(_HEALTH_CAPACITY) :]
            health.capacity_kb = int(value) if value.isdigit() and int(value) > 0 else None
        elif line.startswith(_HEALTH_WRITE):
            value = line[len(_HEALTH_WRITE) :]
            health.writable = value == "ok"
            health.detail = "" if health.writable else value.removeprefix("failed:").strip()
    return health
