"""Context management: offload oversized tool results to the sandbox filesystem.

The agent runs with ``context_manager="agentic"`` — the model manages its own history via
injected tools with a SummarizingConversationManager overflow safety net. We supply our own
sandbox-routed offloader so oversized tool results land in the sandbox FS (readable back with
``bash``/``file_editor``) rather than the agentic path's in-memory store.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from strandly_harness.core.constants import (
    OFFLOAD_DIR,
    OFFLOAD_MAX_RESULT_TOKENS,
    OFFLOAD_PREVIEW_TOKENS,
)

if TYPE_CHECKING:
    from strands.sandbox.base import Sandbox

def build_offloader(sandbox: Sandbox) -> Any:
    """Offload oversized tool results to the sandbox filesystem (preview + reference stay).

    ``include_retrieval_tool=False`` on purpose: the offloader writes each block to a real file in
    the agent's OWN sandbox (``OFFLOAD_DIR``), and the in-context preview keeps that path — so the
    agent reads offloaded content back with its existing ``bash``/``file_editor`` (``cat`` the
    artifact), which returns it as text. The bundled ``retrieve_offloaded_content`` tool is therefore
    redundant, and its ``application/*`` branch emits a Bedrock ``document`` block whose ``format``
    (e.g. ``octet-stream`` for unknown bytes) is outside Converse's allowed enum — failing the turn
    with a ValidationException (strands-agents/harness-sdk#3019). Dropping the tool removes both the
    redundancy and that crash; reading via the sandbox FS is strictly more robust for our agent.
    """
    from strands.vended_plugins.context_offloader import ContextOffloader, FileStorage

    return ContextOffloader(
        storage=FileStorage(OFFLOAD_DIR, sandbox=sandbox),
        max_result_tokens=OFFLOAD_MAX_RESULT_TOKENS,
        preview_tokens=OFFLOAD_PREVIEW_TOKENS,
        include_retrieval_tool=False,
    )

