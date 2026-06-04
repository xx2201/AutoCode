"""Todo writing tool for explicit planning."""

from .base import Tool
from ..todo import normalize_todos, render_todos


class TodoWriteTool(Tool):
    name = "todo_write"
    description = (
        "Create or update the task todo list. "
        "Use this for multi-step tasks to keep an explicit plan with statuses."
    )
    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Todo items with content and status",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string"},
                    },
                    "required": ["content"],
                },
            },
        },
        "required": ["todos"],
    }

    _parent_agent = None

    def execute(self, todos: list[dict]) -> str:
        if self._parent_agent is None or self._parent_agent.task_state is None:
            return "Error: todo tool not initialized (no active task)"

        normalized = normalize_todos(todos)
        self._parent_agent.task_state.set_todos(normalized)
        self._parent_agent.persist_task()
        self._parent_agent.hooks.emit(
            "todo_updated",
            {
                "task_id": self._parent_agent.task_state.task_id,
                "todos": normalized,
            },
        )
        return "Updated todo list:\n" + render_todos(normalized)
