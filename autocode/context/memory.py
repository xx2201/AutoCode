"""Project memory and rule loading."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..state.store import TaskStore


class MemoryManager:
    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self._last_project_memory_key = ""

    def build_memory_block(self, current_task_id: str | None = None) -> str:
        parts = []

        project_rules = self._read_if_exists(self.workspace_root / "AGENTS.md")
        if project_rules:
            parts.append("## Project Rules\n" + self._clip(project_rules, 2000))

        claude_rules = self._read_if_exists(self.workspace_root / "CLAUDE.md")
        if claude_rules:
            parts.append("## Project Notes\n" + self._clip(claude_rules, 1200))

        project_memory = self._read_if_exists(self.workspace_root / ".autocode" / "PROJECT_MEMORY.md")
        project_memory = self._strip_project_memory_heading(project_memory)
        if project_memory:
            parts.append("## Project Memory\n" + self._clip(project_memory, 1200))

        recent = [line for line in TaskStore.recent_task_summaries() if current_task_id is None or current_task_id not in line]
        if recent:
            parts.append("## Recent Tasks\n" + "\n".join(recent[:3]))

        return "\n\n".join(parts)

    def refresh_project_memory(self, messages: list[dict], llm, force: bool = False) -> bool:
        source = self._flatten_messages(messages)
        if not source:
            return False

        key = hashlib.sha1(source.encode("utf-8", errors="replace")).hexdigest()
        if not force and key == self._last_project_memory_key:
            return False

        try:
            resp = llm.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are maintaining PROJECT_MEMORY.md for a coding workspace. "
                            "Rewrite the FULL project memory as 0-6 bullet lines. Keep only durable, high-value "
                            "facts that will still matter in future sessions and are NOT obvious from simply "
                            "rereading the current source tree. Prefer recurring pitfalls, validated but non-obvious "
                            "commands or environment constraints, stable architectural decisions, and mistakes that "
                            "are easy to repeat. Drop stale, redundant, low-value, or code-obvious facts. Merge "
                            "duplicates yourself. Output bullet lines only, each starting with `- ` and each under "
                            "120 characters."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Existing project memory:\n"
                            f"{self._read_if_exists(self.workspace_root / '.autocode' / 'PROJECT_MEMORY.md') or '(none)'}\n\n"
                            "Recent conversation:\n"
                            f"{source}"
                        ),
                    },
                ]
            )
        except Exception:
            return False

        lines = self._normalize_memory_lines(resp.content)
        if not lines:
            self._last_project_memory_key = key
            return False

        path = self.workspace_root / ".autocode" / "PROJECT_MEMORY.md"
        path.parent.mkdir(parents=True, exist_ok=True)

        body = "# Project Memory\n\n" + "\n".join(lines) + "\n"
        path.write_text(body, encoding="utf-8")
        self._last_project_memory_key = key
        return True

    @staticmethod
    def _read_if_exists(path: Path) -> str:
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8", errors="replace").strip()
        return ""

    @staticmethod
    def _clip(text: str, max_chars: int) -> str:
        return text[:max_chars] + ("..." if len(text) > max_chars else "")

    @staticmethod
    def _normalize_memory_lines(text: str) -> list[str]:
        lines: list[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("- "):
                normalized = line
            else:
                normalized = "- " + line.lstrip("-* ").strip()
            if normalized not in lines:
                lines.append(normalized)
        return lines[:6]

    @classmethod
    def _strip_project_memory_heading(cls, text: str) -> str:
        lines = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(raw.rstrip())
        return "\n".join(lines).strip()

    @staticmethod
    def _flatten_messages(messages: list[dict], keep_recent: int = 12, max_chars: int = 6000) -> str:
        parts = []
        for message in messages[-keep_recent:]:
            role = message.get("role", "?")
            content = (message.get("content", "") or "").strip()
            if content:
                parts.append(f"[{role}] {content[:500]}")
        flat = "\n".join(parts)
        return flat[:max_chars]

