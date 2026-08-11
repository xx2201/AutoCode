"""Session-level trace summary derived from runtime events."""

from __future__ import annotations

import json
import threading
import time

from .checkpoint import session_dir


class TraceRecorder:
    """Aggregate runtime events into a compact per-session trace."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stats: dict[str, dict] = {}

    def handle(self, event: str, payload: dict):
        session_id = payload.get("session_id")
        if not session_id:
            return

        with self._lock:
            stats = self._stats.get(session_id) or self._load_or_init(session_id)
            self._apply_event(stats, event, payload)
            stats["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            stats["duration_seconds"] = max(0.0, time.time() - stats["started_at_ts"])
            self._stats[session_id] = stats
            self._save(session_id, stats)

    def _load_or_init(self, session_id: str) -> dict:
        existing = load_trace(session_id)
        if existing:
            existing.setdefault("started_at_ts", time.time())
            existing.setdefault("modified_files", [])
            existing.setdefault("tools", {})
            existing.setdefault("cache_read_tokens", 0)
            existing.setdefault("cache_miss_tokens", 0)
            existing.setdefault("compactions", 0)
            existing.setdefault("cache_segments", 1)
            existing.setdefault("context_saved_tokens", 0)
            return existing
        now = time.time()
        return {
            "session_id": session_id,
            "current_turn_id": "",
            "status": "running",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "started_at_ts": now,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_seconds": 0.0,
            "steps": 0,
            "llm_calls": 0,
            "tool_calls": 0,
            "approval_requests": 0,
            "blocked_tool_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_read_tokens": 0,
            "cache_miss_tokens": 0,
            "compactions": 0,
            "cache_segments": 1,
            "context_saved_tokens": 0,
            "errors": 0,
            "modified_files": [],
            "tools": {},
        }

    @staticmethod
    def _apply_event(stats: dict, event: str, payload: dict):
        if payload.get("turn_id"):
            stats["current_turn_id"] = payload["turn_id"]
        if event == "turn_status":
            stats["status"] = payload.get("status", stats["status"])
        elif event == "user_message":
            stats["status"] = "running"
        elif event == "after_llm":
            stats["llm_calls"] += 1
            stats["steps"] = max(stats["steps"], int(payload.get("step_index", 0)))
            stats["prompt_tokens"] += int(payload.get("prompt_tokens", 0))
            stats["completion_tokens"] += int(payload.get("completion_tokens", 0))
            stats["cache_read_tokens"] += int(payload.get("cache_read_tokens", 0))
            stats["cache_miss_tokens"] += int(payload.get("cache_miss_tokens", 0))
        elif event == "context_compaction":
            stats["compactions"] += 1
            stats["cache_segments"] = 1 + int(stats["compactions"])
            stats["context_saved_tokens"] += int(payload.get("saved_tokens", 0))
        elif event == "policy_decision":
            action = payload.get("decision", {}).get("action")
            if action == "confirm":
                stats["approval_requests"] += 1
            elif action == "deny":
                stats["blocked_tool_calls"] += 1
        elif event == "before_tool":
            tool_name = payload.get("tool_name", "?")
            stats["tool_calls"] += 1
            stats["tools"][tool_name] = stats["tools"].get(tool_name, 0) + 1
            path = payload.get("arguments", {}).get("file_path")
            if tool_name in {"edit_file", "write_file"} and path and path not in stats["modified_files"]:
                stats["modified_files"].append(path)
        elif event == "after_tool":
            result = payload.get("result", "")
            if isinstance(result, str) and result.startswith("Error"):
                stats["errors"] += 1
        elif event == "approval_resolved":
            if not payload.get("approved", False):
                stats["blocked_tool_calls"] += 1
        elif event == "turn_error":
            stats["status"] = "failed"
            stats["errors"] += 1

    def _save(self, session_id: str, stats: dict):
        directory = session_dir(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        data = dict(stats)
        data.pop("started_at_ts", None)
        (directory / "trace.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_trace(session_id: str) -> dict | None:
    path = session_dir(session_id) / "trace.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if "started_at_ts" not in data:
        data["started_at_ts"] = time.time() - float(data.get("duration_seconds", 0.0))
    return data


def format_trace(trace: dict) -> str:
    modified = ", ".join(trace.get("modified_files", [])[:5]) or "-"
    tools = trace.get("tools", {})
    tool_line = ", ".join(f"{name}={count}" for name, count in sorted(tools.items())) or "-"
    cache_read = int(trace.get("cache_read_tokens", 0))
    cache_miss = int(trace.get("cache_miss_tokens", 0))
    cache_total = cache_read + cache_miss
    cache_hit_rate = f"{(cache_read / cache_total * 100):.1f}%" if cache_total else "n/a"
    return (
        f"Session: {trace.get('session_id', '?')}\n"
        f"Current Turn: {trace.get('current_turn_id', '?')}\n"
        f"Status: {trace.get('status', '?')}\n"
        f"Steps: {trace.get('steps', 0)}\n"
        f"LLM calls: {trace.get('llm_calls', 0)}\n"
        f"Tool calls: {trace.get('tool_calls', 0)}\n"
        f"Approval requests: {trace.get('approval_requests', 0)}\n"
        f"Blocked tool calls: {trace.get('blocked_tool_calls', 0)}\n"
        f"Prompt tokens: {trace.get('prompt_tokens', 0)}\n"
        f"Completion tokens: {trace.get('completion_tokens', 0)}\n"
        f"Prompt cache read tokens: {cache_read}\n"
        f"Prompt cache miss tokens: {cache_miss}\n"
        f"Prompt cache hit rate: {cache_hit_rate}\n"
        f"Compactions: {trace.get('compactions', 0)}\n"
        f"Cache segments: {trace.get('cache_segments', 1)}\n"
        f"Context saved tokens: {trace.get('context_saved_tokens', 0)}\n"
        f"Errors: {trace.get('errors', 0)}\n"
        f"Modified files: {modified}\n"
        f"Tools: {tool_line}\n"
        f"Duration: {trace.get('duration_seconds', 0.0):.2f}s"
    )
