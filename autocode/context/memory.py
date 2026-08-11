"""Simple project memory and authoritative rule loading."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import threading
from pathlib import Path

from ..message_content import content_text


class MemoryManager:
    """Maintain one small, model-written PROJECT_MEMORY.md per workspace."""

    _SECTIONS = ("用户偏好", "项目经验", "已知问题")

    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self._last_project_memory_key = ""
        self._last_trajectory_key = ""
        self._pending_project_memory_key = ""
        self._lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="autocode-memory",
        )
        self._future: concurrent.futures.Future | None = None

    def build_rules_block(self) -> str:
        """Load authoritative repository instructions separately from memory."""
        parts = []
        project_rules = self._read_if_exists(self.workspace_root / "AGENTS.md")
        if project_rules:
            parts.append("## Project Rules\n" + self._clip(project_rules, 2000))
        claude_rules = self._read_if_exists(self.workspace_root / "CLAUDE.md")
        if claude_rules:
            parts.append("## Project Notes\n" + self._clip(claude_rules, 1200))
        return "\n\n".join(parts)

    def build_project_memory_block(self, query: str = "") -> str:
        """Load the small project memory file in full."""
        del query
        memory = self._read_if_exists(self.memory_file_path())
        if not memory:
            return ""
        return self._clip(memory, 4000)

    def memory_file_path(self) -> Path:
        return self.workspace_root / ".autocode" / "PROJECT_MEMORY.md"

    def refresh_project_memory(self, messages: list[dict], llm, force: bool = False) -> bool:
        """Incrementally rewrite PROJECT_MEMORY.md from the current task trajectory."""
        trajectory = self._flatten_messages(messages)
        if not trajectory:
            return False

        existing = self._read_if_exists(self.memory_file_path())
        trajectory_key = hashlib.sha1(
            trajectory.encode("utf-8", errors="replace")
        ).hexdigest()
        key = hashlib.sha1(
            f"{existing}\n\n{trajectory}".encode("utf-8", errors="replace")
        ).hexdigest()
        with self._lock:
            if not force and key == self._last_project_memory_key:
                return False

        try:
            response = llm.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You maintain one small PROJECT_MEMORY.md for a coding project. "
                            "Update the existing memory using only the current task trajectory below. "
                            "Keep only: (1) stable preferences explicitly stated or repeatedly corrected by the user; "
                            "(2) project knowledge confirmed by tool output, code inspection, or successful tests; "
                            "and (3) recurring problems with a verified solution. Merge duplicates. When new verified "
                            "information conflicts with an old item, replace the old item. When the conflict is not "
                            "verified, keep the old item. Do not store task status, timestamps, temporary paths, one-off "
                            "results, secrets, or generic advice. Return the complete Markdown file with exactly the title "
                            "'# Project Memory' and only these optional sections: '## 用户偏好', '## 项目经验', "
                            "'## 已知问题'. Put concise bullet items under each section, with at most 12 bullets total. "
                            "If nothing should change, return exactly NO_CHANGE."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Existing PROJECT_MEMORY.md:\n"
                            f"{existing or '(none)'}\n\n"
                            "Current task trajectory:\n"
                            f"{trajectory}"
                        ),
                    },
                ]
            )
        except Exception:
            return False

        updated = self._normalize_project_memory(response.content)
        if not updated or updated == existing.strip():
            self._mark_refresh_complete(key, trajectory_key)
            return False

        self._write_memory(updated)
        self._mark_refresh_complete(key, trajectory_key)
        return True

    def schedule_project_memory_refresh(
        self,
        messages: list[dict],
        llm,
        force: bool = False,
    ) -> bool:
        trajectory = self._flatten_messages(messages)
        if not trajectory or not hasattr(llm, "clone"):
            return False
        key = hashlib.sha1(trajectory.encode("utf-8", errors="replace")).hexdigest()
        with self._lock:
            if not force and (
                key == self._last_trajectory_key
                or key == self._pending_project_memory_key
            ):
                return False
            self._pending_project_memory_key = key

        message_snapshot = [dict(message) for message in messages]
        llm_copy = llm.clone()
        future = self._executor.submit(
            self.refresh_project_memory,
            message_snapshot,
            llm_copy,
            force,
        )
        self._future = future

        def _clear_pending(_future):
            with self._lock:
                if self._pending_project_memory_key == key:
                    self._pending_project_memory_key = ""

        future.add_done_callback(_clear_pending)
        return True

    def wait_for_pending_refresh(self, timeout: float | None = None):
        future = self._future
        if future is not None:
            future.result(timeout=timeout)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _write_memory(self, content: str) -> None:
        path = self.memory_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
        temporary.replace(path)

    def _mark_refresh_complete(self, key: str, trajectory_key: str) -> None:
        with self._lock:
            self._last_project_memory_key = key
            self._last_trajectory_key = trajectory_key

    @classmethod
    def _normalize_project_memory(cls, text: str) -> str:
        if text.strip() == "NO_CHANGE":
            return ""

        sections: dict[str, list[str]] = {name: [] for name in cls._SECTIONS}
        current_section = ""
        bullet_count = 0
        for raw in text.replace("```markdown", "").replace("```", "").splitlines():
            line = raw.strip()
            if line.startswith("## "):
                name = line[3:].strip()
                current_section = name if name in sections else ""
                continue
            if not current_section or not line.startswith(("- ", "* ")):
                continue
            item = line[2:].strip()
            if not item or item in sections[current_section]:
                continue
            sections[current_section].append(item[:240])
            bullet_count += 1
            if bullet_count >= 12:
                break

        if bullet_count == 0:
            return ""

        output = ["# Project Memory"]
        for name in cls._SECTIONS:
            if not sections[name]:
                continue
            output.extend(["", f"## {name}", ""])
            output.extend(f"- {item}" for item in sections[name])
        return "\n".join(output).strip()

    @staticmethod
    def _read_if_exists(path: Path) -> str:
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8", errors="replace").strip()
        return ""

    @staticmethod
    def _clip(text: str, max_chars: int) -> str:
        return text[:max_chars] + ("..." if len(text) > max_chars else "")

    @staticmethod
    def _flatten_messages(
        messages: list[dict],
        keep_recent: int = 20,
        max_chars: int = 12_000,
    ) -> str:
        parts: list[str] = []
        for message in messages[-keep_recent:]:
            role = str(message.get("role") or "?")
            details: list[str] = []
            content = content_text(message.get("content", "")).strip()
            if content:
                details.append(content[:1000])

            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                name = str(function.get("name") or "tool")
                arguments = function.get("arguments") or ""
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                details.append(f"tool call: {name}({arguments[:500]})")

            if role == "tool":
                name = str(message.get("tool_name") or "tool")
                arguments = message.get("tool_arguments") or {}
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                details.insert(0, f"tool result: {name}({arguments[:500]})")

            if details:
                parts.append(f"[{role}] " + "\n".join(details))
        return "\n".join(parts)[:max_chars]
