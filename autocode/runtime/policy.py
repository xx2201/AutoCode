"""Composable pre-execution policy for tool calls."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ..state import PolicyDecision


APPROVAL_POLICIES = {"ask", "never"}


@dataclass(frozen=True)
class ToolPolicyExecution:
    """Immutable identity and arguments presented to pre-execution policies."""

    tool_name: str
    arguments: Mapping[str, object]


NextPolicy = Callable[[], PolicyDecision]
PreExecutePolicy = Callable[[ToolPolicyExecution, NextPolicy], PolicyDecision]
ToolPolicyGuard = Callable[[ToolPolicyExecution], str | None]


class Policy:
    """Run ordered tool policies, approval resolution, and monotonic guards."""

    def __init__(self, workspace_root: str | None = None, approval_policy: str = "ask"):
        self.workspace_root = workspace_root
        self._lock = threading.RLock()
        self._pre_execute: list[PreExecutePolicy] = []
        self._guards: list[ToolPolicyGuard] = []
        self.set_approval_policy(approval_policy)

    def set_approval_policy(self, approval_policy: str) -> None:
        if approval_policy not in APPROVAL_POLICIES:
            raise ValueError(f"Unsupported approval policy: {approval_policy}")
        self.approval_policy = approval_policy

    def on_pre_execute(
        self,
        handler: PreExecutePolicy,
        *,
        prepend: bool = False,
    ) -> Callable[[], None]:
        """Register one ordered policy and return its exact disposer."""
        if not callable(handler):
            raise TypeError("pre-execute policy must be callable")
        with self._lock:
            if prepend:
                self._pre_execute.insert(0, handler)
            else:
                self._pre_execute.append(handler)
        return lambda: self._remove_registration(self._pre_execute, handler)

    def add_guard(self, guard: ToolPolicyGuard) -> Callable[[], None]:
        """Register a post-policy guard that can only deny an allowed call."""
        if not callable(guard):
            raise TypeError("tool policy guard must be callable")
        with self._lock:
            self._guards.append(guard)
        return lambda: self._remove_registration(self._guards, guard)

    def evaluate_tool_call(self, tool_name: str, arguments: dict) -> PolicyDecision:
        execution = ToolPolicyExecution(
            tool_name=str(tool_name),
            arguments=MappingProxyType(dict(arguments)),
        )
        with self._lock:
            handlers = tuple(self._pre_execute)

        decision = self._run_waterfall(execution, handlers, 0)
        return self._apply_approval_policy(decision)

    def evaluate_guards(self, tool_name: str, arguments: dict) -> PolicyDecision:
        """Run monotonic guards immediately before dispatching a permitted call."""
        execution = ToolPolicyExecution(
            tool_name=str(tool_name),
            arguments=MappingProxyType(dict(arguments)),
        )
        with self._lock:
            guards = tuple(self._guards)
        for guard in guards:
            reason = guard(execution)
            if reason is None:
                continue
            if not isinstance(reason, str) or not reason.strip():
                raise TypeError("tool policy guard must return a non-empty reason or None")
            return PolicyDecision("deny", reason)
        return PolicyDecision("allow")

    def _run_waterfall(
        self,
        execution: ToolPolicyExecution,
        handlers: tuple[PreExecutePolicy, ...],
        index: int,
    ) -> PolicyDecision:
        if index == len(handlers):
            return PolicyDecision("allow")

        called = False

        def next_policy() -> PolicyDecision:
            nonlocal called
            if called:
                raise RuntimeError("pre-execute policy called next more than once")
            called = True
            return self._run_waterfall(execution, handlers, index + 1)

        decision = handlers[index](execution, next_policy)
        if not isinstance(decision, PolicyDecision):
            raise TypeError("pre-execute policy must return PolicyDecision")
        return decision

    def _apply_approval_policy(self, decision: PolicyDecision) -> PolicyDecision:
        if self.approval_policy == "never" and decision.action == "ask":
            return PolicyDecision(
                "deny",
                f"approval policy never rejects this request: {decision.reason}",
            )
        return decision

    def _remove_registration(self, registrations: list, target: Callable) -> None:
        with self._lock:
            for index, registered in enumerate(registrations):
                if registered is target:
                    registrations.pop(index)
                    return
