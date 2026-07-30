"""Todo writing tool for explicit planning."""

from .base import ConcurrencySpec, Tool
from ..context import normalize_todos, render_todos


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

    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        return ConcurrencySpec.exclusive(
            "todo updates mutate task and session state",
            main_thread=True,
        )

    def execute(self, todos: list[dict]) -> str:
        if self._parent_agent is None or self._parent_agent.task_state is None:
            return "Error: todo tool not initialized (no active task)"

        normalized = normalize_todos(todos)
        blocked = self._reject_invalid_completion(normalized)
        if blocked:
            return blocked
        self._parent_agent.task_state.set_todos(normalized)
        self._parent_agent.persist_session()
        self._parent_agent.hooks.emit(
            "todo_updated",
            {
                "session_id": self._parent_agent.session_state.session_id,
                "task_id": self._parent_agent.task_state.task_id,
                "todos": normalized,
            },
        )
        return "Updated todo list:\n" + render_todos(normalized)

    def _reject_invalid_completion(self, todos: list[dict]) -> str:
        task_state = self._parent_agent.task_state
        last_result = task_state.last_tool_result or ""
        if not (last_result.startswith("Blocked by policy") or last_result.startswith("Error:")):
            return ""

        previous_status = {item.get("content", ""): item.get("status", "pending") for item in task_state.todos}
        for item in todos:
            content = item.get("content", "")
            if previous_status.get(content) == "completed":
                continue
            if content in previous_status and item.get("status", "pending") == "completed":
                return (
                    f"Error: cannot mark todo '{content}' completed because the latest tool result "
                    f"from {task_state.last_tool_name or 'tool'} was blocked or failed"
                )
        return ""
