"""Track file versions observed by read tools before edits."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path


class FileReadTracker:
    def __init__(self):
        self._versions: dict[str, str] = {}
        self._lock = threading.Lock()

    def record(self, path: Path, content: str | bytes) -> None:
        key = str(path.resolve())
        digest = self._digest(content)
        with self._lock:
            self._versions[key] = digest

    def status(self, path: Path, content: str | bytes) -> str:
        key = str(path.resolve())
        digest = self._digest(content)
        with self._lock:
            expected = self._versions.get(key)
        if expected is None:
            return "unread"
        return "current" if expected == digest else "changed"

    def forget(self, path: Path) -> None:
        with self._lock:
            self._versions.pop(str(path.resolve()), None)

    @staticmethod
    def _digest(content: str | bytes) -> str:
        raw = content if isinstance(content, bytes) else content.encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


DEFAULT_FILE_READ_TRACKER = FileReadTracker()
