"""Append-only raw transcript persistence for task messages."""

from __future__ import annotations

import json
import threading
import time

from .checkpoint import task_dir


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class TranscriptLogger:
    """Persist the raw task transcript without mutating historical entries."""

    def __init__(self):
        self._lock = threading.Lock()

    def append_message(self, task_id: str, message: dict):
        self._append_entry(task_id, {"timestamp": _now(), "kind": "message", "message": message})

    def append_compaction(self, task_id: str, payload: dict):
        self._append_entry(task_id, {"timestamp": _now(), "kind": "compact", "payload": payload})

    def _append_entry(self, task_id: str, entry: dict):
        directory = task_dir(task_id)
        directory.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with (directory / "transcript.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_transcript_entries(task_id: str) -> list[dict]:
    path = task_dir(task_id) / "transcript.jsonl"
    if not path.exists():
        return []

    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def load_transcript_messages(task_id: str) -> list[dict]:
    return [entry["message"] for entry in load_transcript_entries(task_id) if entry.get("kind") == "message" and "message" in entry]
