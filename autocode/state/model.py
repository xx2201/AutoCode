"""Runtime state models for sessions and current tasks."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class PolicyDecision:
    action: str
    reason: str = ""
    requires_manual: bool = False
    approval_scope: str = ""
    approval_label: str = ""

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "requires_manual": self.requires_manual,
            "approval_scope": self.approval_scope,
            "approval_label": self.approval_label,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PolicyDecision":
        return cls(
            action=data.get("action", "deny"),
            reason=data.get("reason", ""),
            requires_manual=bool(data.get("requires_manual", False)),
            approval_scope=data.get("approval_scope", ""),
            approval_label=data.get("approval_label", ""),
        )


@dataclass
class PendingApproval:
    approval_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict
    reason: str
    requires_manual: bool = False
    approval_scope: str = ""
    approval_label: str = ""
    decision: str = "pending"
    requested_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "approval_id": self.approval_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "reason": self.reason,
            "requires_manual": self.requires_manual,
            "approval_scope": self.approval_scope,
            "approval_label": self.approval_label,
            "decision": self.decision,
            "requested_at": self.requested_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PendingApproval":
        return cls(
            approval_id=data.get("approval_id", uuid.uuid4().hex),
            tool_call_id=data.get("tool_call_id", ""),
            tool_name=data.get("tool_name", ""),
            arguments=data.get("arguments", {}),
            reason=data.get("reason", ""),
            requires_manual=bool(data.get("requires_manual", False)),
            approval_scope=data.get("approval_scope", ""),
            approval_label=data.get("approval_label", ""),
            decision=data.get("decision", "pending"),
            requested_at=data.get("requested_at", _now()),
        )


@dataclass
class PendingToolBatch:
    batch_id: str
    turn_id: str
    tool_calls: list[dict]
    policy_decisions: list[dict | None]
    approvals: list[PendingApproval]
    state: str = "waiting"
    created_at: str = field(default_factory=_now)

    def unresolved(self) -> list[PendingApproval]:
        return [item for item in self.approvals if item.decision == "pending"]

    def is_ready(self) -> bool:
        return bool(self.approvals) and not self.unresolved()

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "turn_id": self.turn_id,
            "tool_calls": list(self.tool_calls),
            "policy_decisions": list(self.policy_decisions),
            "approvals": [item.to_dict() for item in self.approvals],
            "state": self.state,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PendingToolBatch":
        return cls(
            batch_id=data.get("batch_id", uuid.uuid4().hex),
            turn_id=data.get("turn_id", ""),
            tool_calls=list(data.get("tool_calls", [])),
            policy_decisions=list(data.get("policy_decisions", [])),
            approvals=[
                PendingApproval.from_dict(item)
                for item in data.get("approvals", [])
            ],
            state=data.get("state", "waiting"),
            created_at=data.get("created_at", _now()),
        )


@dataclass
class TaskState:
    task_id: str
    revision_id: str = ""
    parent_revision_id: str = ""
    supersedes_turn_id: str = ""
    title: str = ""
    status: str = "idle"
    step_index: int = 0
    started_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    last_error: str = ""
    todos: list[dict] = field(default_factory=list)
    recent_failures: list[str] = field(default_factory=list)
    pending_tool_batch: PendingToolBatch | None = None
    approval_grants: list[str] = field(default_factory=list)
    last_tool_name: str = ""
    last_tool_result: str = ""
    langfuse_trace_id: str = ""
    langfuse_root_observation_id: str = ""

    def touch(self, status: str | None = None):
        if status:
            self.status = status
        self.updated_at = _now()

    def next_step(self):
        self.step_index += 1
        self.touch("running")

    def mark_waiting(self, pending: PendingToolBatch):
        self.pending_tool_batch = pending
        self.touch("waiting_approval")

    def clear_pending(self):
        self.pending_tool_batch = None
        if self.status == "waiting_approval":
            self.touch("running")

    def mark_failed(self, error: str):
        self.last_error = error
        self.note_failure(error)
        self.approval_grants.clear()
        self.touch("failed")

    def mark_completed(self):
        self.clear_pending()
        self.approval_grants.clear()
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

    def grant_approval_scope(self, scope: str):
        if scope and scope not in self.approval_grants:
            self.approval_grants.append(scope)
        self.touch()

    def has_approval_scope(self, scope: str) -> bool:
        return bool(scope and scope in self.approval_grants)

    def note_tool_result(self, tool_name: str, result: str):
        self.last_tool_name = tool_name
        self.last_tool_result = result
        self.touch()

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "revision_id": self.revision_id,
            "parent_revision_id": self.parent_revision_id,
            "supersedes_turn_id": self.supersedes_turn_id,
            "title": self.title,
            "status": self.status,
            "step_index": self.step_index,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "last_error": self.last_error,
            "todos": self.todos,
            "recent_failures": self.recent_failures,
            "pending_tool_batch": self.pending_tool_batch.to_dict() if self.pending_tool_batch else None,
            "approval_grants": list(self.approval_grants),
            "last_tool_name": self.last_tool_name,
            "last_tool_result": self.last_tool_result,
            "langfuse_trace_id": self.langfuse_trace_id,
            "langfuse_root_observation_id": self.langfuse_root_observation_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskState":
        pending_batch = data.get("pending_tool_batch")
        legacy_pending = data.get("pending_approval")
        if pending_batch is None and legacy_pending:
            first_call = {
                "id": legacy_pending.get("tool_call_id", ""),
                "name": legacy_pending.get("tool_name", ""),
                "arguments": dict(legacy_pending.get("arguments", {})),
            }
            remaining = list(legacy_pending.get("remaining_tool_calls", []))
            approval = PendingApproval.from_dict(legacy_pending)
            pending_batch = {
                "batch_id": uuid.uuid4().hex,
                "turn_id": data.get("task_id", ""),
                "tool_calls": [first_call, *remaining],
                "policy_decisions": [
                    {
                        "action": "confirm",
                        "reason": approval.reason,
                        "requires_manual": approval.requires_manual,
                        "approval_scope": approval.approval_scope,
                        "approval_label": approval.approval_label,
                    },
                    *([None] * len(remaining)),
                ],
                "approvals": [approval.to_dict()],
                "state": "waiting",
            }
        return cls(
            task_id=data.get("task_id", ""),
            revision_id=data.get("revision_id", ""),
            parent_revision_id=data.get("parent_revision_id", ""),
            supersedes_turn_id=data.get("supersedes_turn_id", ""),
            title=data.get("title", ""),
            status=data.get("status", "idle"),
            step_index=int(data.get("step_index", 0)),
            started_at=data.get("started_at", _now()),
            updated_at=data.get("updated_at", _now()),
            last_error=data.get("last_error", ""),
            todos=list(data.get("todos", [])),
            recent_failures=list(data.get("recent_failures", [])),
            pending_tool_batch=PendingToolBatch.from_dict(pending_batch) if pending_batch else None,
            approval_grants=list(data.get("approval_grants", [])),
            last_tool_name=data.get("last_tool_name", ""),
            last_tool_result=data.get("last_tool_result", ""),
            langfuse_trace_id=data.get("langfuse_trace_id", ""),
            langfuse_root_observation_id=data.get("langfuse_root_observation_id", ""),
        )


@dataclass
class SessionState:
    session_id: str
    title: str = ""
    context_used_tokens: int = 0
    context_anchor_messages: int = 0
    context_anchor_digest: str = ""
    started_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    current_task: TaskState | None = None
    queued_inputs: list[dict] = field(default_factory=list)

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
            "context_used_tokens": self.context_used_tokens,
            "context_anchor_messages": self.context_anchor_messages,
            "context_anchor_digest": self.context_anchor_digest,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "current_task": self.current_task.to_dict() if self.current_task else None,
            "queued_inputs": self.queued_inputs,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionState":
        task = data.get("current_task")
        return cls(
            session_id=data.get("session_id", ""),
            title=data.get("title", ""),
            context_used_tokens=max(0, int(data.get("context_used_tokens", 0))),
            context_anchor_messages=max(0, int(data.get("context_anchor_messages", 0))),
            context_anchor_digest=str(data.get("context_anchor_digest", "")),
            started_at=data.get("started_at", _now()),
            updated_at=data.get("updated_at", _now()),
            current_task=TaskState.from_dict(task) if task else None,
            queued_inputs=list(data.get("queued_inputs", [])),
        )
