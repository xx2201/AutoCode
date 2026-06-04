"""Runtime state for task execution."""

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
    requested_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "reason": self.reason,
            "requires_manual": self.requires_manual,
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
        )
