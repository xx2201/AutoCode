"""Speculative execution of completed, side-effect-free streamed tool calls."""

from __future__ import annotations

import concurrent.futures
import threading
from dataclasses import dataclass

from ..state import PolicyDecision, TaskState
from ..tools.base import ConcurrencyMode, ToolResult
from .engine import PreparedToolExecution, Runtime


@dataclass
class _Entry:
    tool_call: object
    decision: PolicyDecision
    future: concurrent.futures.Future | None = None


class StreamingToolExecutor:
    """Start safe reads during model streaming and commit them only after the model step."""

    def __init__(
        self,
        *,
        runtime: Runtime,
        task_state: TaskState,
        session_id: str,
        on_tool=None,
        max_workers: int = 8,
        trace_context: dict[str, str] | None = None,
    ):
        self.runtime = runtime
        self.task_state = task_state
        self.session_id = session_id
        self.on_tool = on_tool
        self.trace_context = trace_context
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        self._entries: dict[str, _Entry] = {}
        self._discarded = False
        self._lock = threading.Lock()

    def add_tool(self, tool_call) -> bool:
        """Register one complete call and eagerly start it only when it cannot write."""
        with self._lock:
            if self._discarded or tool_call.id in self._entries:
                return False
        decision = self.runtime.evaluate_tool_call(
            self.task_state,
            tool_call,
            self.session_id,
        )
        entry = _Entry(tool_call=tool_call, decision=decision)
        spec = self.runtime.concurrency_spec(tool_call)
        safe_to_start = (
            not getattr(tool_call, "parse_error", None)
            and decision.action == "allow"
            and spec.mode != ConcurrencyMode.EXCLUSIVE
            and not spec.main_thread
            and not spec.write_resources
        )
        if safe_to_start:
            entry.future = self._pool.submit(
                self.runtime.prepare_tool_call,
                self.task_state,
                tool_call,
                self.session_id,
                self.on_tool,
                decision,
                self.trace_context,
            )
        with self._lock:
            if self._discarded:
                if entry.future is not None:
                    entry.future.cancel()
                return False
            self._entries[tool_call.id] = entry
        return safe_to_start

    def decision_for(self, tool_call) -> PolicyDecision | None:
        entry = self._entries.get(tool_call.id)
        return entry.decision if entry is not None else None

    def commit(self, tool_calls: list) -> dict[str, object]:
        """Finalize matching speculative results in model order."""
        committed: dict[str, object] = {}
        expected_ids = {tool_call.id for tool_call in tool_calls}
        for tool_call in tool_calls:
            entry = self._entries.get(tool_call.id)
            if entry is None or entry.future is None:
                continue
            try:
                prepared: PreparedToolExecution = entry.future.result()
            except Exception as exc:
                committed[tool_call.id] = ToolResult(
                    text=(
                        "Error: speculative tool execution failed before commit: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    is_error=True,
                )
                continue
            committed[tool_call.id] = self.runtime.finalize_prepared_tool_call(
                self.task_state,
                tool_call,
                self.session_id,
                prepared,
            )
        for call_id, entry in self._entries.items():
            if call_id not in expected_ids and entry.future is not None:
                entry.future.cancel()
        self._pool.shutdown(wait=True, cancel_futures=True)
        return committed

    def discard(self) -> list[str]:
        """Cancel queued work and make every running result ineligible for commit."""
        with self._lock:
            if self._discarded:
                return []
            self._discarded = True
            entries = list(self._entries.values())
        discarded_ids = []
        for entry in entries:
            discarded_ids.append(str(entry.tool_call.id))
            if entry.future is not None:
                entry.future.cancel()
        self._pool.shutdown(wait=False, cancel_futures=True)
        return discarded_ids
