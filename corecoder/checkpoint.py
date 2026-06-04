"""Checkpoint persistence for in-flight tasks."""

import json
import re
import time
import uuid
from pathlib import Path

from .state import TaskState

TASKS_DIR = Path.home() / ".corecoder" / "tasks"
_SAFE_TASK_RE = re.compile(r"[^A-Za-z0-9._-]+")


def new_task_id() -> str:
    return f"task_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _normalize_task_id(task_id: str) -> str:
    name = _SAFE_TASK_RE.sub("-", task_id.strip()).strip(".-_")
    if not name:
        raise ValueError("Invalid task id")
    return name


def task_dir(task_id: str) -> Path:
    root = TASKS_DIR.resolve()
    target = (root / _normalize_task_id(task_id)).resolve()
    if target.parent != root:
        raise ValueError("Invalid task id")
    return target


def save_checkpoint(task_state: TaskState, messages: list[dict], model: str):
    directory = task_dir(task_state.task_id)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "task": task_state.to_dict(),
        "messages": messages,
        "model": model,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (directory / "checkpoint.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_checkpoint(task_id: str) -> tuple[TaskState, list[dict], str] | None:
    path = task_dir(task_id) / "checkpoint.json"
    if not path.exists():
        return None
    try:
        data = _read_json(path)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return TaskState.from_dict(data["task"]), data["messages"], data["model"]


def list_checkpoints() -> list[dict]:
    if not TASKS_DIR.exists():
        return []

    entries = []
    for directory in sorted(TASKS_DIR.iterdir(), reverse=True):
        if not directory.is_dir():
            continue
        path = directory / "checkpoint.json"
        if not path.exists():
            continue
        try:
            data = _read_json(path)
            task = data.get("task", {})
            entries.append({
                "task_id": task.get("task_id", directory.name),
                "status": task.get("status", "?"),
                "step_index": task.get("step_index", 0),
                "saved_at": data.get("saved_at", "?"),
                "model": data.get("model", "?"),
            })
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError):
            continue
    return entries[:20]


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
