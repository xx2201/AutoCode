"""Explicit project-memory operations."""

from .base import ConcurrencySpec, Tool, ToolResult


class MemoryTool(Tool):
    name = "memory"
    description = (
        "Add, update, or remove one persistent project-memory item. "
        "Call this only when the user explicitly asks to remember, update a remembered item, "
        "or forget something. Never infer implicit preferences and never store secrets, "
        "credentials, temporary task state, or one-off results. For update and forget, content "
        "must exactly match the existing memory item."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["remember", "update", "forget"],
                "description": "The explicit memory operation to perform.",
            },
            "section": {
                "type": "string",
                "enum": ["user_preference", "project_knowledge", "known_issue"],
                "description": "The project-memory section containing the item.",
            },
            "content": {
                "type": "string",
                "description": (
                    "For remember, the new item. For update or forget, the exact existing item."
                ),
            },
            "replacement": {
                "type": "string",
                "description": "The replacement item. Required only when action is update.",
            },
        },
        "required": ["action", "section", "content"],
        "additionalProperties": False,
    }

    _memory_manager = None

    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        return ConcurrencySpec.exclusive(
            "project memory updates mutate persistent workspace state",
            main_thread=True,
        )

    def execute(
        self,
        action: str,
        section: str,
        content: str,
        replacement: str = "",
    ) -> str | ToolResult:
        if self._memory_manager is None:
            return ToolResult(
                text="Error: memory tool not initialized",
                is_error=True,
            )
        try:
            return self._memory_manager.apply_project_memory(
                action=action,
                section=section,
                content=content,
                replacement=replacement,
            )
        except ValueError as exc:
            return ToolResult(text=f"Error: {exc}", is_error=True)
