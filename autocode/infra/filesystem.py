"""Workspace-bound filesystem helpers."""

from __future__ import annotations

import shutil
from pathlib import Path

class WorkspaceFS:
    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).expanduser().resolve()

    def resolve_path(self, path: str) -> Path:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.workspace_root / p
        return p.resolve()

    def ensure_within_workspace(self, path: Path):
        try:
            path.relative_to(self.workspace_root)
        except ValueError:
            raise ValueError(f"path must stay inside workspace: {self.workspace_root}")

    def read_text(self, path: str, errors: str = "strict") -> str:
        target = self.resolve_path(path)
        self.ensure_within_workspace(target)
        return target.read_text(encoding="utf-8", errors=errors)

    def write_text(self, path: str, content: str):
        target = self.resolve_path(path)
        self.ensure_within_workspace(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def delete_path(self, path: str, recursive: bool = False) -> Path:
        target = self.resolve_path(path)
        self.ensure_within_workspace(target)
        if target == self.workspace_root:
            raise ValueError("cannot delete the workspace root")
        if not target.exists():
            raise FileNotFoundError(f"{path} not found")
        if target.is_dir():
            if recursive:
                shutil.rmtree(target)
            else:
                target.rmdir()
        else:
            target.unlink()
        return target

    def exists(self, path: str) -> bool:
        target = self.resolve_path(path)
        try:
            self.ensure_within_workspace(target)
        except ValueError:
            return False
        return target.exists()

    def is_file(self, path: str) -> bool:
        target = self.resolve_path(path)
        self.ensure_within_workspace(target)
        return target.is_file()

    def is_dir(self, path: str) -> bool:
        target = self.resolve_path(path)
        self.ensure_within_workspace(target)
        return target.is_dir()

    def glob(self, pattern: str, path: str = ".") -> list[Path]:
        base = self.resolve_path(path)
        self.ensure_within_workspace(base)
        if not base.is_dir():
            raise ValueError(f"{path} is not a directory")
        hits = list(base.glob(pattern))
        results = []
        for item in hits:
            try:
                self.ensure_within_workspace(item.resolve())
            except ValueError:
                continue
            results.append(item.resolve())
        return results
