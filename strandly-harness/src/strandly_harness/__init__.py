"""strandly-harness: one opinionated Strands agent, local or AgentCore, served many ways.

Deliberately minimal init: only the import-light ``Config`` is exposed here. Everything else is
reached via its module path (``core.agent.build_agent``, ``serve.turn.run_turn``, …). Keeping this
file free of any ``strands`` import is load-bearing — ``strandly_harness.ops`` imports through it
and must stay stdlib+boto3-only for the trigger-Lambda bundles (see ``ops/__init__.py`` and
``tests/unit/ops/test_import_hygiene.py``).
"""

# Must run before any transitive ``strands``/``opentelemetry`` import below, so a stray
# ``OTEL_PROPAGATORS=xray`` (common in AWS/AgentCore runtimes) can't crash the process on import.
from strandly_harness.otel_guard import sanitize_otel_env

sanitize_otel_env()

from strandly_harness.core.config import Config  # noqa: E402  (must follow sanitize_otel_env)

__all__ = ["Config", "__version__"]

__version__ = "0.1.0"
