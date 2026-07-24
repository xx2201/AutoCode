"""Runtime state models for sessions and current tasks."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class PolicyDecision:
    action: str
    reason: str = ""
    requires_manual: bool = False

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "requires_manual": self.requires_manual,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PolicyDecision":
        return cls(
            action=data.get("action", "deny"),
            reason=data.get("reason", ""),
            requires_manual=bool(data.get("requires_manual", False)),
        )


@dataclass
class PendingApproval:
    tool_call_id: str
    tool_name: str
    arguments: dict
    reason: str
    requires_manual: bool = False
    remaining_tool_calls: list[dict] = field(default_factory=list)
    requested_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "reason": self.reason,
            "requires_manual": self.requires_manual,
            "remaining_tool_calls": self.remaining_tool_calls,
            "requested_at": self.requested_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PendingApproval":
        return cls(
            tool_call_id=data.get("tool_call_id", ""),
            tool_name=data.get("tool_name", ""),
            arguments=data.get("arguments", {}),
            reason=data.get("reason", ""),
            requires_manual=bool(data.get("requires_manual", False)),
            remaining_tool_calls=list(data.get("remaining_tool_calls", [])),
            requested_at=data.get("requested_at", _now()),
        )


@dataclass
class TaskState:
    task_id: str
    title: str = ""
    status: str = "idle"
    step_index: int = 0
    started_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    last_error: str = ""
    todos: list[dict] = field(default_factory=list)
    recent_failures: list[str] = field(default_factory=list)
    pending_approval: PendingApproval | None = None
    auto_approve_for_task: bool = False
    last_tool_name: str = ""
    last_tool_result: str = ""

    def touch(self, status: str | None = None):
        if status:
            self.status = status
        self.updated_at = _now()

    def next_step(self):
        self.step_index += 1
        self.touch("running")

    def mark_waiting(self, pending: PendingApproval):
        self.pending_approval = pending
        self.touch("waiting_approval")

    def clear_pending(self):
        self.pending_approval = None
        if self.status == "waiting_approval":
            self.touch("running")

    def mark_failed(self, error: str):
        self.last_error = error
        self.note_failure(error)
        self.auto_approve_for_task = False
        self.touch("failed")

    def mark_completed(self):
        self.clear_pending()
        self.auto_approve_for_task = False
        self.touch("completed")

    def set_todos(self, todos: list[dict]):
        self.todos = todos
        self.touch()

    def note_failure(self, failure: str):
        if failure:
            self.recent_failures.append(failure)
            self.recent_failures = self.recent_failures[-5:]
            self.last_error = failure
        self.touch()

    def clear_failures(self):
        self.recent_failures.clear()
        self.last_error = ""
        self.touch()

    def set_auto_approve(self, enabled: bool):
        self.auto_approve_for_task = bool(enabled)
        self.touch()

    def note_tool_result(self, tool_name: str, result: str):
        self.last_tool_name = tool_name
        self.last_tool_result = result
        self.touch()

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "status": self.status,
            "step_index": self.step_index,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "last_error": self.last_error,
            "todos": self.todos,
            "recent_failures": self.recent_failures,
            "pending_approval": self.pending_approval.to_dict() if self.pending_approval else None,
            "auto_approve_for_task": self.auto_approve_for_task,
            "last_tool_name": self.last_tool_name,
            "last_tool_result": self.last_tool_result,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskState":
        pending = data.get("pending_approval")
        return cls(
            task_id=data.get("task_id", ""),
            title=data.get("title", ""),
            status=data.get("status", "idle"),
            step_index=int(data.get("step_index", 0)),
            started_at=data.get("started_at", _now()),
            updated_at=data.get("updated_at", _now()),
            last_error=data.get("last_error", ""),
            todos=list(data.get("todos", [])),
            recent_failures=list(data.get("recent_failures", [])),
            pending_approval=PendingApproval.from_dict(pending) if pending else None,
            auto_approve_for_task=bool(data.get("auto_approve_for_task", False)),
            last_tool_name=data.get("last_tool_name", ""),
            last_tool_result=data.get("last_tool_result", ""),
        )


@dataclass
class SessionState:
    session_id: str
    title: str = ""
    started_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    current_task: TaskState | None = None

    def touch(self):
        self.updated_at = _now()

    def set_current_task(self, task: TaskState):
        self.current_task = task
        self.touch()

    def clear_current_task(self):
        self.current_task = None
        self.touch()

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "current_task": self.current_task.to_dict() if self.current_task else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionState":
        task = data.get("current_task")
        return cls(
            session_id=data.get("session_id", ""),
            title=data.get("title", ""),
            started_at=data.get("started_at", _now()),
            updated_at=data.get("updated_at", _now()),
            current_task=TaskState.from_dict(task) if task else None,
        )
