"""Runtime helpers for one agent step."""

from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass

from ..state import PolicyDecision, TaskState
from ..tools.base import ConcurrencySpec, ToolResult
from .hooks import HookBus
from .policy import Policy
from .scheduler import ExecutionGroup, plan_execution_groups


@dataclass(frozen=True)
class ToolExecutionOutcome:
    result: str | ToolResult
    duration_ms: float


@dataclass(frozen=True)
class PreparedToolExecution:
    """A tool execution whose result has not entered canonical history yet."""

    outcome: ToolExecutionOutcome
    group: ExecutionGroup
    spec: ConcurrencySpec


class Runtime:
    def __init__(
        self,
        tool_registry: dict,
        policy: Policy | None = None,
        hooks: HookBus | None = None,
        recovery=None,
        tracer=None,
    ):
        self.tool_registry = tool_registry
        self.policy = policy or Policy()
        self.hooks = hooks or HookBus()
        self.recovery = recovery
        self.tracer = tracer

    @staticmethod
    def _payload(session_id: str, task_state: TaskState, **extra) -> dict:
        payload = {
            "session_id": session_id,
            "task_id": task_state.task_id,
            "task_title": task_state.title,
        }
        payload.update(extra)
        return payload

    def call_llm(
        self,
        llm,
        messages: list[dict],
        tools: list[dict],
        task_state: TaskState,
        session_id: str,
        on_token=None,
        on_tool_call=None,
    ):
        self.hooks.emit(
            "before_llm",
            self._payload(session_id, task_state, step_index=task_state.step_index + 1),
        )
        kwargs = {
            "messages": messages,
            "tools": tools,
            "on_token": on_token,
        }
        if on_tool_call is not None and getattr(llm, "supports_streaming_tool_calls", False):
            kwargs["on_tool_call"] = on_tool_call
        resp = llm.chat(**kwargs)
        task_state.next_step()
        self.hooks.emit(
            "after_llm",
            self._payload(
                session_id,
                task_state,
                step_index=task_state.step_index,
                prompt_tokens=resp.prompt_tokens,
                completion_tokens=resp.completion_tokens,
                cache_read_tokens=resp.cache_read_tokens,
                cache_miss_tokens=resp.cache_miss_tokens,
                tool_calls=len(resp.tool_calls),
            ),
        )
        return resp

    def evaluate_tool_call(self, task_state: TaskState, tool_call, session_id: str) -> PolicyDecision:
        decision = self.policy.evaluate_tool_call(tool_call.name, tool_call.arguments)
        self.hooks.emit(
            "policy_decision",
            self._payload(
                session_id,
                task_state,
                tool_name=tool_call.name,
                arguments=tool_call.arguments,
                decision=decision.to_dict(),
            ),
        )
        return decision

    def execute_tool_call(
        self,
        task_state: TaskState,
        tool_call,
        session_id: str,
        on_tool=None,
        decision: PolicyDecision | None = None,
    ) -> str | ToolResult:
        prepared = self.prepare_tool_call(
            task_state,
            tool_call,
            session_id,
            on_tool=on_tool,
            decision=decision,
        )
        return self.finalize_prepared_tool_call(
            task_state,
            tool_call,
            session_id,
            prepared,
        )

    def prepare_tool_call(
        self,
        task_state: TaskState,
        tool_call,
        session_id: str,
        on_tool=None,
        decision: PolicyDecision | None = None,
    ) -> PreparedToolExecution:
        """Run a tool without committing recovery state or its result event."""
        spec = self._concurrency_spec(tool_call)
        group = ExecutionGroup(
            group_id=1,
            call_indexes=(0,),
            mode=spec.mode.value,
        )
        self._announce_tool_call(
            task_state,
            tool_call,
            session_id,
            on_tool=on_tool,
            group=group,
            spec=spec,
        )
        outcome = self._run_traced_tool_call(
            task_state,
            tool_call,
            decision=decision,
            group=group,
            spec=spec,
        )
        return PreparedToolExecution(outcome=outcome, group=group, spec=spec)

    def finalize_prepared_tool_call(
        self,
        task_state: TaskState,
        tool_call,
        session_id: str,
        prepared: PreparedToolExecution,
    ) -> str | ToolResult:
        return self._finalize_tool_call(
            task_state,
            tool_call,
            session_id,
            prepared.outcome,
            group=prepared.group,
            spec=prepared.spec,
        )

    def _run_traced_tool_call(
        self,
        task_state: TaskState,
        tool_call,
        *,
        decision: PolicyDecision | None,
        group: ExecutionGroup,
        spec: ConcurrencySpec,
    ) -> ToolExecutionOutcome:
        tracer = self.tracer
        if tracer is None or not tracer.enabled:
            return self._run_tool_call(
                task_state,
                tool_call,
                decision=decision,
            )

        metadata = {
            "task_id": task_state.task_id,
            "tool_call_id": tool_call.id,
            "policy_action": decision.action if decision else "allow",
            "execution_group_id": group.group_id,
            "execution_group_size": len(group.call_indexes),
            "concurrency_mode": spec.mode.value,
            "concurrency_reason": spec.reason,
        }
        with tracer.start_tool(
            name=f"tool.{tool_call.name or 'unknown'}",
            input_payload={"arguments": tool_call.arguments},
            metadata=metadata,
        ) as observation:
            outcome = self._run_tool_call(
                task_state,
                tool_call,
                decision=decision,
            )
            result = outcome.result
            result_text = result.text if isinstance(result, ToolResult) else result
            is_error = (
                result.is_error
                if isinstance(result, ToolResult)
                else result_text.startswith("Error:")
            )
            observation.update(
                output={
                    "result": result_text,
                    "multimodal": bool(
                        isinstance(result, ToolResult) and result.model_content
                    ),
                },
                metadata={**metadata, "success": not is_error},
                level="ERROR" if is_error else "DEFAULT",
                status_message=result_text if is_error else None,
            )
            return outcome

    def _run_tool_call(
        self,
        task_state: TaskState,
        tool_call,
        decision: PolicyDecision | None = None,
    ) -> ToolExecutionOutcome:
        started_at = time.monotonic()
        tool = self.tool_registry.get(tool_call.name)
        if tool is None:
            result = f"Error: unknown tool '{tool_call.name}'"
            return ToolExecutionOutcome(
                result=result,
                duration_ms=round((time.monotonic() - started_at) * 1000, 1),
            )
        execute_kwargs = dict(tool_call.arguments)
        if tool_call.name == "start_process":
            execute_kwargs["_task_id"] = task_state.task_id
        if tool_call.name == "shell_command" and decision is not None and decision.requires_manual:
            execute_kwargs["_confirmed_sensitive"] = True
        try:
            result = tool.execute(**execute_kwargs)
        except TypeError as e:
            result = f"Error: bad arguments for {tool_call.name}: {e}"
        except Exception as e:
            result = f"Error executing {tool_call.name}: {e}"
        return ToolExecutionOutcome(
            result=result,
            duration_ms=round((time.monotonic() - started_at) * 1000, 1),
        )

    def _finalize_tool_call(
        self,
        task_state: TaskState,
        tool_call,
        session_id: str,
        outcome: ToolExecutionOutcome,
        *,
        group: ExecutionGroup,
        spec: ConcurrencySpec,
    ) -> str | ToolResult:
        result = outcome.result
        if self.recovery is not None:
            result = self.recovery.note_tool_result(task_state, tool_call.name, result)
        result_text = result.text if isinstance(result, ToolResult) else result
        self.hooks.emit(
            "after_tool",
            self._payload(
                session_id,
                task_state,
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                arguments=tool_call.arguments,
                result=result_text[:4000],
                duration_ms=outcome.duration_ms,
                success=not (
                    result.is_error
                    if isinstance(result, ToolResult)
                    else result_text.startswith("Error:")
                ),
                multimodal=bool(isinstance(result, ToolResult) and result.model_content),
                execution_group_id=group.group_id,
                execution_group_size=len(group.call_indexes),
                concurrency_mode=spec.mode.value,
                concurrency_reason=spec.reason,
            ),
        )
        return result

    def _announce_tool_call(
        self,
        task_state: TaskState,
        tool_call,
        session_id: str,
        *,
        on_tool,
        group: ExecutionGroup,
        spec: ConcurrencySpec,
    ) -> None:
        if self.tool_registry.get(tool_call.name) is None:
            return
        if on_tool:
            on_tool(tool_call.name, tool_call.arguments)
        self.hooks.emit(
            "before_tool",
            self._payload(
                session_id,
                task_state,
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                arguments=tool_call.arguments,
                execution_group_id=group.group_id,
                execution_group_size=len(group.call_indexes),
                concurrency_mode=spec.mode.value,
                concurrency_reason=spec.reason,
            ),
        )

    def _concurrency_spec(self, tool_call) -> ConcurrencySpec:
        tool = self.tool_registry.get(tool_call.name)
        if tool is None:
            return ConcurrencySpec.exclusive("unknown tool")
        try:
            return tool.concurrency_spec(tool_call.arguments)
        except Exception as exc:
            return ConcurrencySpec.exclusive(
                f"invalid concurrency declaration: {type(exc).__name__}: {exc}"
            )

    def concurrency_spec(self, tool_call) -> ConcurrencySpec:
        """Expose the validated declaration to the streaming scheduler."""
        return self._concurrency_spec(tool_call)

    def execute_tool_calls_parallel(
        self,
        task_state: TaskState,
        tool_calls,
        session_id: str,
        on_tool=None,
        decisions: list[PolicyDecision | None] | None = None,
    ) -> list[str | ToolResult]:
        call_decisions = decisions or [None] * len(tool_calls)
        if len(call_decisions) != len(tool_calls):
            raise ValueError("Tool calls and policy decisions must have the same length.")
        if not tool_calls:
            return []

        specs = [self._concurrency_spec(tool_call) for tool_call in tool_calls]
        groups = plan_execution_groups(specs)
        results: list[str | ToolResult | None] = [None] * len(tool_calls)

        for group in groups:
            for index in group.call_indexes:
                self._announce_tool_call(
                    task_state,
                    tool_calls[index],
                    session_id,
                    on_tool=on_tool,
                    group=group,
                    spec=specs[index],
                )

            outcomes: list[ToolExecutionOutcome]
            if len(group.call_indexes) == 1:
                index = group.call_indexes[0]
                outcomes = [
                    self._run_traced_tool_call(
                        task_state,
                        tool_calls[index],
                        decision=call_decisions[index],
                        group=group,
                        spec=specs[index],
                    )
                ]
            else:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(8, len(group.call_indexes))
                ) as pool:
                    futures = [
                        pool.submit(
                            self._run_traced_tool_call,
                            task_state,
                            tool_calls[index],
                            decision=call_decisions[index],
                            group=group,
                            spec=specs[index],
                        )
                        for index in group.call_indexes
                    ]
                    outcomes = [future.result() for future in futures]

            for index, outcome in zip(group.call_indexes, outcomes):
                results[index] = self._finalize_tool_call(
                    task_state,
                    tool_calls[index],
                    session_id,
                    outcome,
                    group=group,
                    spec=specs[index],
                )

        if any(result is None for result in results):
            raise RuntimeError("Tool scheduler did not produce a result for every call.")
        return list(results)

    def invalid_tool_call_result(self, task_state: TaskState, tool_call, session_id: str) -> str:
        tool_name = tool_call.name or "<unknown>"
        raw_arguments = getattr(tool_call, "raw_arguments", "") or ""
        parse_error = getattr(tool_call, "parse_error", "") or "tool-call arguments could not be parsed"
        raw_preview = raw_arguments if len(raw_arguments) <= 300 else raw_arguments[:300] + "... (truncated)"
        result = (
            f"Error: invalid arguments for {tool_name}: {parse_error}\n"
            f"Raw arguments: {raw_preview}\n"
            f"Resend the same tool call with complete valid JSON arguments that match the tool schema."
        )
        if self.recovery is not None:
            result = self.recovery.note_tool_result(task_state, tool_name, result)
        self.hooks.emit(
            "after_tool",
            self._payload(
                session_id,
                task_state,
                tool_call_id=tool_call.id,
                tool_name=tool_name,
                result=result[:4000],
                duration_ms=0.0,
                success=False,
            ),
        )
        return result

    @staticmethod
    def blocked_result(tool_name: str, decision: PolicyDecision) -> str:
        label = "Blocked by policy" if decision.action == "deny" else "Approval required"
        return f"{label} for {tool_name}: {decision.reason or 'no reason provided'}"
