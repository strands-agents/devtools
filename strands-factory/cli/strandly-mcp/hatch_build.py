"""Vendor the stdlib session primitives without a runtime or unpublished dependency."""

import shutil
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class SessionPrimitivesHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        for name in ("LICENSE", "NOTICE"):
            canonical = root.parent.parent / name
            if canonical.is_file() and (root / name).read_bytes() != canonical.read_bytes():
                raise ValueError(f"Package {name} must match the canonical root {name} byte-for-byte")
        # Prefer canonical source even if a previous editable build left a generated copy.
        source = root.parent.parent / "harness/src/strandly/core/session_ids.py"
        vendored = root / "src/strandly_mcp/_session_primitives.py"
        if not source.is_file():
            source = vendored  # An extracted sdist is self-contained.
        if not source.is_file():
            raise FileNotFoundError(f"Missing session primitives: {source}")
        if version == "editable" and source != vendored:
            # Editable wheels expose the source directory, not force-included package members.
            shutil.copyfile(source, vendored)
        destination = "strandly_mcp/_session_primitives.py"
        if self.target_name == "sdist":
            destination = f"src/{destination}"
        build_data["force_include"][str(source)] = destination
