"""Evidence-backed project memory and authoritative rule loading."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from ..message_content import content_text


@dataclass
class MemoryFact:
    """A durable fact whose validity is bound to one project file revision."""

    fact: str
    source: str
    source_hash: str
    confidence: str
    scope: str
    invalidated: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryFact":
        return cls(
            fact=str(data.get("fact") or "").strip(),
            source=str(data.get("source") or "").strip(),
            source_hash=str(data.get("source_hash") or "").strip(),
            confidence=str(data.get("confidence") or "stale").strip(),
            scope=str(data.get("scope") or "project").strip(),
            invalidated=bool(data.get("invalidated", False)),
        )


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
        self._last_project_memory_source_key = ""
        self._pending_project_memory_key = ""
        self._lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="autocode-memory",
        )
        self._future: concurrent.futures.Future | None = None

    def build_rules_block(self) -> str:
        """Load authoritative user/repository instructions separately from fallible facts."""
        parts = []
        project_rules = self._read_if_exists(self.workspace_root / "AGENTS.md")
        if project_rules:
            parts.append("## Project Rules\n" + self._clip(project_rules, 2000))
        claude_rules = self._read_if_exists(self.workspace_root / "CLAUDE.md")
        if claude_rules:
            parts.append("## Project Notes\n" + self._clip(claude_rules, 1200))
        return "\n\n".join(parts)

    def build_project_memory_block(self, query: str = "", max_facts: int = 5) -> str:
        """Return only currently verified facts relevant to the new turn."""
        facts = self._load_facts(validate_sources=True)
        active = [
            fact
            for fact in facts
            if not fact.invalidated and fact.confidence == "verified"
        ]
        if not active:
            return ""

        query_terms = _terms(query)
        ranked = sorted(
            active,
            key=lambda fact: (
                -_relevance_score(fact, query_terms),
                fact.scope,
                fact.source,
                fact.fact,
            ),
        )
        selected = ranked[:max_facts]
        return "\n".join(
            f"- {fact.fact} "
            f"(source: {fact.source}; sha256: {fact.source_hash[:12]}; confidence: verified)"
            for fact in selected
        )

    def memory_file_path(self) -> Path:
        return self.workspace_root / ".autocode" / "memory" / "facts.json"

    def refresh_project_memory(self, messages: list[dict], llm, force: bool = False) -> bool:
        """Extract file-grounded facts; existing memory is never accepted as evidence."""
        recent_conversation = self._flatten_messages(messages)
        if not recent_conversation:
            return False
        source_key = hashlib.sha1(
            recent_conversation.encode("utf-8", errors="replace")
        ).hexdigest()
        inventory = self._project_file_inventory()
        inventory_key = self._project_file_inventory_key()
        key = self._memory_refresh_key(recent_conversation, inventory_key)
        with self._lock:
            if not force and key == self._last_project_memory_key:
                return False

        selected_paths = self._select_project_file_paths(
            llm,
            recent_conversation=recent_conversation,
            file_inventory=inventory,
            max_files=8,
        )
        evidence, evidence_hashes = self._project_file_evidence(selected_paths)
        if not evidence:
            self._mark_refresh_complete(key, source_key)
            return False

        try:
            resp = llm.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Extract durable coding-repository facts from the supplied project file evidence. "
                            "The recent conversation may tell you what is relevant, but it is not evidence. "
                            "Return a JSON array with 0-8 objects. Every object must contain exactly: "
                            "fact, source, scope. The source must be one exact file path shown in Project file "
                            "evidence, and the fact must be directly supported by that file. Keep only "
                            "non-obvious run commands, architecture invariants, integration constraints, "
                            "platform pitfalls, or stable project conventions. Do not store task status, "
                            "temporary observations, timestamps, workspace paths, generic best practices, "
                            "or conclusions supported only by prior memory or conversation."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Recent conversation (relevance only):\n{recent_conversation}\n\n"
                            f"Project file evidence (authoritative):\n{evidence}"
                        ),
                    },
                ]
            )
            extracted = self._parse_extracted_facts(resp.content, evidence_hashes)
        except Exception:
            return False

        existing = self._load_facts(validate_sources=True)
        selected_sources = set(evidence_hashes)
        retained = [fact for fact in existing if fact.source not in selected_sources]
        merged = _dedupe_facts([*retained, *extracted])[:32]
        changed = [asdict(fact) for fact in merged] != [asdict(fact) for fact in existing]
        if changed:
            self._write_facts(merged)
        self._mark_refresh_complete(key, source_key)
        return changed

    def schedule_project_memory_refresh(
        self,
        messages: list[dict],
        llm,
        force: bool = False,
    ) -> bool:
        recent_conversation = self._flatten_messages(messages)
        if not recent_conversation or not hasattr(llm, "clone"):
            return False
        key = hashlib.sha1(
            recent_conversation.encode("utf-8", errors="replace")
        ).hexdigest()
        with self._lock:
            if not force and (
                key == self._last_project_memory_source_key
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

    def _load_facts(self, *, validate_sources: bool) -> list[MemoryFact]:
        path = self.memory_file_path()
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        facts = [
            MemoryFact.from_dict(item)
            for item in payload
            if isinstance(item, dict)
        ]
        facts = [
            fact
            for fact in facts
            if fact.fact and fact.source and fact.source_hash
        ]
        if not validate_sources:
            return facts

        changed = False
        for fact in facts:
            current_hash = self._source_hash(fact.source)
            stale = current_hash is None or current_hash != fact.source_hash
            if stale and (not fact.invalidated or fact.confidence != "stale"):
                fact.invalidated = True
                fact.confidence = "stale"
                changed = True
        if changed:
            self._write_facts(facts)
        return facts

    def _write_facts(self, facts: list[MemoryFact]) -> None:
        path = self.memory_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps([asdict(fact) for fact in facts], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def _source_hash(self, relative_path: str) -> str | None:
        path = (self.workspace_root / relative_path).resolve()
        try:
            path.relative_to(self.workspace_root)
        except ValueError:
            return None
        if not path.is_file():
            return None
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None

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
        keep_recent: int = 12,
        max_chars: int = 6000,
    ) -> str:
        parts = []
        for message in messages[-keep_recent:]:
            role = message.get("role", "?")
            content = content_text(message.get("content", "")).strip()
            if content:
                parts.append(f"[{role}] {content[:500]}")
        return "\n".join(parts)[:max_chars]

    def _candidate_project_files(
        self,
        max_depth: int = 3,
        max_files: int = 80,
    ) -> list[Path]:
        candidates: list[tuple[int, str, Path]] = []
        for path in self.workspace_root.rglob("*"):
            if not path.is_file() or path.name == ".env":
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

    def _project_file_evidence(
        self,
        selected_paths: list[tuple[str, Path]],
        max_chars: int = 6000,
        per_file_chars: int = 1200,
    ) -> tuple[str, dict[str, str]]:
        blocks: list[str] = []
        hashes: dict[str, str] = {}
        total_chars = 0
        for relative, path in selected_paths:
            text = self._read_if_exists(path)
            source_hash = self._source_hash(relative)
            if not text or source_hash is None:
                continue
            block = (
                f"[source: {relative}; sha256: {source_hash}]\n"
                f"{self._clip(text, per_file_chars)}"
            )
            if blocks and total_chars + len(block) > max_chars:
                break
            blocks.append(block)
            hashes[relative] = source_hash
            total_chars += len(block)
        return "\n\n".join(blocks), hashes

    def _select_project_file_paths(
        self,
        llm,
        *,
        recent_conversation: str,
        file_inventory: str,
        max_files: int,
    ) -> list[tuple[str, Path]]:
        if not file_inventory:
            return []
        try:
            resp = llm.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Choose 0-8 project files whose current contents may contain durable facts "
                            "relevant to the recent conversation. Return one exact path per line from the "
                            "inventory, or NONE. Existing memory is deliberately unavailable and must not "
                            "influence this selection. Prefer run commands, configuration constraints, "
                            "architecture boundaries, integration points, and persistent platform pitfalls."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Recent conversation:\n{recent_conversation}\n\n"
                            f"Project file inventory:\n{file_inventory}"
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

    @staticmethod
    def _parse_extracted_facts(
        content: str,
        evidence_hashes: dict[str, str],
    ) -> list[MemoryFact]:
        payload = json.loads(content)
        if not isinstance(payload, list):
            raise ValueError("Memory extraction must return a JSON array.")
        facts: list[MemoryFact] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            fact = str(item.get("fact") or "").strip()
            source = str(item.get("source") or "").strip()
            scope = str(item.get("scope") or "project").strip()
            source_hash = evidence_hashes.get(source)
            if not fact or source_hash is None:
                continue
            facts.append(
                MemoryFact(
                    fact=fact[:300],
                    source=source,
                    source_hash=source_hash,
                    confidence="verified",
                    scope=scope[:80] or "project",
                    invalidated=False,
                )
            )
        return _dedupe_facts(facts)[:8]

    @staticmethod
    def _memory_refresh_key(source: str, inventory_key: str) -> str:
        return hashlib.sha1(
            f"{source}\n\n{inventory_key}".encode("utf-8", errors="replace")
        ).hexdigest()

    def _project_file_inventory_key(self) -> str:
        rows: list[str] = []
        for path in self._candidate_project_files():
            relative = path.relative_to(self.workspace_root).as_posix()
            try:
                stat = path.stat()
            except OSError:
                continue
            rows.append(f"{relative}|{stat.st_size}|{stat.st_mtime_ns}")
        return hashlib.sha1(
            "\n".join(rows).encode("utf-8", errors="replace")
        ).hexdigest()

    def _mark_refresh_complete(self, key: str, source_key: str) -> None:
        with self._lock:
            self._last_project_memory_key = key
            self._last_project_memory_source_key = source_key


def _terms(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[\w./-]{2,}", text, flags=re.UNICODE)
    }


def _relevance_score(fact: MemoryFact, query_terms: set[str]) -> int:
    if not query_terms:
        return 0
    fact_terms = _terms(f"{fact.scope} {fact.source} {fact.fact}")
    return len(query_terms & fact_terms)


def _dedupe_facts(facts: list[MemoryFact]) -> list[MemoryFact]:
    seen: set[tuple[str, str]] = set()
    result: list[MemoryFact] = []
    for fact in facts:
        key = (fact.source, " ".join(fact.fact.lower().split()))
        if key in seen:
            continue
        seen.add(key)
        result.append(fact)
    return result
