"""Task-level trace summary derived from runtime events."""

from __future__ import annotations

import json
import threading
import time

from .checkpoint import task_dir


class TraceRecorder:
    """Aggregate runtime events into a compact per-task trace."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stats: dict[str, dict] = {}

    def handle(self, event: str, payload: dict):
        task_id = payload.get("task_id")
        if not task_id:
            return

        with self._lock:
            stats = self._stats.get(task_id) or self._load_or_init(task_id)
            self._apply_event(stats, event, payload)
            stats["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            stats["duration_seconds"] = max(0.0, time.time() - stats["started_at_ts"])
            self._stats[task_id] = stats
            self._save(task_id, stats)

    def _load_or_init(self, task_id: str) -> dict:
        existing = load_trace(task_id)
        if existing:
            existing.setdefault("started_at_ts", time.time())
            existing.setdefault("modified_files", [])
            existing.setdefault("tools", {})
            return existing
        now = time.time()
        return {
            "task_id": task_id,
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
            "errors": 0,
            "modified_files": [],
            "tools": {},
        }

    @staticmethod
    def _apply_event(stats: dict, event: str, payload: dict):
        if event == "task_status":
            stats["status"] = payload.get("status", stats["status"])
        elif event == "user_message":
            stats["status"] = "running"
        elif event == "after_llm":
            stats["llm_calls"] += 1
            stats["steps"] = max(stats["steps"], int(payload.get("step_index", 0)))
            stats["prompt_tokens"] += int(payload.get("prompt_tokens", 0))
            stats["completion_tokens"] += int(payload.get("completion_tokens", 0))
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
        elif event == "task_error":
            stats["status"] = "failed"
            stats["errors"] += 1

    def _save(self, task_id: str, stats: dict):
        directory = task_dir(task_id)
        directory.mkdir(parents=True, exist_ok=True)
        data = dict(stats)
        data.pop("started_at_ts", None)
        (directory / "trace.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_trace(task_id: str) -> dict | None:
    path = task_dir(task_id) / "trace.json"
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
    return (
        f"Task: {trace.get('task_id', '?')}\n"
        f"Status: {trace.get('status', '?')}\n"
        f"Steps: {trace.get('steps', 0)}\n"
        f"LLM calls: {trace.get('llm_calls', 0)}\n"
        f"Tool calls: {trace.get('tool_calls', 0)}\n"
        f"Approval requests: {trace.get('approval_requests', 0)}\n"
        f"Blocked tool calls: {trace.get('blocked_tool_calls', 0)}\n"
        f"Prompt tokens: {trace.get('prompt_tokens', 0)}\n"
        f"Completion tokens: {trace.get('completion_tokens', 0)}\n"
        f"Errors: {trace.get('errors', 0)}\n"
        f"Modified files: {modified}\n"
        f"Tools: {tool_line}\n"
        f"Duration: {trace.get('duration_seconds', 0.0):.2f}s"
    )
