"""Bounded retry decision log shared by the gateway and the direct-mode relay.

Shared verbatim by ClaudApter and CodexApter. It has no Protoverta dependency
so a session can always record why a request was or was not retried.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

LOG_LIMIT_BYTES = 1_000_000


class RetryLog:
    """Append-only decision log, rotated to ``.1`` past ``LOG_LIMIT_BYTES``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def __call__(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n"
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > LOG_LIMIT_BYTES:
                    self.path.replace(self.path.with_name(self.path.name + ".1"))
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
            except OSError:
                pass  # Diagnostics never fail a session.
