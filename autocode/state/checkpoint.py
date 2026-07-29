"""Checkpoint persistence for in-flight sessions."""

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from .model import SessionState
from .session_layout import SessionLayout
from ..message_content import content_text

SESSIONS_DIR = Path(
    os.getenv("AUTOCODE_SESSIONS_DIR", str(Path.home() / ".autocode" / "sessions"))
).expanduser()
_SAFE_SESSION_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_TASK_RE = re.compile(r"[^A-Za-z0-9._-]+")
_CHECKPOINT_CACHE: dict[Path, tuple[int, int, dict]] = {}
_CHECKPOINT_CACHE_LOCK = threading.Lock()
_TURN_QUEUE_LOCK = threading.Lock()


def new_session_id() -> str:
    return f"session_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def new_task_id() -> str:
    return f"task_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _normalize_name(value: str, pattern: re.Pattern[str]) -> str:
    name = pattern.sub("-", value.strip()).strip(".-_")
    if not name:
        raise ValueError("Invalid identifier")
    return name


def session_dir(session_id: str) -> Path:
    return _session_layout().resolve_session(
        _normalize_name(session_id, _SAFE_SESSION_RE)
    )


def _normalize_workspace_root(workspace_root: str | None) -> str:
    if not workspace_root:
        return ""
    try:
        return Path(workspace_root).expanduser().resolve().as_posix()
    except OSError:
        return Path(workspace_root).expanduser().as_posix()


def _workspace_identity(workspace_root: str | None) -> str:
    normalized = _normalize_workspace_root(workspace_root)
    return normalized.casefold() if os.name == "nt" else normalized


def save_checkpoint(session_state: SessionState, messages: list[dict], model: str, workspace_root: str):
    normalized_workspace = _normalize_workspace_root(workspace_root)
    directory = _session_layout().assign_session(
        _normalize_name(session_state.session_id, _SAFE_SESSION_RE),
        normalized_workspace,
    )
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "session": session_state.to_dict(),
        "messages": messages,
        "model": model,
        "workspace_root": normalized_workspace,
        "transcript_file": "transcript.jsonl",
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    path = directory / "checkpoint.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    stat = path.stat()
    with _CHECKPOINT_CACHE_LOCK:
        _CHECKPOINT_CACHE[path] = (stat.st_mtime_ns, stat.st_size, payload)


def save_turn_queue(session_id: str, queued_inputs: list[dict]) -> None:
    """Persist queue control state independently from the running agent checkpoint."""
    directory = session_dir(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "queued_inputs.json"
    temporary = directory / "queued_inputs.tmp"
    with _TURN_QUEUE_LOCK:
        temporary.write_text(
            json.dumps(queued_inputs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)


def load_turn_queue(session_id: str) -> list[dict] | None:
    path = session_dir(session_id) / "queued_inputs.json"
    if not path.exists():
        return None
    try:
        data = _read_json(path)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return list(data) if isinstance(data, list) else None


def load_checkpoint(session_id: str) -> tuple[SessionState, list[dict], str] | None:
    path = session_dir(session_id) / "checkpoint.json"
    if not path.exists():
        return None
    try:
        data = _read_json(path)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    state = SessionState.from_dict(data["session"])
    persisted_queue = load_turn_queue(session_id)
    if persisted_queue is not None:
        state.queued_inputs = persisted_queue
    messages = data["messages"]
    if not state.title:
        state.title = _first_user_title(messages)
    return state, messages, data["model"]


def list_sessions(workspace_root: str | None = None, limit: int = 20) -> list[dict]:
    if not SESSIONS_DIR.exists():
        return []

    workspace_filter = _normalize_workspace_root(workspace_root)
    if workspace_filter:
        directories = _session_layout().workspace_session_directories(workspace_filter)
        return _scan_session_directories(directories)[:limit]

    return _scan_session_directories(
        _session_layout().all_session_directories()
    )[:limit]


def _scan_session_directories(directories: list[Path]) -> list[dict]:
    entries = []
    for directory in sorted(directories, key=lambda item: item.name, reverse=True):
        path = directory / "checkpoint.json"
        try:
            data = _read_cached_json(path)
            entry = _session_summary(data, directory.name)
            entries.append(entry)
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError):
            continue
    return entries


def delete_session(session_id: str, workspace_root: str) -> None:
    """Delete one checkpoint only when it belongs to the requested workspace."""
    directory = session_dir(session_id)
    path = directory / "checkpoint.json"
    if not path.exists():
        raise ValueError(f"Session '{session_id}' not found.")
    data = _read_cached_json(path)
    expected_workspace = _normalize_workspace_root(workspace_root)
    actual_workspace = _normalize_workspace_root(data.get("workspace_root", ""))
    if (
        not expected_workspace
        or _workspace_identity(actual_workspace)
        != _workspace_identity(expected_workspace)
    ):
        raise ValueError(f"Session '{session_id}' is not available for this workspace.")
    shutil.rmtree(directory)
    with _CHECKPOINT_CACHE_LOCK:
        stale_paths = [cached_path for cached_path in _CHECKPOINT_CACHE if directory in cached_path.parents]
        for cached_path in stale_paths:
            _CHECKPOINT_CACHE.pop(cached_path, None)
    _session_layout().remove_location(session_id)


def _session_summary(data: dict, fallback_session_id: str) -> dict:
    session = data.get("session", {})
    current_task = session.get("current_task") or {}
    return {
        "session_id": session.get("session_id", fallback_session_id),
        "task_id": current_task.get("task_id", ""),
        "title": (
            session.get("title")
            or _first_user_title(data.get("messages", []))
            or current_task.get("title", "")
        ),
        "status": current_task.get("status", "idle"),
        "step_index": current_task.get("step_index", 0),
        "saved_at": data.get("saved_at", "?"),
        "model": data.get("model", "?"),
        "workspace_root": _normalize_workspace_root(data.get("workspace_root", "")),
    }


def restore_flat_session_layout() -> int:
    """Restore legacy root-level directories before downgrading AutoCode."""
    return _session_layout().restore_flat()


def migrate_session_storage() -> int:
    """Upgrade legacy root-level sessions to project-partitioned storage."""
    return _session_layout().ensure_migrated()


def _session_layout() -> SessionLayout:
    return SessionLayout(SESSIONS_DIR)


def _first_user_title(messages: list[dict], max_length: int = 120) -> str:
    for message in messages:
        if message.get("role") != "user":
            continue
        title = content_text(message.get("content", "")).strip().splitlines()[0]
        if title:
            return title[:max_length]
    return ""


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_cached_json(path: Path) -> dict:
    stat = path.stat()
    with _CHECKPOINT_CACHE_LOCK:
        cached = _CHECKPOINT_CACHE.get(path)
        if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return cached[2]
    data = _read_json(path)
    with _CHECKPOINT_CACHE_LOCK:
        _CHECKPOINT_CACHE[path] = (stat.st_mtime_ns, stat.st_size, data)
    return data
