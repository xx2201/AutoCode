"""Project memory and rule loading."""

from __future__ import annotations

from pathlib import Path

from .tasks import TaskStore


class MemoryManager:
    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).expanduser().resolve()

    def build_memory_block(self, current_task_id: str | None = None) -> str:
        parts = []

        project_rules = self._read_if_exists(self.workspace_root / "AGENTS.md")
        if project_rules:
            parts.append("## Project Rules\n" + self._clip(project_rules, 2000))

        claude_rules = self._read_if_exists(self.workspace_root / "CLAUDE.md")
        if claude_rules:
            parts.append("## Project Notes\n" + self._clip(claude_rules, 1200))

        local_memory = self._read_if_exists(self.workspace_root / ".corecoder" / "memory.md")
        if local_memory:
            parts.append("## Local Memory\n" + self._clip(local_memory, 1200))

        recent = [line for line in TaskStore.recent_task_summaries() if current_task_id is None or current_task_id not in line]
        if recent:
            parts.append("## Recent Tasks\n" + "\n".join(recent[:3]))

        return "\n\n".join(parts)

    @staticmethod
    def _read_if_exists(path: Path) -> str:
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8", errors="replace").strip()
        return ""

    @staticmethod
    def _clip(text: str, max_chars: int) -> str:
        return text[:max_chars] + ("..." if len(text) > max_chars else "")
