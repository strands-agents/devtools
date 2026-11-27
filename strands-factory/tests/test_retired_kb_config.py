"""The retired knowledge base and the Code Interpreter role are opt-in.

Nothing reads or writes the knowledge base any more. A new deployment does not create it; a deployment
that still holds it sets ``with_kb=true`` until its data is migrated, and its resources are
``RETAIN`` either way. The harness has no Code Interpreter backend, so its execution role
(``ci_bedrock_role``) is opt-in too.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_the_app_leaves_retired_and_unused_resources_off_by_default():
    source = (_ROOT / "infra" / "app.py").read_text()
    assert 'with_kb=ctx_bool("with_kb", False)' in source
    assert 'ci_bedrock_role=ctx_bool("ci_bedrock_role", False)' in source
