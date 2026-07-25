"""Secure, short-lived file offers for the Web channel."""

from __future__ import annotations

import base64
import mimetypes
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..tools.base import Tool

MAX_WEB_FILE_BYTES = 25 * 1024 * 1024
WEB_FILE_TTL_SECONDS = 60 * 60
_MAX_RECORDS = 256
_PROTECTED_DIRECTORIES = {".git", ".autocode"}
_IGNORED_BROWSER_DIRECTORIES = {
    ".git",
    ".autocode",
    ".venv",
    "__pycache__",
    "node_modules",
}
MAX_WORKSPACE_FILES = 10_000
MAX_WORKSPACE_FILE_BYTES = 1024 * 1024


class WorkspaceFileBrowser:
    """List and read safe project files without exposing local secrets."""

    def __init__(self, workspace_root: Path):
        self.root = workspace_root.resolve()

    def list_files(self) -> dict:
        files: list[str] = []
        truncated = False
        for directory, names, filenames in os.walk(self.root):
            names[:] = sorted(
                name
                for name in names
                if name.lower() not in _IGNORED_BROWSER_DIRECTORIES
                and not name.lower().startswith(".env")
            )
            current = Path(directory)
            for name in sorted(filenames):
                path = current / name
                relative = path.relative_to(self.root)
                if self._is_protected(relative):
                    continue
                files.append(relative.as_posix())
                if len(files) >= MAX_WORKSPACE_FILES:
                    truncated = True
                    return {"files": files, "truncated": truncated}
        return {"files": files, "truncated": truncated}

    def read(self, file_path: str) -> dict:
        normalized = file_path.strip().replace("\\", "/")
        if not normalized or len(normalized) > 1000:
            raise ValueError("Invalid workspace file path.")
        path = (self.root / normalized).resolve()
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Workspace file must stay inside the project.") from exc
        if self._is_protected(relative):
            raise ValueError("Protected workspace files cannot be viewed through Web.")
        if not path.is_file():
            raise ValueError("Workspace file does not exist or is not a file.")

        size = path.stat().st_size
        data = path.read_bytes()[: MAX_WORKSPACE_FILE_BYTES + 1]
        truncated = len(data) > MAX_WORKSPACE_FILE_BYTES
        data = data[:MAX_WORKSPACE_FILE_BYTES]
        if b"\0" in data:
            return {
                "path": relative.as_posix(),
                "content": "",
                "size": size,
                "binary": True,
                "truncated": False,
            }
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "path": relative.as_posix(),
                "content": "",
                "size": size,
                "binary": True,
                "truncated": False,
            }
        return {
            "path": relative.as_posix(),
            "content": content,
            "size": size,
            "binary": False,
            "truncated": truncated,
        }

    @staticmethod
    def _is_protected(relative: Path) -> bool:
        return any(
            part.lower() in _PROTECTED_DIRECTORIES or part.lower().startswith(".env")
            for part in relative.parts
        )


class WebSendTool(Tool):
    """Offer one workspace file to the current authenticated Web conversation."""

    name = "web_send"
    description = (
        "Send a local file from the current workspace to the current Web chat so the "
        "user can preview or download it on their phone. Use this whenever the user "
        "asks you to send, share, show, or provide a local file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to an existing file inside the current workspace.",
            },
        },
        "required": ["file_path"],
    }

    def __init__(self, sender=None):
        self._sender = sender

    def clone(self) -> "WebSendTool":
        return type(self)(self._sender)

    def execute(self, file_path: str) -> str:
        if self._sender is None:
            return "Error: web_send tool is not initialized."
        try:
            return self._sender(file_path)
        except Exception as exc:
            return f"Error sending Web attachment: {exc}"


@dataclass(frozen=True)
class _WebFileRecord:
    workspace_id: str
    workspace_root: Path
    path: Path
    name: str
    media_type: str
    size: int
    expires_at: float


class WebFileStore:
    """Keep opaque file handles in the local Runner; file bytes stay local until fetched."""

    def __init__(self, *, ttl_seconds: int = WEB_FILE_TTL_SECONDS):
        self._ttl_seconds = ttl_seconds
        self._records: dict[str, _WebFileRecord] = {}
        self._lock = threading.Lock()

    def offer(self, workspace_id: str, workspace_root: Path, file_path: str) -> dict:
        root = workspace_root.resolve()
        path = Path(file_path).expanduser()
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        self._validate_path(root, path)

        size = path.stat().st_size
        if size > MAX_WEB_FILE_BYTES:
            raise ValueError("File exceeds the 25 MB Web transfer limit.")
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        file_id = secrets.token_urlsafe(24)
        record = _WebFileRecord(
            workspace_id=workspace_id,
            workspace_root=root,
            path=path,
            name=path.name,
            media_type=media_type,
            size=size,
            expires_at=time.monotonic() + self._ttl_seconds,
        )
        with self._lock:
            self._clean_locked()
            if len(self._records) >= _MAX_RECORDS:
                oldest = min(self._records, key=lambda item: self._records[item].expires_at)
                self._records.pop(oldest, None)
            self._records[file_id] = record
        return self._public_metadata(file_id, record)

    def read(self, workspace_id: str, file_id: str) -> dict:
        with self._lock:
            self._clean_locked()
            record = self._records.get(file_id)
        if record is None or record.workspace_id != workspace_id:
            raise ValueError("File offer is invalid or has expired.")

        path = record.path.resolve()
        self._validate_path(record.workspace_root, path)
        size = path.stat().st_size
        if size > MAX_WEB_FILE_BYTES:
            raise ValueError("File exceeds the 25 MB Web transfer limit.")
        return {
            **self._public_metadata(file_id, record),
            "size": size,
            "data_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
        }

    @staticmethod
    def _validate_path(root: Path, path: Path) -> None:
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise ValueError("Web attachments must be inside the current workspace.") from exc
        if any(
            part.lower() in _PROTECTED_DIRECTORIES or part.lower().startswith(".env")
            for part in relative.parts
        ):
            raise ValueError("Protected workspace files cannot be sent through Web.")
        if not path.is_file():
            raise ValueError("Web attachment does not exist or is not a file.")

    @staticmethod
    def _public_metadata(file_id: str, record: _WebFileRecord) -> dict:
        return {
            "file_id": file_id,
            "name": record.name,
            "media_type": record.media_type,
            "size": record.size,
            "can_preview": (
                record.media_type.startswith("image/")
                or record.media_type == "application/pdf"
            ),
        }

    def _clean_locked(self) -> None:
        now = time.monotonic()
        expired = [
            file_id
            for file_id, record in self._records.items()
            if record.expires_at <= now
        ]
        for file_id in expired:
            self._records.pop(file_id, None)
