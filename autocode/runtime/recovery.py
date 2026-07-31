"""Lightweight error recovery helpers."""

from __future__ import annotations

from ..state import TaskState
from ..tools.base import ToolResult


class RecoveryManager:
    def note_tool_result(
        self,
        task_state: TaskState,
        tool_name: str,
        result: str | ToolResult,
    ) -> str | ToolResult:
        text = result.text if isinstance(result, ToolResult) else result
        failed = result.is_error if isinstance(result, ToolResult) else self._is_failure(text)
        if failed:
            task_state.note_failure(f"{tool_name}: {text.splitlines()[0][:200]}")
            if isinstance(result, ToolResult):
                return result
            hint = self._hint_for_result(text)
            recovered = text + f"\n\n[recovery]\n{hint}"
            return recovered

        task_state.clear_failures()
        return result

    @staticmethod
    def _is_failure(result: str) -> bool:
        lowered = result.lower()
        return lowered.startswith("error") or lowered.startswith("blocked by policy") or lowered.startswith("approval required")

    @staticmethod
    def _hint_for_result(result: str) -> str:
        lowered = result.lower()
        if "not found" in lowered:
            return "The target was not found. Re-read the project structure or use glob/grep before retrying."
        if "appears" in lowered and "times" in lowered:
            return "The edit target is ambiguous. Read the file and include more unique surrounding context."
        if "blocked by policy" in lowered or "approval required" in lowered:
            return "The action crossed a safety boundary. Choose a safer tool call or ask for approval intentionally."
        return "Analyze the failure before retrying. Prefer reading files or narrowing the command instead of repeating it unchanged."
