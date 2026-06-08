"""Workspace-bound filesystem helpers."""

from __future__ import annotations

from pathlib import Path

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", "dist", "build"}


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

    def walk_files(self, path: str = ".", include: str | None = None, limit: int = 5000) -> list[Path]:
        base = self.resolve_path(path)
        self.ensure_within_workspace(base)
        if not base.exists():
            raise ValueError(f"{path} not found")
        if base.is_file():
            return [base]

        results = []
        for item in base.rglob(include or "*"):
            if any(part in _SKIP_DIRS for part in item.parts):
                continue
            if item.is_file():
                results.append(item.resolve())
            if len(results) >= limit:
                break
        return results
