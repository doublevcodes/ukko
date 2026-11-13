"""The supplier's own record of what each agent looked up and ordered.

This is Valio's side of the audit trail: an append-only JSONL file plus the
latest entries in memory for the /log endpoint.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from gateway import settings

HELSINKI = ZoneInfo("Europe/Helsinki")
_recent: deque[dict[str, Any]] = deque(maxlen=500)
_lock = threading.Lock()


def record(event: str, agent: str | None, summary: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write one plain-English entry. `agent` is who asked, as far as we know."""
    entry = {
        "timestamp": datetime.now(HELSINKI).isoformat(timespec="seconds"),
        "event": event,
        "agent": agent or "unknown agent",
        "summary": summary,
        "details": details or {},
    }
    with _lock:
        _recent.append(entry)
        path = settings.GATEWAY_DATA_DIR / "supplier_log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return entry


def latest(limit: int = 100) -> list[dict[str, Any]]:
    """The most recent entries, newest first."""
    with _lock:
        return list(_recent)[-limit:][::-1]


def reset() -> None:
    with _lock:
        _recent.clear()
