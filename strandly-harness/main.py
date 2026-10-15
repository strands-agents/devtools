"""AgentCore Runtime entrypoint for a deployed Strandly.

The bedrock-agentcore starter toolkit builds a container whose ``CMD`` is ``python -m main`` (or
``opentelemetry-instrument python -m main``). It expects a module-level ``BedrockAgentCoreApp``
named ``app`` and runs it — ``app.run()`` starts the HTTP server AgentCore Runtime invokes.

Config comes from the environment the way every other surface does: the runtime is deployed with
``STRANDLY_SECRETS_ARN`` set, so ``Config.load()`` fetches the Memory / Code Interpreter / KB ids
(and any tokens) from Secrets Manager. The package sanitizes ``OTEL_PROPAGATORS`` on import.
"""

from __future__ import annotations

import os
import sys

# Direct-code-deploy ships the source tree as-is to /var/task and runs `python -m main`, but our
# package lives under src/ (src layout) and isn't pip-installed into the runtime's site-packages.
# Put src/ on the path so `import strandly_harness` resolves in the deployed runtime (no-op locally
# where the package is already importable via the editable install).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import strandly_harness  # noqa: E402,F401 — runs sanitize_otel_env() before any strands import
from strandly_harness.core.config import Config  # noqa: E402
from strandly_harness.serve.agentcore_app import build_app  # noqa: E402

# Module-level app the toolkit's `python -m main` container CMD imports and runs.
app = build_app(Config.load())


if __name__ == "__main__":
    app.run()
