"""Per-turn workspace snapshots with conflict-safe undo and reapply.

The store deliberately operates on the Git working tree instead of Git's index.
It therefore preserves any uncommitted baseline that existed before a turn.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .checkpoint import session_dir


DEFAULT_MAX_FILES = 10_000
DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MANIFEST_VERSION = 1


class ChangeSetError(RuntimeError):
    """Base error for ChangeSet operations."""


class ChangeSetLimitError(ChangeSetError):
    """Raised when a workspace exceeds configured capture limits."""


class ChangeSetConflictError(ChangeSetError):
    """Raised before applying a ChangeSet whose expected baseline has drifted."""


class ChangeSetUnavailableError(ChangeSetError):
    """Raised when a ChangeSet cannot be safely applied."""


FileKind = Literal["missing", "file", "symlink"]


@dataclass(frozen=True)
class FileState:
    kind: FileKind
    sha256: str | None
    mode: int | None
    data: bytes | None

    def metadata(self, blob: str | None) -> dict:
        return {
            "kind": self.kind,
            "sha256": self.sha256,
            "mode": self.mode,
            "size": len(self.data) if self.data is not None else 0,
            "blob": blob,
        }


@dataclass(frozen=True)
class GitState:
    head: str
    index_sha256: str

    def to_dict(self) -> dict:
        return {"head": self.head, "index_sha256": self.index_sha256}


@dataclass(frozen=True)
class WorkspaceSnapshot:
    turn_id: str
    workspace_root: str
    files: dict[str, FileState]
    git: GitState


@dataclass(frozen=True)
class ChangeSetManifest:
    session_id: str
    turn_id: str
    workspace_root: str
    created_at: str
    git_before: GitState
    git_after: GitState
    files: tuple[dict, ...]
    applicable: bool
    blocked_reason: str
    state: Literal["applied", "undone"]

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(item["path"] for item in self.files)

    def to_dict(self) -> dict:
        return {
            "version": _MANIFEST_VERSION,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "workspace_root": self.workspace_root,
            "created_at": self.created_at,
            "git_before": self.git_before.to_dict(),
            "git_after": self.git_after.to_dict(),
            "files": list(self.files),
            "applicable": self.applicable,
            "blocked_reason": self.blocked_reason,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ChangeSetManifest":
        if data.get("version") != _MANIFEST_VERSION:
            raise ChangeSetError("Unsupported ChangeSet manifest version.")
        return cls(
            session_id=str(data["session_id"]),
            turn_id=str(data["turn_id"]),
            workspace_root=str(data["workspace_root"]),
            created_at=str(data["created_at"]),
            git_before=GitState(**data["git_before"]),
            git_after=GitState(**data["git_after"]),
            files=tuple(data["files"]),
            applicable=bool(data["applicable"]),
            blocked_reason=str(data.get("blocked_reason", "")),
            state=data["state"],
        )


class ChangeSetStore:
    """Capture and apply file changes made by individual agent turns."""

    def __init__(
        self,
        workspace_root: str | Path,
        session_id: str,
        *,
        changes_root: str | Path | None = None,
        max_files: int = DEFAULT_MAX_FILES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("Workspace root must be a directory.")
        self.session_id = _validate_id(session_id, "session id")
        self.changes_root = (
            Path(changes_root).expanduser().resolve()
            if changes_root is not None
            else session_dir(session_id) / "changes"
        )
        self.max_files = _positive_limit(max_files, "max_files")
        self.max_total_bytes = _positive_limit(max_total_bytes, "max_total_bytes")
        self.max_file_bytes = _positive_limit(max_file_bytes, "max_file_bytes")
        top_level = Path(self._git("rev-parse", "--show-toplevel").decode().strip()).resolve()
        if top_level != self.workspace_root:
            raise ValueError("Workspace root must be the Git repository root.")

    def capture_before(self, turn_id: str) -> WorkspaceSnapshot:
        """Capture the exact working-tree baseline immediately before a turn."""
        _validate_id(turn_id, "turn id")
        if self._turn_dir(turn_id).exists():
            raise ChangeSetError(f"ChangeSet for turn '{turn_id}' already exists.")
        return self._capture_workspace(turn_id)

    def capture_after(
        self, turn_id: str, before: WorkspaceSnapshot
    ) -> ChangeSetManifest:
        """Capture turn output and atomically persist only files that changed."""
        turn_id = _validate_id(turn_id, "turn id")
        if before.turn_id != turn_id or Path(before.workspace_root).resolve() != self.workspace_root:
            raise ChangeSetError("Before snapshot belongs to another turn or workspace.")
        final_dir = self._turn_dir(turn_id)
        if final_dir.exists():
            raise ChangeSetError(f"ChangeSet for turn '{turn_id}' already exists.")
        after = self._capture_workspace(turn_id)
        paths = sorted(set(before.files) | set(after.files))
        changed = [
            path
            for path in paths
            if _state_identity(before.files.get(path, _missing_state()))
            != _state_identity(after.files.get(path, _missing_state()))
        ]
        applicable = before.git == after.git
        blocked_reason = "" if applicable else "Git HEAD or index changed during the turn."

        self.changes_root.mkdir(parents=True, exist_ok=True)
        temporary_dir = Path(tempfile.mkdtemp(prefix=f".{turn_id}-", dir=self.changes_root))
        try:
            entries = []
            for index, relative_path in enumerate(changed):
                before_state = before.files.get(relative_path, _missing_state())
                after_state = after.files.get(relative_path, _missing_state())
                before_blob = self._write_blob(temporary_dir, "before", index, before_state)
                after_blob = self._write_blob(temporary_dir, "after", index, after_state)
                entries.append(
                    {
                        "path": relative_path,
                        "before": before_state.metadata(before_blob),
                        "after": after_state.metadata(after_blob),
                    }
                )
            manifest = ChangeSetManifest(
                session_id=self.session_id,
                turn_id=turn_id,
                workspace_root=self.workspace_root.as_posix(),
                created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                git_before=before.git,
                git_after=after.git,
                files=tuple(entries),
                applicable=applicable,
                blocked_reason=blocked_reason,
                state="applied",
            )
            self._write_json_atomic(temporary_dir / "manifest.json", manifest.to_dict())
            os.replace(temporary_dir, final_dir)
            return manifest
        except BaseException:
            shutil.rmtree(temporary_dir, ignore_errors=True)
            raise

    def load(self, turn_id: str) -> ChangeSetManifest:
        turn_dir = self._turn_dir(_validate_id(turn_id, "turn id"))
        path = turn_dir / "manifest.json"
        if not path.is_file():
            raise ChangeSetUnavailableError(f"ChangeSet for turn '{turn_id}' was not found.")
        try:
            manifest = ChangeSetManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ChangeSetError("Invalid ChangeSet manifest.") from exc
        if manifest.session_id != self.session_id or manifest.turn_id != turn_id:
            raise ChangeSetError("ChangeSet manifest identity does not match its directory.")
        if Path(manifest.workspace_root).resolve() != self.workspace_root:
            raise ChangeSetUnavailableError("ChangeSet belongs to another workspace.")
        return manifest

    def undo(self, turn_id: str) -> ChangeSetManifest:
        """Restore the pre-turn state after validating the complete ChangeSet."""
        return self._apply(turn_id, expected_side="after", desired_side="before", next_state="undone")

    def reapply(self, turn_id: str) -> ChangeSetManifest:
        """Restore the post-turn state after validating the complete ChangeSet."""
        return self._apply(turn_id, expected_side="before", desired_side="after", next_state="applied")

    def _apply(
        self,
        turn_id: str,
        *,
        expected_side: Literal["before", "after"],
        desired_side: Literal["before", "after"],
        next_state: Literal["applied", "undone"],
    ) -> ChangeSetManifest:
        manifest = self.load(turn_id)
        expected_state = "applied" if expected_side == "after" else "undone"
        if manifest.state != expected_state:
            raise ChangeSetUnavailableError(
                f"ChangeSet is '{manifest.state}', expected '{expected_state}'."
            )
        if not manifest.applicable:
            raise ChangeSetUnavailableError(manifest.blocked_reason)
        expected_git = manifest.git_after if expected_side == "after" else manifest.git_before
        if self._git_state() != expected_git:
            raise ChangeSetConflictError("Git HEAD or index no longer matches the ChangeSet baseline.")

        operations = []
        for item in manifest.files:
            target = self._workspace_path(item["path"])
            actual = self._read_path(target, enforce_size=False)
            expected = item[expected_side]
            if _state_identity(actual) != _metadata_identity(expected):
                raise ChangeSetConflictError(f"File changed since capture: {item['path']}")
            desired = self._state_from_metadata(self._turn_dir(turn_id), item[desired_side])
            operations.append((target, desired, actual))

        completed: list[tuple[Path, FileState]] = []
        updated = ChangeSetManifest(
            **{**manifest.__dict__, "state": next_state}
        )
        try:
            for target, desired, original in operations:
                self._replace_path(target, desired)
                completed.append((target, original))
            self._write_json_atomic(
                self._turn_dir(turn_id) / "manifest.json", updated.to_dict()
            )
        except BaseException as exc:
            rollback_errors = []
            for target, original in reversed(completed):
                try:
                    self._replace_path(target, original)
                except OSError as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            if rollback_errors:
                raise ChangeSetError(
                    "ChangeSet apply failed and rollback was incomplete: " + "; ".join(rollback_errors)
                ) from exc
            raise ChangeSetError("ChangeSet apply failed; completed writes were rolled back.") from exc

        return updated

    def _capture_workspace(self, turn_id: str) -> WorkspaceSnapshot:
        raw = self._git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
        decoded_paths = {os.fsdecode(item) for item in raw.split(b"\0") if item}
        if len(decoded_paths) > self.max_files:
            raise ChangeSetLimitError(
                f"Workspace contains {len(decoded_paths)} files; limit is {self.max_files}."
            )
        files: dict[str, FileState] = {}
        total_bytes = 0
        for relative_path in sorted(decoded_paths):
            normalized = _validate_relative_path(relative_path)
            state = self._read_path(self._workspace_path(normalized), enforce_size=True)
            total_bytes += len(state.data or b"")
            if total_bytes > self.max_total_bytes:
                raise ChangeSetLimitError(
                    f"Workspace snapshot exceeds {self.max_total_bytes} bytes."
                )
            files[normalized] = state
        return WorkspaceSnapshot(
            turn_id=turn_id,
            workspace_root=self.workspace_root.as_posix(),
            files=files,
            git=self._git_state(),
        )

    def _read_path(self, path: Path, *, enforce_size: bool) -> FileState:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return _missing_state()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            data = os.fsencode(os.readlink(path))
            kind: FileKind = "symlink"
        elif stat.S_ISREG(info.st_mode):
            if enforce_size and info.st_size > self.max_file_bytes:
                relative = path.relative_to(self.workspace_root).as_posix()
                raise ChangeSetLimitError(
                    f"File '{relative}' exceeds {self.max_file_bytes} bytes."
                )
            data = path.read_bytes()
            kind = "file"
        else:
            relative = path.relative_to(self.workspace_root).as_posix()
            raise ChangeSetUnavailableError(f"Unsupported file type: {relative}")
        return FileState(kind=kind, sha256=_sha256(data), mode=mode, data=data)

    def _state_from_metadata(self, turn_dir: Path, metadata: dict) -> FileState:
        kind = metadata.get("kind")
        if kind == "missing":
            return _missing_state()
        if kind not in {"file", "symlink"}:
            raise ChangeSetError("Invalid file kind in ChangeSet manifest.")
        blob = metadata.get("blob")
        if not isinstance(blob, str):
            raise ChangeSetError("Missing ChangeSet blob.")
        blob_path = _safe_child(turn_dir, blob)
        data = blob_path.read_bytes()
        if _sha256(data) != metadata.get("sha256") or len(data) != metadata.get("size"):
            raise ChangeSetError("ChangeSet blob failed integrity validation.")
        return FileState(kind=kind, sha256=metadata["sha256"], mode=metadata["mode"], data=data)

    def _replace_path(self, target: Path, state: FileState) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if state.kind == "missing":
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            return
        if target.exists() and target.is_dir() and not target.is_symlink():
            raise ChangeSetConflictError(f"Refusing to replace directory: {target}")
        temporary = target.parent / f".{target.name}.autocode-{os.getpid()}-{time.time_ns()}"
        try:
            if state.kind == "file":
                temporary.write_bytes(state.data or b"")
                if state.mode is not None:
                    os.chmod(temporary, state.mode)
            else:
                os.symlink(os.fsdecode(state.data or b""), temporary)
            os.replace(temporary, target)
        finally:
            try:
                if temporary.is_symlink() or temporary.exists():
                    temporary.unlink()
            except OSError:
                pass

    def _write_blob(self, directory: Path, side: str, index: int, state: FileState) -> str | None:
        if state.kind == "missing":
            return None
        relative = f"{side}/{index:06d}.bin"
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(state.data or b"")
        return relative

    def _git_state(self) -> GitState:
        head_oid = self._git_optional("rev-parse", "--verify", "HEAD").decode().strip()
        head_ref = self._git_optional("symbolic-ref", "-q", "HEAD").decode().strip()
        index_path_raw = self._git("rev-parse", "--git-path", "index").decode().strip()
        index_path = Path(index_path_raw)
        if not index_path.is_absolute():
            index_path = self.workspace_root / index_path
        index_bytes = index_path.read_bytes() if index_path.is_file() else b""
        return GitState(
            head=f"{head_ref}\0{head_oid}",
            index_sha256=_sha256(index_bytes),
        )

    def _git(self, *arguments: str) -> bytes:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=self.workspace_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            message = completed.stderr.decode(errors="replace").strip()
            raise ChangeSetUnavailableError(message or "Git command failed.")
        return completed.stdout

    def _git_optional(self, *arguments: str) -> bytes:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=self.workspace_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return completed.stdout if completed.returncode == 0 else b""

    def _turn_dir(self, turn_id: str) -> Path:
        return _safe_child(self.changes_root, turn_id)

    def _workspace_path(self, relative_path: str) -> Path:
        normalized = _validate_relative_path(relative_path)
        candidate = self.workspace_root.joinpath(*normalized.split("/"))
        parent = candidate.parent.resolve(strict=False)
        try:
            parent.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ChangeSetError(f"Path escapes workspace: {relative_path}") from exc
        return candidate

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _missing_state() -> FileState:
    return FileState(kind="missing", sha256=None, mode=None, data=None)


def _state_identity(state: FileState) -> tuple:
    return state.kind, state.sha256, state.mode


def _metadata_identity(metadata: dict) -> tuple:
    return metadata.get("kind"), metadata.get("sha256"), metadata.get("mode")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValueError(f"Invalid {label}.")
    return value


def _validate_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ChangeSetError("Invalid workspace path.")
    normalized = value.replace("\\", "/")
    path = Path(normalized)
    if path.is_absolute() or normalized.startswith("/") or any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ChangeSetError(f"Unsafe workspace path: {value}")
    return normalized


def _safe_child(root: Path, relative_path: str) -> Path:
    root = root.resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ChangeSetError(f"Path escapes ChangeSet directory: {relative_path}") from exc
    return candidate


def _positive_limit(value: int, name: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value
