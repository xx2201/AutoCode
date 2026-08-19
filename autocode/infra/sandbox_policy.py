"""Shared file-effect policy for trusted tools and untrusted processes."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path


SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}


class SandboxDenied(PermissionError):
    """The active sandbox policy rejected a file mutation."""

    def __init__(self, mode: str, path: Path):
        self.mode = mode
        self.path = path
        super().__init__(f'sandbox mode "{mode}" denies writing {path}')


@dataclass(frozen=True)
class SandboxExecutionPolicy:
    mode: str
    workspace_root: Path

    def can_write(self, path: Path) -> bool:
        target = path.expanduser().resolve()
        if self.mode == "danger-full-access":
            return True
        if self.mode == "read-only":
            return False
        try:
            target.relative_to(self.workspace_root)
            return True
        except ValueError:
            return False

    def require_write(self, path: Path) -> None:
        target = path.expanduser().resolve()
        if not self.can_write(target):
            raise SandboxDenied(self.mode, target)


class SandboxPolicy:
    """Single owner of the standing sandbox mode and canonical workspace root."""

    def __init__(self, workspace_root: str, mode: str = "workspace-write"):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self._lock = threading.RLock()
        self._mode = ""
        self.set_mode(mode)

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in SANDBOX_MODES:
            raise ValueError(f"Unsupported sandbox mode: {mode}")
        with self._lock:
            self._mode = mode

    def resolve(self, mode: str | None = None) -> SandboxExecutionPolicy:
        selected = mode or self.mode
        if selected not in SANDBOX_MODES:
            raise ValueError(f"Unsupported sandbox mode: {selected}")
        return SandboxExecutionPolicy(selected, self.workspace_root)
