"""Drift guard: infra/stacks/common.py must mirror strandly_harness.core.constants.

The CDK app (``infra/``) is a separate package with its own venv and can't import the harness
without pulling in the Strands SDK, so it hand-copies a handful of fixed provisioning values into
``infra/stacks/common.py``. That hand-mirroring is a latent drift risk — change a value in
``constants.py`` and the deployed resources silently diverge from what the code expects.

This test parses ``common.py`` with ``ast`` (no import — it pulls in ``aws_cdk``, absent from the
harness venv) and asserts every mirrored constant equals its source of truth. If you intentionally
change one, update both files and this test stays green; if you change only one, this fails.
"""

from __future__ import annotations

import ast
from pathlib import Path

from strandly_harness.core import constants

_ROOT = Path(__file__).resolve().parents[1]
_COMMON = _ROOT / "infra" / "stacks" / "common.py"
_HANDLER = _ROOT / "dashboard" / "api" / "handler.py"

# common.py constant name -> source of truth in strandly_harness.core.constants. (common.py uses the
# short name RUN_LEDGER_GSI; constants.py uses RUN_LEDGER_GSI_NAME — they hold the same value.)
_MIRRORED = {
    "MEMORY_EVENT_EXPIRY_DAYS": constants.MEMORY_EVENT_EXPIRY_DAYS,
    "CODE_INTERPRETER_NETWORK_MODE": constants.CODE_INTERPRETER_NETWORK_MODE,
    "KB_EMBEDDING_MODEL": constants.KB_EMBEDDING_MODEL,
    "KB_VECTOR_DIMENSION": constants.KB_VECTOR_DIMENSION,
    "KB_VECTOR_DISTANCE_METRIC": constants.KB_VECTOR_DISTANCE_METRIC,
    "RUN_LEDGER_GSI": constants.RUN_LEDGER_GSI_NAME,
    "MENTION_LOG_GSI": constants.MENTION_LOG_GSI_NAME,
}


def _module_constants(path: Path) -> dict[str, object]:
    """Module-level ``NAME = <literal>`` assignments in a file, via ast (no import).

    Neither common.py (CDK venv) nor handler.py (standalone Lambda bundle) can import the harness,
    so we read their literals statically and compare to the canonical value in constants.py.
    """
    tree = ast.parse(path.read_text())
    found: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    found[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    pass  # non-literal (e.g. a dataclass) — not a mirrored constant
    return found


def test_common_mirrors_constants():
    found = _module_constants(_COMMON)
    for name, expected in _MIRRORED.items():
        assert name in found, f"{name} missing from infra/stacks/common.py"
        assert found[name] == expected, (
            f"{name} drifted: common.py has {found[name]!r}, source of truth is {expected!r}. "
            "Update both infra/stacks/common.py and strandly_harness/constants.py together."
        )


def test_dashboard_handler_gsi_name_matches():
    """The dashboard Lambda's GSI_NAME literal must equal the canonical run-ledger GSI name.

    Closes the third leg of the mirror (common.py / handler.py / constants.py): the Lambda queries
    this index by name, so a drift here silently breaks the dashboard's Runs/Overview tabs.
    """
    found = _module_constants(_HANDLER)
    assert found.get("GSI_NAME") == constants.RUN_LEDGER_GSI_NAME, (
        f"dashboard/api/handler.py GSI_NAME={found.get('GSI_NAME')!r} drifted from "
        f"constants.RUN_LEDGER_GSI_NAME={constants.RUN_LEDGER_GSI_NAME!r}."
    )


def test_dashboard_handler_mention_log_constants_match():
    """The dashboard Lambda queries the mention-log GSI by the same name/partition the poller writes.

    Fourth leg of the mirror for the Mentions tab: ``mention_log.record`` writes ``gsi_pk`` from
    ``constants.MENTION_LOG_GSI_PK_VALUE`` and the Data stack names the index from ``common.py``'s
    ``MENTION_LOG_GSI`` — a drift in the handler's copies silently empties the tab.
    """
    found = _module_constants(_HANDLER)
    assert found.get("MENTION_LOG_GSI_NAME") == constants.MENTION_LOG_GSI_NAME, (
        f"dashboard/api/handler.py MENTION_LOG_GSI_NAME={found.get('MENTION_LOG_GSI_NAME')!r} drifted "
        f"from constants.MENTION_LOG_GSI_NAME={constants.MENTION_LOG_GSI_NAME!r}."
    )
    assert found.get("MENTION_LOG_GSI_PK_VALUE") == constants.MENTION_LOG_GSI_PK_VALUE, (
        f"dashboard/api/handler.py MENTION_LOG_GSI_PK_VALUE={found.get('MENTION_LOG_GSI_PK_VALUE')!r} "
        f"drifted from constants.MENTION_LOG_GSI_PK_VALUE={constants.MENTION_LOG_GSI_PK_VALUE!r}."
    )


def test_dashboard_handler_runtime_session_min_len_matches():
    """The dashboard Lambda pads chat session ids to the same floor the harness uses.

    ``handler._runtime_session_id`` mirrors ``memory.runtime_session_id`` so a chat launched from
    the dashboard lands on the same AgentCore runtime affinity / Memory key the harness would use
    for that session. A drift in the minimum length would desync the two id derivations.
    """
    found = _module_constants(_HANDLER)
    assert found.get("RUNTIME_SESSION_ID_MIN_LEN") == constants.RUNTIME_SESSION_ID_MIN_LEN, (
        f"dashboard/api/handler.py RUNTIME_SESSION_ID_MIN_LEN={found.get('RUNTIME_SESSION_ID_MIN_LEN')!r} "
        f"drifted from constants.RUNTIME_SESSION_ID_MIN_LEN={constants.RUNTIME_SESSION_ID_MIN_LEN!r}."
    )


def test_dashboard_handler_memory_constants_match():
    """The dashboard Lambda mirrors the Memory actor id + page ceiling it reads transcripts with.

    ``handler.MemoryReader`` addresses AgentCore Memory under the same stable actor id the harness
    wrote under (``DEFAULT_ACTOR_ID``) and requests the same high page ceiling (``MEMORY_MAX_EVENTS``)
    so a long run's FINAL assistant message isn't truncated at the 100/page default. A drift in
    either would make the dashboard read the wrong actor or a partial transcript.
    """
    found = _module_constants(_HANDLER)
    assert found.get("DEFAULT_ACTOR_ID") == constants.DEFAULT_ACTOR_ID, (
        f"dashboard/api/handler.py DEFAULT_ACTOR_ID={found.get('DEFAULT_ACTOR_ID')!r} drifted from "
        f"constants.DEFAULT_ACTOR_ID={constants.DEFAULT_ACTOR_ID!r}."
    )
    assert found.get("MEMORY_MAX_EVENTS") == constants.MEMORY_MAX_EVENTS, (
        f"dashboard/api/handler.py MEMORY_MAX_EVENTS={found.get('MEMORY_MAX_EVENTS')!r} drifted from "
        f"constants.MEMORY_MAX_EVENTS={constants.MEMORY_MAX_EVENTS!r}."
    )
