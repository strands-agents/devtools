"""The ops/ strands-free contract, machine-enforced.

``strandly_harness.ops`` (plus the import-light ``core.config``/``core.constants`` and the
top-level package init it pulls in) must import with **stdlib + boto3 only** — these modules are
bundled into trigger Lambdas by ``infra/scripts/build-poller-package.sh`` (``pip install .
--no-deps``) where the Strands SDK, requests, MCP, and opentelemetry simply do not exist.

The test poisons ``sys.modules`` for every forbidden distribution (``None`` makes any ``import``
of it raise ``ImportError``), purges cached ``strandly_harness`` modules so imports re-execute,
then imports every module under ``ops/``. If someone adds a top-level ``import strands`` (or
``requests``, …) anywhere in the subtree — or makes an ``__init__`` eagerly pull one in — this
fails loudly in CI instead of at Lambda cold start.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys

# Anything the Lambda bundle does not carry. boto3 is deliberately absent (provided by the Lambda
# runtime and allowed); everything agent-side is forbidden.
FORBIDDEN = [
    "strands",
    "strands_tools",
    "bedrock_agentcore",
    "bedrock_agentcore_starter_toolkit",
    "opentelemetry",
    "mcp",
    "requests",
    "httpx",
]


# Modules OUTSIDE ``ops.*`` that the Lambda bundle nonetheless imports at runtime — ``ops`` reaches
# these across the package, so they must be strands-free too, but ``walk_packages(ops)`` wouldn't
# cover them. Enumerating them here closes the gap: a hoisted ``import strands`` in any of them fails
# this test instead of the deployed poller's first dispatch. (Verified strands-free today.)
_CROSS_BOUNDARY = [
    "strandly_harness.core.config",
    "strandly_harness.core.constants",
    "strandly_harness.core.context",
    "strandly_harness.core.session_ids",
]


def _ops_modules() -> list[str]:
    import strandly_harness.ops as ops

    names = ["strandly_harness.ops", *_CROSS_BOUNDARY]
    for info in pkgutil.walk_packages(ops.__path__, prefix="strandly_harness.ops."):
        names.append(info.name)
    return names


def test_ops_subtree_imports_without_agent_dependencies(monkeypatch):
    module_names = _ops_modules()  # enumerate before purging the cache

    # Re-execute all package imports under poisoned modules (monkeypatch restores everything).
    for name in list(sys.modules):
        if name == "strandly_harness" or name.startswith("strandly_harness."):
            monkeypatch.delitem(sys.modules, name)
    for dist in FORBIDDEN:
        monkeypatch.setitem(sys.modules, dist, None)
        # also purge any already-imported submodules so the poison pin is what resolves
        for name in list(sys.modules):
            if name.startswith(dist + "."):
                monkeypatch.delitem(sys.modules, name)

    for name in module_names:
        importlib.import_module(name)  # raises ImportError if anything touches a forbidden dep
