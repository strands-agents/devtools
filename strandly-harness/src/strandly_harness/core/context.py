"""Per-invocation runtime context shared across the harness."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class RuntimeContext:
    """Everything an invocation needs that is not in the static config.

    Built fresh per request/turn by a serving adapter and threaded into ``build_agent``.
    """

    cwd: str = field(default_factory=os.getcwd)
    session_id: str | None = None
    session_key: str | None = None  # derive a deterministic id when session_id is unset
    event: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    now: datetime | None = None  # clock override for deterministic tests

    def timestamp(self) -> datetime:
        return self.now or datetime.now(timezone.utc)

    def environment_block(self) -> str:
        """A '# Environment' block that makes prompt promises real (cwd/platform/date)."""
        ts = self.timestamp().strftime("%Y-%m-%d")
        lines = [
            "# Environment",
            f"- Working directory: {self.cwd}",
            f"- Platform: {platform.system().lower()}",
            f"- OS: {platform.platform()}",
            f"- Date: {ts}",
        ]
        if self.session_id:
            lines.append(f"- Session: {self.session_id}")
        return "\n".join(lines)
