"""Simple project memory and authoritative rule loading."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
import threading
from pathlib import Path

from ..message_content import content_text
from ..infra.sandbox_policy import SandboxDenied, SandboxPolicy


class MemoryManager:
    """Maintain one small PROJECT_MEMORY.md per workspace."""

    _SECTIONS = ("用户偏好", "项目经验", "已知问题")
    _SECTION_NAMES = {
        "user_preference": "用户偏好",
        "project_knowledge": "项目经验",
        "known_issue": "已知问题",
    }
    _MAX_ITEMS = 12
    _MAX_ITEM_CHARS = 240
    _SECRET_PATTERN = re.compile(
        r"(?i)(?:\b(?:api[_ -]?key|password|secret|access[_ -]?token)\b|密码|密钥)"
        r"\s*[:：=]\s*\S+|\bsk-[A-Za-z0-9_-]{12,}"
    )

    def __init__(self, workspace_root: str, policy: SandboxPolicy | None = None):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.policy = policy or SandboxPolicy(str(self.workspace_root))
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
            parts.append("## Project Rules\n" + project_rules)
        claude_rules = self._read_if_exists(self.workspace_root / "CLAUDE.md")
        if claude_rules:
            parts.append("## Project Notes\n" + claude_rules)
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

    def apply_project_memory(
        self,
        action: str,
        section: str,
        content: str,
        replacement: str = "",
    ) -> str:
        """Apply one explicit memory operation in the shared memory write queue."""
        future = self._executor.submit(
            self._apply_project_memory,
            action,
            section,
            content,
            replacement,
        )
        return future.result()

    def refresh_project_memory(self, messages: list[dict], llm, force: bool = False) -> bool:
        """Incrementally rewrite PROJECT_MEMORY.md from the current turn trajectory."""
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
                            "Update the existing memory using only the current turn trajectory below. "
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
                            "Current turn trajectory:\n"
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
        if not self.policy.resolve().can_write(self.memory_file_path()):
            return False
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
            try:
                future.result(timeout=timeout)
            except SandboxDenied:
                return

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _write_memory(self, content: str) -> None:
        path = self.memory_file_path()
        self.policy.resolve().require_write(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
        temporary.replace(path)

    def _apply_project_memory(
        self,
        action: str,
        section: str,
        content: str,
        replacement: str,
    ) -> str:
        if action not in {"remember", "update", "forget"}:
            raise ValueError("action must be remember, update, or forget")
        section_name = self._SECTION_NAMES.get(section)
        if section_name is None:
            raise ValueError(
                "section must be user_preference, project_knowledge, or known_issue"
            )

        item = self._validate_explicit_item(content, field_name="content")
        new_item = ""
        if action == "update":
            new_item = self._validate_explicit_item(
                replacement,
                field_name="replacement",
            )
        elif replacement.strip():
            raise ValueError("replacement is only valid when action is update")

        sections = self._parse_project_memory(
            self._read_if_exists(self.memory_file_path())
        )
        items = sections[section_name]

        if action == "remember":
            if item in items:
                return f"Memory unchanged: item already exists in {section_name}: {item}"
            if sum(len(values) for values in sections.values()) >= self._MAX_ITEMS:
                raise ValueError(
                    f"project memory already contains the maximum of {self._MAX_ITEMS} items"
                )
            items.append(item)
            result = f"Remembered in {section_name}: {item}"
        else:
            if item not in items:
                available = "\n".join(f"- {value}" for value in items) or "(none)"
                raise ValueError(
                    f"memory item was not found in {section_name}: {item}\n"
                    f"Current items:\n{available}"
                )
            index = items.index(item)
            if action == "forget":
                items.pop(index)
                result = f"Forgot from {section_name}: {item}"
            elif new_item == item:
                return f"Memory unchanged: replacement matches the existing item: {item}"
            elif new_item in items:
                items.pop(index)
                result = (
                    f"Updated {section_name}: removed duplicate source item '{item}'; "
                    f"'{new_item}' already exists"
                )
            else:
                items[index] = new_item
                result = f"Updated {section_name}: {item} -> {new_item}"

        rendered = self._render_project_memory(sections)
        if rendered:
            self._write_memory(rendered)
        else:
            path = self.memory_file_path()
            self.policy.resolve().require_write(path)
            path.unlink(missing_ok=True)
        return result

    @classmethod
    def _validate_explicit_item(cls, value: str, *, field_name: str) -> str:
        item = " ".join(str(value or "").split()).removeprefix("- ").strip()
        if not item:
            raise ValueError(f"{field_name} must not be empty")
        if len(item) > cls._MAX_ITEM_CHARS:
            raise ValueError(
                f"{field_name} must not exceed {cls._MAX_ITEM_CHARS} characters"
            )
        if cls._SECRET_PATTERN.search(item):
            raise ValueError("project memory must not store secrets or credentials")
        return item

    def _mark_refresh_complete(self, key: str, trajectory_key: str) -> None:
        with self._lock:
            self._last_project_memory_key = key
            self._last_trajectory_key = trajectory_key

    @classmethod
    def _normalize_project_memory(cls, text: str) -> str:
        if text.strip() == "NO_CHANGE":
            return ""
        return cls._render_project_memory(cls._parse_project_memory(text))

    @classmethod
    def _parse_project_memory(cls, text: str) -> dict[str, list[str]]:
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
            sections[current_section].append(item[:cls._MAX_ITEM_CHARS])
            bullet_count += 1
            if bullet_count >= cls._MAX_ITEMS:
                break
        return sections

    @classmethod
    def _render_project_memory(cls, sections: dict[str, list[str]]) -> str:
        if not any(sections.values()):
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
