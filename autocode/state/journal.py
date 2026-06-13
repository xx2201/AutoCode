"""Minimal audit log for session execution."""

from __future__ import annotations

import json
import threading
import time

from .checkpoint import session_dir


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class AuditLogger:
    """Append structured session events to a JSONL journal."""

    def __init__(self):
        self._lock = threading.Lock()

    def append_event(self, session_id: str, event_type: str, payload: dict):
        directory = session_dir(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": _now(),
            "event": event_type,
            "payload": payload,
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with (directory / "audit.jsonl").open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def handle(self, event: str, payload: dict):
        session_id = payload.get("session_id")
        if not session_id:
            return
        self.append_event(session_id, event, payload)


def load_events(session_id: str) -> list[dict]:
    path = session_dir(session_id) / "audit.jsonl"
    if not path.exists():
        return []

    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events
