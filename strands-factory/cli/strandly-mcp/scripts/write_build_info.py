#!/usr/bin/env python3
"""Write ``strandly_mcp/_build_info.py`` from the environment (run by the release workflow before build).

    STRANDLY_MCP_PORTAL_URL=https://…cloudfront.net python scripts/write_build_info.py

Unset/empty → PORTAL_URL = None (the committed default), so a plain ``uv build`` is unaffected.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "src" / "strandly_mcp" / "_build_info.py"


def render(portal_url: str | None) -> str:
    value = repr(portal_url.rstrip("/")) if portal_url else "None"
    return (
        '"""Values baked in at release-build time (the release workflow).\n\n'
        "The committed defaults are what a source checkout gets. The release workflow overwrites this file\n"
        "(``scripts/write_build_info.py``) before ``uv build`` so the wheel served from a portal defaults\n"
        "``login`` to that same portal.\n"
        '"""\n\n'
        "# The Maintainer Portal URL this wheel was published from, or None (``login --portal-url`` needed).\n"
        f"PORTAL_URL = {value}  # type: str | None\n"
    )


def main() -> int:
    portal = os.environ.get("STRANDLY_MCP_PORTAL_URL", "").strip() or None
    if portal and not portal.startswith("https://"):
        print(f"STRANDLY_MCP_PORTAL_URL must be an https URL, got {portal!r}", file=sys.stderr)
        return 1
    TARGET.write_text(render(portal), encoding="utf-8")
    print(f"wrote {TARGET} (PORTAL_URL={portal!r})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
