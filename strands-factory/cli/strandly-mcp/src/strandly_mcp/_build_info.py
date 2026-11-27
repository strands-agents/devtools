"""Values baked in at release-build time (the release workflow).

The committed defaults are what a source checkout gets. The release workflow overwrites this file
(``scripts/write_build_info.py``) before ``uv build`` so the wheel served from a portal defaults
``login`` to that same portal.
"""

# The Maintainer Portal URL this wheel was published from, or None (``login --portal-url`` needed).
PORTAL_URL = None  # type: str | None
