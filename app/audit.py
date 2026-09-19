"""Append-only JSONL audit log: one line per request, written after the response.

A line is built only from values the service controls or has validated (key label, role, route template,
allow-listed params), and json.dumps escapes newlines, so a request can never forge or split a line. A lock
serialises writes from concurrent requests in one process.
"""
from __future__ import annotations

import json
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_STRING = 200

# outcome vocabulary (brief §7)
OUTCOMES = ("ok", "denied", "not_found", "ambiguous", "error", "unauthenticated")


def outcome_for_status(status_code: int) -> str:
    if status_code < 400:
        return "ok"
    return {401: "unauthenticated", 403: "denied", 404: "not_found"}.get(status_code, "error")


def clip(value: Any) -> Any:
    """Bound what a caller can put into the log: long strings are cut, containers are clipped recursively."""
    if isinstance(value, str):
        return value[:MAX_STRING]
    if isinstance(value, dict):
        return {clip(str(k)): clip(v) for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [clip(v) for v in list(value)[:50]]
    return value


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, entry: dict[str, Any]) -> None:
        line = json.dumps(
            {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **entry},
            ensure_ascii=True,  # escapes \n, \r and U+2028: one request is always exactly one line
            default=str,
        )
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def tail(self, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            if not self.path.exists():
                return []
            with self.path.open(encoding="utf-8") as handle:
                lines = deque(handle, maxlen=limit)
        return [json.loads(line) for line in lines]
