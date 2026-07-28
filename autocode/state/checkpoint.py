"""Checkpoint persistence for in-flight sessions."""

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from .model import SessionState
from ..message_content import content_text

SESSIONS_DIR = Path(
    os.getenv("AUTOCODE_SESSIONS_DIR", str(Path.home() / ".autocode" / "sessions"))
).expanduser()
_SAFE_SESSION_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_TASK_RE = re.compile(r"[^A-Za-z0-9._-]+")
_CHECKPOINT_CACHE: dict[Path, tuple[int, int, dict]] = {}
_CHECKPOINT_CACHE_LOCK = threading.Lock()
_SESSION_INDEX_LOCK = threading.RLock()
_TURN_QUEUE_LOCK = threading.Lock()
_SESSION_INDEX_VERSION = 1


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
    root = SESSIONS_DIR.resolve()
    target = (root / _normalize_name(session_id, _SAFE_SESSION_RE)).resolve()
    if target.parent != root:
        raise ValueError("Invalid session id")
    return target


def _normalize_workspace_root(workspace_root: str | None) -> str:
    if not workspace_root:
        return ""
    try:
        return Path(workspace_root).expanduser().resolve().as_posix().lower()
    except OSError:
        return Path(workspace_root).expanduser().as_posix().lower()


def save_checkpoint(session_state: SessionState, messages: list[dict], model: str, workspace_root: str):
    directory = session_dir(session_state.session_id)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "session": session_state.to_dict(),
        "messages": messages,
        "model": model,
        "workspace_root": _normalize_workspace_root(workspace_root),
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
    _upsert_workspace_index(_session_summary(payload, directory.name))


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
        with _SESSION_INDEX_LOCK:
            entries = list(_load_workspace_index(workspace_filter).values())
        return sorted(entries, key=lambda item: item["session_id"], reverse=True)[:limit]

    return _scan_sessions()[:limit]


def _scan_sessions(workspace_filter: str = "") -> list[dict]:
    entries = []
    for directory in sorted(SESSIONS_DIR.iterdir(), reverse=True):
        if not directory.is_dir():
            continue
        path = directory / "checkpoint.json"
        if not path.exists():
            continue
        try:
            data = _read_cached_json(path)
            entry = _session_summary(data, directory.name)
            item_workspace = entry["workspace_root"]
            if workspace_filter and item_workspace != workspace_filter:
                continue
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
    if not expected_workspace or actual_workspace != expected_workspace:
        raise ValueError(f"Session '{session_id}' is not available for this workspace.")
    shutil.rmtree(directory)
    with _CHECKPOINT_CACHE_LOCK:
        stale_paths = [cached_path for cached_path in _CHECKPOINT_CACHE if directory in cached_path.parents]
        for cached_path in stale_paths:
            _CHECKPOINT_CACHE.pop(cached_path, None)
    _remove_from_workspace_index(actual_workspace, session_id)


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


def _workspace_index_dir(workspace_root: str) -> Path:
    digest = hashlib.sha256(workspace_root.encode("utf-8")).hexdigest()
    return SESSIONS_DIR / ".workspace-index" / digest


def _workspace_index_entry_path(workspace_root: str, session_id: str) -> Path:
    name = _normalize_name(session_id, _SAFE_SESSION_RE)
    return _workspace_index_dir(workspace_root) / f"{name}.json"


def _load_workspace_index(workspace_root: str) -> dict[str, dict]:
    directory = _workspace_index_dir(workspace_root)
    ready_path = directory / ".ready"
    try:
        ready = _read_json(ready_path)
        if ready != {
            "version": _SESSION_INDEX_VERSION,
            "workspace_root": workspace_root,
        }:
            raise ValueError("Session index marker mismatch")
        sessions = {}
        for path in directory.glob("*.json"):
            entry = _read_json(path)
            session_id = str(entry.get("session_id", ""))
            if (
                not session_id
                or entry.get("workspace_root") != workspace_root
                or _workspace_index_entry_path(workspace_root, session_id) != path
            ):
                raise ValueError("Invalid session index entry")
            if not (session_dir(session_id) / "checkpoint.json").exists():
                path.unlink(missing_ok=True)
                continue
            sessions[session_id] = entry
        return sessions
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _rebuild_workspace_index(workspace_root)


def _rebuild_workspace_index(workspace_root: str) -> dict[str, dict]:
    sessions = {
        entry["session_id"]: entry
        for entry in _scan_sessions(workspace_root)
    }
    directory = _workspace_index_dir(workspace_root)
    directory.mkdir(parents=True, exist_ok=True)
    expected_paths = {
        _workspace_index_entry_path(workspace_root, session_id)
        for session_id in sessions
    }
    for path in directory.glob("*.json"):
        if path not in expected_paths:
            path.unlink(missing_ok=True)
    for entry in sessions.values():
        _write_workspace_index_entry(entry)
    _write_json_atomic(
        directory / ".ready",
        {
            "version": _SESSION_INDEX_VERSION,
            "workspace_root": workspace_root,
        },
    )
    return sessions


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_workspace_index_entry(entry: dict) -> None:
    _write_json_atomic(
        _workspace_index_entry_path(entry["workspace_root"], entry["session_id"]),
        entry,
    )


def _upsert_workspace_index(entry: dict) -> None:
    workspace_root = entry["workspace_root"]
    if not workspace_root:
        return
    with _SESSION_INDEX_LOCK:
        _load_workspace_index(workspace_root)
        _write_workspace_index_entry(entry)


def _remove_from_workspace_index(workspace_root: str, session_id: str) -> None:
    if not workspace_root:
        return
    with _SESSION_INDEX_LOCK:
        _load_workspace_index(workspace_root)
        _workspace_index_entry_path(workspace_root, session_id).unlink(missing_ok=True)


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
