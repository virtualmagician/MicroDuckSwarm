"""Append-only JSONL log of every message the mock duck receives.

This is the whole point of mock_duck: end-to-end tests tail this file to
assert on what was sent and when, in receipt order, with a monotonic
timestamp that is comparable across the whole test run.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class IntentLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Line-buffered text mode; we also explicitly flush() after every
        # write so a tailing test never has to wait for a buffer to fill.
        self._fh = open(self.path, "a", buffering=1, encoding="utf-8")

    def write_message(self, msg: Any) -> None:
        record = {
            "rx_ns": time.monotonic_ns(),
            "rx_wall": time.time(),
            "msg": msg,
        }
        line = json.dumps(record)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._fh.close()
