"""Project memory and rule loading."""

from __future__ import annotations

import concurrent.futures
import hashlib
import threading
from pathlib import Path


class MemoryManager:
    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self._last_project_memory_key = ""
        self._pending_project_memory_key = ""
        self._lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="autocode-memory")
        self._future: concurrent.futures.Future | None = None

    def build_memory_block(self) -> str:
        parts = []

        project_rules = self._read_if_exists(self.workspace_root / "AGENTS.md")
        if project_rules:
            parts.append("## Project Rules\n" + self._clip(project_rules, 2000))

        claude_rules = self._read_if_exists(self.workspace_root / "CLAUDE.md")
        if claude_rules:
            parts.append("## Project Notes\n" + self._clip(claude_rules, 1200))

        project_memory = self._read_if_exists(self.memory_file_path())
        project_memory = self._strip_project_memory_heading(project_memory)
        if project_memory:
            parts.append("## Project Memory\n" + self._clip(project_memory, 2000))

        return "\n\n".join(parts)

    def memory_file_path(self) -> Path:
        return self.workspace_root / ".autocode" / "PROJECT_MEMORY.md"

    def refresh_project_memory(self, messages: list[dict], llm, force: bool = False) -> bool:
        source = self._flatten_messages(messages)
        if not source:
            return False

        key = hashlib.sha1(source.encode("utf-8", errors="replace")).hexdigest()
        with self._lock:
            if not force and key == self._last_project_memory_key:
                return False

        try:
            resp = llm.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are maintaining PROJECT_MEMORY.md for a coding repository. This file is loaded at the start "
                            "of future sessions, so every line must earn its cost. Rewrite the FULL file as 0-8 bullet lines. "
                            "Keep only durable facts that would save a future agent from a likely mistake, failed run, or "
                            "repeated rediscovery. Prefer information that is high-impact and hard to infer quickly from "
                            "AGENTS.md, README, or a quick scan of the top-level tree. Prefer these categories: non-obvious "
                            "run/test/build commands or required services and environments; stable architecture boundaries, "
                            "ownership rules, and invariants; recurring debugging discoveries and platform-specific pitfalls; "
                            "tool or provider constraints that change how the agent must operate; and enduring user or team "
                            "preferences that should shape most future changes. Strong examples: 'Use `conda activate foo` "
                            "before pytest; system Python misses required deps', 'API tests require local Redis and fail "
                            "without it', 'Session history is keyed by session_id; task_id stores only the current task "
                            "state', 'Long-running workers must run under the process manager and be explicitly cleaned up', "
                            "'When child Python stdout is redirected on Windows, force UTF-8 or logs become garbled', "
                            "'After approval, resume the remaining tool calls from the same batch instead of dropping them'. "
                            "Do NOT store: workspace paths, file listings, task/session/process ids, one-off plans, temporary "
                            "verification notes, timestamps, exact durations, or obvious facts like 'uses Python'. Prefer "
                            "surprises over summaries. If unsure, leave it out. Merge duplicates yourself. Output bullet "
                            "lines only, each starting with `- ` and each under 140 characters."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Existing PROJECT_MEMORY.md:\n"
                            f"{self._read_if_exists(self.memory_file_path()) or '(none)'}\n\n"
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
            with self._lock:
                self._last_project_memory_key = key
            return False

        path = self.memory_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        body = "# Project Memory\n\n" + "\n".join(lines) + "\n"
        path.write_text(body, encoding="utf-8")
        with self._lock:
            self._last_project_memory_key = key
        return True

    def schedule_project_memory_refresh(self, messages: list[dict], llm, force: bool = False) -> bool:
        source = self._flatten_messages(messages)
        if not source or not hasattr(llm, "clone"):
            return False

        key = hashlib.sha1(source.encode("utf-8", errors="replace")).hexdigest()
        with self._lock:
            if not force and (key == self._last_project_memory_key or key == self._pending_project_memory_key):
                return False
            self._pending_project_memory_key = key

        message_snapshot = [dict(message) for message in messages]
        llm_copy = llm.clone()
        future = self._executor.submit(self.refresh_project_memory, message_snapshot, llm_copy, force)
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
        return lines[:8]


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

