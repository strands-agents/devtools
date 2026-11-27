"""Entrypoint for the **sandbox** AgentCore Runtime — a shell and a filesystem, not an agent.

Strandly runs as two AgentCore Runtimes. This is the second one: the box the agent's ``bash``,
``read``, ``write`` and ``edit`` tools actually execute in, reached with
``InvokeAgentRuntimeCommand`` (see ``src/strandly/sandbox/runtime.py``). Its execution role
has a scoped, tested grant set (see ``infra/stacks/sandbox_stack.py``); model-run code can use it.

**There is deliberately no agent here, and no Strandly import.** Two consequences that are the whole
point:

1. *No agent credentials.* No agent model client, config secret, or agent Memory/KB access. The
   only explicit credential delivered by the agent is the sandbox's ``Contents``-scoped git token,
   which arrives in the **wake payload** and is written to a ``0600`` credential store by
   :func:`configure_git_credential` below. It arrives that way rather than inside a shell command
   because AgentCore Runtime writes every command it executes to this runtime's CloudWatch log group
   — a token in a command would be plaintext in the logs, forever, for every session. Ambient AWS
   execution-role credentials are separate; optional provider federation exchanges them through
   ``strandly_sandbox_auth.py`` without importing that helper into this entrypoint.
2. *It starts instantly.* AgentCore fails a runtime whose initialization exceeds 30 seconds;
   importing the harness (and therefore the Strands SDK, boto3, MCP, OpenTelemetry) is exactly the
   kind of import that gets close to that. This module imports one thing.

The HTTP entrypoint below exists only to satisfy the AgentCore Runtime service contract and to give
the harness something to call that *starts the session's instance* — the real work all arrives as
commands on that same session. Answering a ``ping`` with the workspace's tool inventory makes the
image self-describing, which is what you want at 3am when ``git`` is mysteriously missing.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

#: Where the session's persistent EBS workspace volume is mounted. Kept in sync with
#: ``strandly.core.constants.SANDBOX_MOUNT_PATH`` by
#: ``tests/test_infra_constants_sync.py`` — this file cannot import the harness (see the module
#: docstring), so the value is mirrored and the mirror is enforced by a test.
WORKSPACE = os.environ.get("STRANDLY_SANDBOX_WORKSPACE", "/mnt/workspace")

#: Tools the harness (and the agent's skills) assume are present. Reported by ``ping`` so a missing
#: one is visible immediately instead of surfacing as a confusing "command not found" mid-turn.
#: ``aws`` is baked into Dockerfile.sandbox for skills that call AWS from the shell.
EXPECTED_TOOLS = ("bash", "git", "tar", "base64", "rg", "python3", "uv", "node", "aws")

#: Wake-payload key carrying the sandbox's git credential, and the key this module answers with.
#: Mirrored from ``strandly.core.constants`` (this file imports nothing from the harness);
#: ``tests/test_infra_constants_sync.py`` enforces the mirror, because a silent drift here would mean
#: a credential that is sent, ignored, and never written — i.e. pushes failing with nothing to read.
GIT_CREDENTIAL_KEY = "gitCredential"
GIT_CREDENTIAL_STATUS_KEY = "gitCredentialStatus"
GIT_CREDENTIAL_WRITTEN = "written"

#: Where the credential store goes. ``git config credential.helper store`` (set by the harness's
#: bootstrap command) reads exactly this path, expanded against the *shell's* ``$HOME`` — which is the
#: same container and the same user, and is probed by that bootstrap so a disagreement is one named
#: warning rather than an opaque auth failure.
GIT_CREDENTIALS_FILE = ".git-credentials"

app = BedrockAgentCoreApp()


def configure_git_credential(token: str) -> str:
    """Write ``~/.git-credentials`` (mode 0600) so native git can clone/fetch/push. Never raises.

    ``x-access-token`` as the basic-auth username works for classic PATs, fine-grained PATs and GitHub
    App installation tokens alike, so rotating to a different token type changes nothing here.

    The file is written whole on every wake rather than appended to: a wake is also how a rotated
    credential arrives, and appending would leave the stale one to be tried first.

    Returns:
        ``"written"`` on success, or a short ``"failed: <ExceptionType>"`` for the harness to log. The
        token is never returned, echoed or printed — the response goes back to the harness, but this
        container's stdout is its CloudWatch log group.
    """
    path = os.path.join(os.path.expanduser("~"), GIT_CREDENTIALS_FILE)
    previous = os.umask(0o077)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"https://x-access-token:{token}@github.com\n")
        os.chmod(path, 0o600)
    except OSError as e:
        return f"failed: {type(e).__name__}"
    finally:
        os.umask(previous)
    return GIT_CREDENTIAL_WRITTEN


def workspace_status() -> dict[str, Any]:
    """Describe the sandbox: its workspace and which expected tools are actually on ``PATH``."""
    tools = {name: shutil.which(name) for name in EXPECTED_TOOLS}
    status: dict[str, Any] = {
        "workspace": WORKSPACE,
        "workspaceReady": os.path.isdir(WORKSPACE) and os.access(WORKSPACE, os.W_OK),
        "tools": {name: bool(path) for name, path in tools.items()},
        "missingTools": sorted(name for name, path in tools.items() if not path),
    }
    status.update(workspace_capacity(WORKSPACE))
    return status


def workspace_capacity(path: str) -> dict[str, int]:
    """The workspace volume's real size and free space (bytes), from ``statvfs``; ``{}`` if unknown.

    The workspace is the session's own EBS volume, so these are real numbers: the harness words its
    storage advice with the size, and the first wake after a deploy shows the volume is attached
    (about 40 GiB, not the instance's root disk).
    """
    try:
        st = os.statvfs(path)
    except OSError:
        return {}
    return {
        "workspaceCapacityBytes": st.f_blocks * st.f_frsize,
        "workspaceFreeBytes": st.f_bavail * st.f_frsize,
    }


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    """Answer the harness's session-wake call, and write the git credential if one came with it.

    Any payload is accepted: the *call* is what makes AgentCore provision this session's compute,
    which is most of what the caller needs from it. Never raises — a failure here would look to the
    harness like "the sandbox is unavailable" when the sandbox is fine — and never logs or echoes the
    credential, only whether writing it worked.
    """
    payload = payload or {}
    try:
        os.makedirs(WORKSPACE, exist_ok=True)
    except OSError as e:
        return {"status": "degraded", "error": f"{type(e).__name__}: {e}", "workspace": WORKSPACE}
    response: dict[str, Any] = {
        "status": "ready",
        "action": payload.get("action", "ping"),
        **workspace_status(),
    }
    token = payload.get(GIT_CREDENTIAL_KEY)
    if isinstance(token, str) and token:
        response[GIT_CREDENTIAL_STATUS_KEY] = configure_git_credential(token)
    return response


if __name__ == "__main__":
    app.run()
