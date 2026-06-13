"""Session record persistence."""

from __future__ import annotations

import json
import time

from ..context.todo import render_todos
from .checkpoint import list_sessions, session_dir
from .model import SessionState, TaskState


class SessionStore:
    def sync(self, session_state: SessionState, model: str):
        directory = session_dir(session_state.session_id)
        directory.mkdir(parents=True, exist_ok=True)
        current_task = session_state.current_task
        session_payload = {
            "session_id": session_state.session_id,
            "task_id": current_task.task_id if current_task else "",
            "title": current_task.title if current_task else "",
            "status": current_task.status if current_task else "idle",
            "step_index": current_task.step_index if current_task else 0,
            "transcript_file": "transcript.jsonl",
            "llm_rounds_file": "llm_rounds.md",
            "llm_rounds_raw_file": "llm_rounds.jsonl",
            "current_task_file": "current_task.json",
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": model,
        }
        (directory / "session.json").write_text(json.dumps(session_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        current_task_payload = current_task.to_dict() if current_task else None
        (directory / "current_task.json").write_text(
            json.dumps(current_task_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def load(session_id: str) -> dict | None:
        path = session_dir(session_id) / "session.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def render_task(task_state: TaskState | None) -> str:
        if task_state is None:
            return "Task: (none)\nStatus: idle\nStep: 0\nTodos:\n(no todos)"
        title = task_state.title or "(untitled task)"
        return (
            f"Task Id: {task_state.task_id}\n"
            f"Task: {title}\n"
            f"Status: {task_state.status}\n"
            f"Step: {task_state.step_index}\n"
            f"Todos:\n{render_todos(task_state.todos)}"
        )

    @staticmethod
    def recent_session_summaries(limit: int = 3) -> list[str]:
        items = []
        for entry in list_sessions()[:limit]:
            items.append(
                f"- {entry['session_id']} ({entry['status']}, step {entry['step_index']}, model {entry['model']})"
            )
        return items
