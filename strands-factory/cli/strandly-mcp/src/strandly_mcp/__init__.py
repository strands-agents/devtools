"""strandly-mcp — use a *deployed* Strandly agent from any MCP client, with a browser login.

Kept deliberately thin: stdlib + ``mcp`` (``boto3`` only behind the ``[aws]`` extra). Nothing in
this package may import ``strands``, ``strands_harness`` or ``strandly`` — the release
workflow installs the wheel into a clean venv and fails if any of them shows up.
"""

from __future__ import annotations

__version__ = "0.1.0"
