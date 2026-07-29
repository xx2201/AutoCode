"""Project-partitioned physical storage for persisted sessions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


LAYOUT_VERSION = 3
_SAFE_SESSION_RE = re.compile(r"[^A-Za-z0-9._-]+")
_UNSAFE_PROJECT_CHARACTER_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_LAYOUT_LOCK = threading.RLock()
_READY_ROOTS: set[Path] = set()
_LOCATION_CACHE: dict[tuple[Path, str], Path] = {}
_PROJECT_CACHE: dict[tuple[Path, str], Path] = {}
_RESERVED_DIRECTORIES = {
    ".session-locations",
    ".staging",
    ".workspace-index",
    "projects",
}


class SessionLayoutError(RuntimeError):
    """Raised when persisted sessions cannot be migrated without data loss."""


class SessionLayout:
    """Resolve and migrate session directories under one storage root."""

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.projects_root = self.root / "projects"
        self.locations_root = self.root / ".session-locations"
        self.staging_root = self.root / ".staging"
        self.marker_path = self.root / ".layout.json"

    def ensure_migrated(self) -> int:
        """Move every legacy root-level session into its workspace partition."""
        self.root.mkdir(parents=True, exist_ok=True)
        with _LAYOUT_LOCK:
            if self.root in _READY_ROOTS:
                return 0
        with _LAYOUT_LOCK, self._file_lock():
            if self.root in _READY_ROOTS:
                return 0
            migrated = 0
            for source in self._legacy_session_directories():
                workspace_root = self._workspace_from_checkpoint(source)
                self._assign_locked(source.name, workspace_root, source)
                migrated += 1
            migrated += self._migrate_hashed_projects_locked()
            legacy_index = self.root / ".workspace-index"
            if legacy_index.exists():
                shutil.rmtree(legacy_index)
            self._write_json_atomic(
                self.marker_path,
                {"version": LAYOUT_VERSION},
            )
            _READY_ROOTS.add(self.root)
            return migrated

    def assign_session(self, session_id: str, workspace_root: str) -> Path:
        """Assign a new or staged session to one normalized workspace."""
        name = _session_name(session_id)
        workspace_root = _normalize_workspace_root(workspace_root)
        if not workspace_root:
            raise SessionLayoutError("workspace_root is required for persisted sessions.")
        self.ensure_migrated()
        target = self._project_sessions_dir(workspace_root) / name
        with _LAYOUT_LOCK:
            if _LOCATION_CACHE.get((self.root, name)) == target and target.is_dir():
                return target
        with _LAYOUT_LOCK, self._file_lock():
            source = self._resolve_existing_locked(name)
            return self._assign_locked(name, workspace_root, source)

    def resolve_session(self, session_id: str) -> Path:
        """Resolve by stable id, repairing a missing location pointer if needed."""
        name = _session_name(session_id)
        self.ensure_migrated()
        with _LAYOUT_LOCK:
            cached = _LOCATION_CACHE.get((self.root, name))
            if cached is not None and cached.is_dir():
                return cached
        with _LAYOUT_LOCK, self._file_lock():
            existing = self._resolve_existing_locked(name)
            if existing is not None:
                return existing
            return self.staging_root / name

    def workspace_session_directories(self, workspace_root: str) -> list[Path]:
        """Return only the physical session directories for one workspace."""
        if not workspace_root:
            return []
        self.ensure_migrated()
        directory = self._project_sessions_dir(workspace_root)
        if not directory.exists():
            return []
        return [
            path
            for path in directory.iterdir()
            if path.is_dir() and (path / "checkpoint.json").exists()
        ]

    def all_session_directories(self) -> list[Path]:
        """Return every partitioned session for global administrative listing."""
        self.ensure_migrated()
        return self._all_session_directories()

    def _all_session_directories(self) -> list[Path]:
        if not self.projects_root.exists():
            return []
        directories = []
        for project in self.projects_root.iterdir():
            sessions = project / "sessions"
            if not sessions.is_dir():
                continue
            directories.extend(
                path
                for path in sessions.iterdir()
                if path.is_dir() and (path / "checkpoint.json").exists()
            )
        return directories

    def remove_location(self, session_id: str) -> None:
        name = _session_name(session_id)
        with _LAYOUT_LOCK, self._file_lock():
            _LOCATION_CACHE.pop((self.root, name), None)
            (self.locations_root / f"{name}.json").unlink(missing_ok=True)

    def restore_flat(self) -> int:
        """Move partitioned sessions back to the legacy layout before downgrade."""
        self.ensure_migrated()
        with _LAYOUT_LOCK, self._file_lock():
            if self.staging_root.exists() and any(self.staging_root.iterdir()):
                raise SessionLayoutError("Cannot restore while staged sessions exist.")
            sessions = self._all_session_directories()
            for source in sessions:
                target = self.root / source.name
                if target.exists():
                    raise SessionLayoutError(
                        f"Cannot restore '{source.name}': legacy target already exists."
                    )
                os.replace(source, target)
            if self.projects_root.exists():
                shutil.rmtree(self.projects_root)
            if self.locations_root.exists():
                shutil.rmtree(self.locations_root)
            if self.staging_root.exists():
                self.staging_root.rmdir()
            self.marker_path.unlink(missing_ok=True)
            _READY_ROOTS.discard(self.root)
            for key in [key for key in _LOCATION_CACHE if key[0] == self.root]:
                _LOCATION_CACHE.pop(key, None)
            for key in [key for key in _PROJECT_CACHE if key[0] == self.root]:
                _PROJECT_CACHE.pop(key, None)
            return len(sessions)

    def _migrate_hashed_projects_locked(self) -> int:
        if not self.projects_root.exists():
            return 0
        moved = 0
        for source_project in list(self.projects_root.iterdir()):
            manifest_path = source_project / "project.json"
            if not source_project.is_dir() or not manifest_path.exists():
                continue
            try:
                manifest = _read_json(manifest_path)
                workspace_root = _normalize_workspace_root(
                    str(manifest["workspace_root"])
                )
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                KeyError,
            ) as exc:
                raise SessionLayoutError(
                    f"Invalid project manifest: {manifest_path}"
                ) from exc
            target_project = self._readable_project_dir(workspace_root)
            source_sessions = source_project / "sessions"
            if source_project != target_project:
                target_sessions = target_project / "sessions"
                target_sessions.mkdir(parents=True, exist_ok=True)
                self._write_json_atomic(
                    target_project / "project.json",
                    {
                        "version": LAYOUT_VERSION,
                        "workspace_root": workspace_root,
                    },
                )
                if source_sessions.exists():
                    for source_session in list(source_sessions.iterdir()):
                        if not source_session.is_dir():
                            continue
                        target_session = target_sessions / source_session.name
                        if target_session.exists():
                            raise SessionLayoutError(
                                f"Session migration conflict for "
                                f"'{source_session.name}'."
                            )
                        os.replace(source_session, target_session)
                        self._write_location(
                            source_session.name,
                            workspace_root,
                            target_session,
                        )
                        _LOCATION_CACHE[(self.root, source_session.name)] = (
                            target_session
                        )
                        moved += 1
                try:
                    shutil.rmtree(source_project)
                except PermissionError:
                    # Windows Runner 收尾时可能短暂占用目录句柄。会话正文已逐条
                    # 原子移动，后续启动可以安全重试清理这个空项目外壳。
                    pass
                source_project = target_project
            _PROJECT_CACHE[
                (self.root, _workspace_identity(workspace_root))
            ] = source_project
        return moved

    def _assign_locked(
        self,
        session_id: str,
        workspace_root: str,
        source: Path | None,
    ) -> Path:
        target = self._project_sessions_dir(workspace_root) / session_id
        self._ensure_project_manifest(workspace_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source is not None and source != target:
            if target.exists():
                raise SessionLayoutError(
                    f"Session migration conflict for '{session_id}'."
                )
            os.replace(source, target)
        else:
            target.mkdir(parents=True, exist_ok=True)
        self._write_location(session_id, workspace_root, target)
        _LOCATION_CACHE[(self.root, session_id)] = target
        return target

    def _resolve_existing_locked(self, session_id: str) -> Path | None:
        location_path = self.locations_root / f"{session_id}.json"
        try:
            location = _read_json(location_path)
            target = self._safe_relative_target(str(location["relative_path"]))
            if target.name != session_id or not target.is_dir():
                raise ValueError("Invalid session location target")
            _LOCATION_CACHE[(self.root, session_id)] = target
            return target
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError):
            location_path.unlink(missing_ok=True)

        legacy = self.root / session_id
        if legacy.is_dir():
            _LOCATION_CACHE[(self.root, session_id)] = legacy
            return legacy
        staged = self.staging_root / session_id
        if staged.is_dir():
            _LOCATION_CACHE[(self.root, session_id)] = staged
            return staged

        matches = []
        if self.projects_root.exists():
            matches = [
                candidate
                for candidate in self.projects_root.glob(f"*/sessions/{session_id}")
                if candidate.is_dir()
            ]
        if len(matches) > 1:
            raise SessionLayoutError(
                f"Session '{session_id}' exists in multiple workspace partitions."
            )
        if not matches:
            return None
        target = matches[0]
        project = _read_json(target.parent.parent / "project.json")
        workspace_root = str(project["workspace_root"])
        self._write_location(session_id, workspace_root, target)
        _LOCATION_CACHE[(self.root, session_id)] = target
        return target

    def _legacy_session_directories(self) -> list[Path]:
        directories = []
        for path in self.root.iterdir():
            if not path.is_dir() or path.name in _RESERVED_DIRECTORIES:
                continue
            if (path / "checkpoint.json").exists():
                _session_name(path.name)
                directories.append(path)
        return directories

    @staticmethod
    def _workspace_from_checkpoint(directory: Path) -> str:
        try:
            checkpoint = _read_json(directory / "checkpoint.json")
            workspace_root = str(checkpoint["workspace_root"]).strip()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
            raise SessionLayoutError(
                f"Cannot migrate session '{directory.name}': invalid checkpoint."
            ) from exc
        if not workspace_root:
            raise SessionLayoutError(
                f"Cannot migrate session '{directory.name}': workspace_root is empty."
            )
        return _normalize_workspace_root(workspace_root)

    def _ensure_project_manifest(self, workspace_root: str) -> None:
        project = self._project_dir(workspace_root)
        manifest_path = project / "project.json"
        if manifest_path.exists():
            try:
                manifest = _read_json(manifest_path)
                if not _same_workspace(
                    str(manifest.get("workspace_root", "")),
                    workspace_root,
                ):
                    raise SessionLayoutError("Workspace directory collision detected.")
                return
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SessionLayoutError(
                    f"Invalid project manifest: {manifest_path}"
                ) from exc
        project.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(
            manifest_path,
            {
                "version": LAYOUT_VERSION,
                "workspace_root": workspace_root,
            },
        )

    def _write_location(
        self,
        session_id: str,
        workspace_root: str,
        target: Path,
    ) -> None:
        relative_path = target.relative_to(self.root).as_posix()
        self._write_json_atomic(
            self.locations_root / f"{session_id}.json",
            {
                "version": LAYOUT_VERSION,
                "session_id": session_id,
                "workspace_root": workspace_root,
                "relative_path": relative_path,
            },
        )

    def _project_dir(self, workspace_root: str) -> Path:
        identity = _workspace_identity(workspace_root)
        cached = _PROJECT_CACHE.get((self.root, identity))
        if cached is not None and self._project_matches(cached, workspace_root):
            return cached
        project = self._readable_project_dir(workspace_root)
        _PROJECT_CACHE[(self.root, identity)] = project
        return project

    def _readable_project_dir(self, workspace_root: str) -> Path:
        max_name_length = max(
            24,
            min(160, 235 - len(str(self.projects_root)) - len("/sessions/") - 72),
        )
        readable_name = _project_directory_name(
            workspace_root,
            max_length=max_name_length,
        )
        primary = self.projects_root / readable_name
        if self._project_matches(primary, workspace_root):
            return primary
        digest = hashlib.sha256(_workspace_identity(workspace_root).encode("utf-8"))
        suffix = f"--{digest.hexdigest()[:12]}"
        readable_name = readable_name[: max_name_length - len(suffix)].rstrip("-")
        return self.projects_root / f"{readable_name}{suffix}"

    @staticmethod
    def _project_matches(project: Path, workspace_root: str) -> bool:
        manifest_path = project / "project.json"
        if not project.exists() or not manifest_path.exists():
            return True
        try:
            manifest = _read_json(manifest_path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return True
        return _same_workspace(
            str(manifest.get("workspace_root", "")),
            workspace_root,
        )

    def _project_sessions_dir(self, workspace_root: str) -> Path:
        return self._project_dir(workspace_root) / "sessions"

    def _safe_relative_target(self, relative_path: str) -> Path:
        target = (self.root / Path(relative_path)).resolve()
        if target == self.root or self.root not in target.parents:
            raise ValueError("Session location escapes storage root")
        return target

    def _write_json_atomic(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

    @contextmanager
    def _file_lock(self):
        lock_path = self.root / ".layout.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                deadline = time.monotonic() + 120
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if time.monotonic() >= deadline:
                            raise SessionLayoutError(
                                "Timed out waiting for the session layout lock."
                            ) from exc
                        time.sleep(0.05)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _session_name(value: str) -> str:
    name = _SAFE_SESSION_RE.sub("-", value.strip()).strip(".-_")
    if not name or name != value:
        raise ValueError("Invalid session id")
    return name


def _normalize_workspace_root(workspace_root: str) -> str:
    if not workspace_root:
        return ""
    try:
        return Path(workspace_root).expanduser().resolve().as_posix()
    except OSError:
        return Path(workspace_root).expanduser().as_posix()


def _workspace_identity(workspace_root: str) -> str:
    normalized = _normalize_workspace_root(workspace_root)
    return normalized.casefold() if os.name == "nt" else normalized


def _same_workspace(left: str, right: str) -> bool:
    return _workspace_identity(left) == _workspace_identity(right)


def _project_directory_name(workspace_root: str, *, max_length: int = 160) -> str:
    normalized = _normalize_workspace_root(workspace_root)
    readable = _UNSAFE_PROJECT_CHARACTER_RE.sub("-", normalized).strip("-")
    if not readable:
        raise SessionLayoutError("workspace_root cannot produce a project directory.")
    if re.match(r"^[A-Za-z]-", readable):
        readable = readable[0].upper() + readable[1:]
    if len(readable) > max_length:
        digest = hashlib.sha256(_workspace_identity(normalized).encode("utf-8"))
        suffix = f"--{digest.hexdigest()[:12]}"
        readable = f"{readable[: max_length - len(suffix)].rstrip('-')}{suffix}"
    return readable


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
