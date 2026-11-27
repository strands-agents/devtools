"""AgentCore Runtime entrypoint (``opentelemetry-instrument main.py``) for a deployed Strandly.

Defines the module-level ``BedrockAgentCoreApp`` named ``app`` and runs it. The import is
snapshot-safe: it passes the loader ``Config.load`` rather than a loaded config, so no secret,
AWS client, credential or random seed is captured before the Runtime V2 snapshot
(``strandly.serve.instance_init``). Heavy imports stay here, where the snapshot captures them.
"""

from __future__ import annotations

import os
import sys

# Direct-code-deploy ships the source tree as-is to /var/task and runs `python -m main`, but our
# package lives under src/ (src layout) and isn't pip-installed into the runtime's site-packages.
# Put src/ on the path so `import strandly` resolves in the deployed runtime (no-op locally
# where the package is already importable via the editable install).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

# Same layout means the deps' executables land in /var/task/bin, which the runtime's Python can't
# see (not its scripts dir, not on PATH) — pydantic-monty's lookup then crashes with IndexError and
# every programmatic_tool_caller call fails. Point it at the bundled binary.
_monty = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "monty")
if os.path.isfile(_monty):
    os.environ.setdefault("MONTY_BIN", _monty)

import strandly  # noqa: E402,F401 — runs sanitize_otel_env() before any strands import
from strandly.core.config import Config  # noqa: E402
from strandly.serve.agentcore_app import build_app  # noqa: E402

# Module-level app the runtime's `main.py` entrypoint imports and runs. The loader (not a loaded
# Config) keeps import free of secrets/credentials — see the module docstring.
app = build_app(Config.load)


if __name__ == "__main__":
    app.run()
