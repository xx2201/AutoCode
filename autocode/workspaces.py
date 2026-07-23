"""Persistent workspace registry shared by the CLI and local Web runner."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REGISTRY_VERSION = 1
_REGISTRY_LOCK = threading.RLock()


@dataclass(frozen=True)
class WorkspaceInfo:
    workspace_id: str
    name: str
    path: str
    last_opened_at: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class WorkspaceRegistry:
    """Store projects that have been opened explicitly by the local CLI."""

    def __init__(self, registry_file: str | Path | None = None):
        configured_path = (
            registry_file
            or os.getenv("AUTOCODE_WORKSPACES_FILE")
            or Path.home() / ".autocode" / "workspaces.json"
        )
        self.path = Path(configured_path).expanduser().resolve()

    def register(self, workspace_path: str | Path) -> WorkspaceInfo:
        """Register an existing project when the CLI opens it."""
        resolved = Path(workspace_path).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"Workspace directory does not exist: {resolved}")

        now = datetime.now(timezone.utc).isoformat()
        normalized_path = _normalized_path(resolved)
        workspace = WorkspaceInfo(
            workspace_id=_workspace_id(normalized_path),
            name=resolved.name,
            path=str(resolved),
            last_opened_at=now,
        )

        with _REGISTRY_LOCK:
            entries = self._read_entries()
            entries = [
                entry
                for entry in entries
                if entry.get("workspace_id") != workspace.workspace_id
            ]
            entries.append(workspace.to_dict())
            self._write_entries(entries)
        return workspace

    def list_workspaces(self) -> list[dict[str, str]]:
        """Return registered projects that still exist, most recent first."""
        with _REGISTRY_LOCK:
            entries = self._read_entries()

        workspaces: list[dict[str, str]] = []
        for entry in entries:
            workspace = _validated_entry(entry)
            if workspace is None or not Path(workspace.path).is_dir():
                continue
            workspaces.append(workspace.to_dict())
        workspaces.sort(
            key=lambda item: (
                item["last_opened_at"],
                item["name"].casefold(),
            ),
            reverse=True,
        )
        return workspaces

    def resolve(self, workspace_id: str) -> Path:
        """Resolve only an existing project already present in the CLI registry."""
        for workspace in self.list_workspaces():
            if workspace["workspace_id"] == workspace_id:
                return Path(workspace["path"]).resolve()
        raise ValueError(
            "Workspace is not registered by the local CLI. "
            "Open the project in AutoCode CLI, then refresh the Web project list."
        )

    def _read_entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read workspace registry: {self.path}") from exc
        if not isinstance(payload, dict) or payload.get("version") != _REGISTRY_VERSION:
            raise RuntimeError(f"Unsupported workspace registry format: {self.path}")
        entries = payload.get("workspaces")
        if not isinstance(entries, list):
            raise RuntimeError(f"Invalid workspace registry entries: {self.path}")
        return [entry for entry in entries if isinstance(entry, dict)]

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _REGISTRY_VERSION,
            "workspaces": entries,
        }
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                json.dump(payload, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


def _validated_entry(entry: dict[str, Any]) -> WorkspaceInfo | None:
    required = ("workspace_id", "name", "path", "last_opened_at")
    if any(not isinstance(entry.get(field), str) or not entry[field] for field in required):
        return None
    resolved = Path(entry["path"]).expanduser().resolve()
    normalized_path = _normalized_path(resolved)
    if entry["workspace_id"] != _workspace_id(normalized_path):
        return None
    return WorkspaceInfo(
        workspace_id=entry["workspace_id"],
        name=entry["name"],
        path=str(resolved),
        last_opened_at=entry["last_opened_at"],
    )


def _normalized_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _workspace_id(normalized_path: str) -> str:
    return hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()[:20]
