"""Project memory and rule loading."""

from __future__ import annotations

import concurrent.futures
import hashlib
import threading
from pathlib import Path


class MemoryManager:
    _EVIDENCE_IGNORED_DIRS = {
        ".autocode",
        ".git",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
    _EVIDENCE_IGNORED_SUFFIXES = {
        ".db",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".exe",
        ".dll",
        ".so",
        ".woff",
        ".woff2",
        ".ttf",
        ".log",
    }
    _TEXT_FILE_SUFFIXES = {
        "",
        ".md",
        ".txt",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".ini",
        ".cfg",
        ".env",
        ".sh",
        ".ps1",
        ".bat",
        ".sql",
        ".html",
        ".css",
        ".vue",
    }

    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self._last_project_memory_key = ""
        self._pending_project_memory_key = ""
        self._lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="autocode-memory")
        self._future: concurrent.futures.Future | None = None

    def build_rules_block(self) -> str:
        parts = []
        project_rules = self._read_if_exists(self.workspace_root / "AGENTS.md")
        if project_rules:
            parts.append("## Project Rules\n" + self._clip(project_rules, 2000))
        claude_rules = self._read_if_exists(self.workspace_root / "CLAUDE.md")
        if claude_rules:
            parts.append("## Project Notes\n" + self._clip(claude_rules, 1200))
        return "\n\n".join(parts)

    def build_project_memory_block(self) -> str:
        project_memory = self._read_if_exists(self.memory_file_path())
        project_memory = self._strip_project_memory_heading(project_memory)
        if not project_memory:
            return ""
        return self._clip(project_memory, 2000)

    def memory_file_path(self) -> Path:
        return self.workspace_root / ".autocode" / "PROJECT_MEMORY.md"

    def refresh_project_memory(self, messages: list[dict], llm, force: bool = False) -> bool:
        source = self._flatten_messages(messages)
        if not source:
            return False
        existing_memory = self._read_if_exists(self.memory_file_path()) or "(none)"
        inventory = self._project_file_inventory()
        inventory_key = self._project_file_inventory_key()
        key = self._memory_refresh_key(source, inventory_key)
        with self._lock:
            if not force and key == self._last_project_memory_key:
                return False

        try:
            project_evidence = self._select_project_file_evidence(
                llm,
                existing_memory=existing_memory,
                recent_conversation=source,
                file_inventory=inventory,
            )
            resp = llm.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are maintaining PROJECT_MEMORY.md for a coding repository. This file is loaded at the start "
                            "of future sessions, so every line must earn its cost. Rewrite the FULL file as 0-8 bullet lines. "
                            "Keep only durable facts that would save a future agent from a likely mistake, failed run, or "
                            "repeated rediscovery. Every bullet must be explicitly supported by one of three evidence sources "
                            "provided below: Existing PROJECT_MEMORY.md, Recent conversation, or Project file evidence. Never "
                            "infer from generic library best practices, what the codebase probably should do, or what is merely "
                            "common in similar repos. If the evidence does not directly show a fact, leave it out. Project file "
                            "evidence is authoritative over general knowledge. Prefer information that is high-impact and hard "
                            "to infer quickly from AGENTS.md, README, or a quick scan of the top-level tree. Prefer these "
                            "categories: non-obvious run/test/build commands or required services and environments; stable "
                            "architecture boundaries, ownership rules, and invariants; recurring debugging discoveries and "
                            "platform-specific pitfalls; tool or provider constraints that change how the agent must operate; "
                            "and enduring user or team preferences that should shape most future changes. Strong examples: "
                            "'Use `conda activate foo` before pytest; system Python misses required deps', 'API tests require "
                            "local Redis and fail without it', 'Session history is keyed by session_id; task_id stores only the "
                            "current task state', 'Long-running workers must run under the process manager and be explicitly "
                            "cleaned up', 'When child Python stdout is redirected on Windows, force UTF-8 or logs become "
                            "garbled', 'After approval, resume the remaining tool calls from the same batch instead of "
                            "dropping them'. "
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
                            f"{existing_memory}\n\n"
                            "Recent conversation:\n"
                            f"{source}\n\n"
                            "Project file evidence:\n"
                            f"{project_evidence or '(none)'}"
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
        key = self._memory_refresh_key(source, self._project_file_inventory_key())
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

    def _candidate_project_files(self, max_depth: int = 3, max_files: int = 80) -> list[Path]:
        candidates: list[tuple[int, str, Path]] = []
        for path in self.workspace_root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(self.workspace_root)
            if len(relative.parts) > max_depth:
                continue
            if any(part in self._EVIDENCE_IGNORED_DIRS for part in relative.parts[:-1]):
                continue
            if path.suffix.lower() in self._EVIDENCE_IGNORED_SUFFIXES:
                continue
            if path.suffix.lower() not in self._TEXT_FILE_SUFFIXES:
                continue
            try:
                if path.stat().st_size > 64 * 1024:
                    continue
            except OSError:
                continue
            candidates.append((len(relative.parts), relative.as_posix(), path))
        candidates.sort(key=lambda item: (item[0], item[1]))
        return [path for _, _, path in candidates[:max_files]]

    def _project_file_inventory(self, max_chars: int = 4000) -> str:
        lines: list[str] = []
        total = 0
        for path in self._candidate_project_files():
            relative = path.relative_to(self.workspace_root).as_posix()
            try:
                size = path.stat().st_size
            except OSError:
                continue
            line = f"- {relative} ({size} bytes)"
            if lines and total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line)
        return "\n".join(lines)

    def _select_project_file_evidence(
        self,
        llm,
        *,
        existing_memory: str,
        recent_conversation: str,
        file_inventory: str,
        max_files: int = 8,
        max_chars: int = 3500,
        per_file_chars: int = 700,
    ) -> str:
        if not file_inventory:
            return ""
        selected_paths = self._select_project_file_paths(
            llm,
            existing_memory=existing_memory,
            recent_conversation=recent_conversation,
            file_inventory=file_inventory,
            max_files=max_files,
        )
        blocks: list[str] = []
        total_chars = 0
        for relative, path in selected_paths:
            text = self._read_if_exists(path)
            if not text:
                continue
            block = f"[{relative}]\n{self._clip(text, per_file_chars)}"
            if blocks and total_chars + len(block) > max_chars:
                break
            blocks.append(block)
            total_chars += len(block)
            if len(blocks) >= max_files or total_chars >= max_chars:
                break
        return "\n\n".join(blocks)

    @staticmethod
    def _memory_refresh_key(source: str, inventory_key: str) -> str:
        payload = source + "\n\n" + inventory_key
        return hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()

    def _project_file_inventory_key(self) -> str:
        rows: list[str] = []
        for path in self._candidate_project_files():
            relative = path.relative_to(self.workspace_root).as_posix()
            try:
                stat = path.stat()
            except OSError:
                continue
            rows.append(f"{relative}|{stat.st_size}|{stat.st_mtime_ns}")
        return hashlib.sha1("\n".join(rows).encode("utf-8", errors="replace")).hexdigest()

    def _select_project_file_paths(
        self,
        llm,
        *,
        existing_memory: str,
        recent_conversation: str,
        file_inventory: str,
        max_files: int,
    ) -> list[tuple[str, Path]]:
        try:
            resp = llm.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are selecting project files for PROJECT_MEMORY.md grounding. Choose 0-8 file paths from the "
                            "provided inventory that are most likely to contain durable, high-signal facts worth remembering "
                            "across future coding sessions. Prefer files that reveal real run commands, config constraints, "
                            "architecture boundaries, integration points, or persistent pitfalls. Do not invent paths. Do not "
                            "choose files only because they are common in other repos. Return one exact path per line, copied "
                            "verbatim from the inventory. Return NONE if no file is worth reading."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Existing PROJECT_MEMORY.md:\n"
                            f"{existing_memory}\n\n"
                            "Recent conversation:\n"
                            f"{recent_conversation}\n\n"
                            "Project file inventory:\n"
                            f"{file_inventory}"
                        ),
                    },
                ]
            )
        except Exception:
            return []

        inventory_map = {
            path.relative_to(self.workspace_root).as_posix(): path
            for path in self._candidate_project_files()
        }
        selected: list[tuple[str, Path]] = []
        for raw in resp.content.splitlines():
            line = raw.strip().lstrip("-* ").strip()
            if not line or line.upper() == "NONE":
                continue
            path = inventory_map.get(line)
            if path and all(existing[0] != line for existing in selected):
                selected.append((line, path))
            if len(selected) >= max_files:
                break
        return selected

