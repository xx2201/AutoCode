"""Task record persistence."""

from __future__ import annotations

import json
import time

from ..context.todo import render_todos
from .checkpoint import list_checkpoints, task_dir
from .model import TaskState


class TaskStore:
    def sync(self, task_state: TaskState, model: str):
        directory = task_dir(task_state.task_id)
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "task_id": task_state.task_id,
            "title": task_state.title,
            "status": task_state.status,
            "step_index": task_state.step_index,
            "todos": task_state.todos,
            "recent_failures": task_state.recent_failures[-5:],
            "transcript_file": "transcript.jsonl",
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": model,
        }
        (directory / "task.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def load(task_id: str) -> dict | None:
        path = task_dir(task_id) / "task.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def render(task_state: TaskState) -> str:
        title = task_state.title or "(untitled task)"
        return (
            f"Task: {title}\n"
            f"Status: {task_state.status}\n"
            f"Step: {task_state.step_index}\n"
            f"Todos:\n{render_todos(task_state.todos)}"
        )

    @staticmethod
    def recent_task_summaries(limit: int = 3) -> list[str]:
        items = []
        for entry in list_checkpoints()[:limit]:
            items.append(
                f"- {entry['task_id']} ({entry['status']}, step {entry['step_index']}, model {entry['model']})"
            )
        return items
