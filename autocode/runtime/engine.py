"""Runtime helpers for one agent step."""

from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass

from ..state import PolicyDecision, TurnState
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
    def _payload(session_id: str, turn_state: TurnState, **extra) -> dict:
        payload = {
            "session_id": session_id,
            "turn_id": turn_state.turn_id,
            "turn_title": turn_state.title,
        }
        payload.update(extra)
        return payload

    def call_llm(
        self,
        llm,
        messages: list[dict],
        tools: list[dict],
        turn_state: TurnState,
        session_id: str,
        on_token=None,
        on_tool_call=None,
    ):
        self.hooks.emit(
            "before_llm",
            self._payload(session_id, turn_state, step_index=turn_state.step_index + 1),
        )
        kwargs = {
            "messages": messages,
            "tools": tools,
            "on_token": on_token,
        }
        if on_tool_call is not None and getattr(llm, "supports_streaming_tool_calls", False):
            kwargs["on_tool_call"] = on_tool_call
        resp = llm.chat(**kwargs)
        turn_state.next_step()
        self.hooks.emit(
            "after_llm",
            self._payload(
                session_id,
                turn_state,
                step_index=turn_state.step_index,
                prompt_tokens=resp.prompt_tokens,
                completion_tokens=resp.completion_tokens,
                cache_read_tokens=resp.cache_read_tokens,
                cache_miss_tokens=resp.cache_miss_tokens,
                tool_calls=len(resp.tool_calls),
            ),
        )
        return resp

    def evaluate_tool_call(self, turn_state: TurnState, tool_call, session_id: str) -> PolicyDecision:
        try:
            decision = self.policy.evaluate_tool_call(tool_call.name, tool_call.arguments)
        except Exception as exc:
            decision = PolicyDecision(
                "deny",
                f"pre-execute policy failed: {type(exc).__name__}: {exc}",
            )
        self.hooks.emit(
            "policy_decision",
            self._payload(
                session_id,
                turn_state,
                tool_name=tool_call.name,
                arguments=tool_call.arguments,
                decision=decision.to_dict(),
            ),
        )
        return decision

    def evaluate_tool_guards(
        self,
        turn_state: TurnState,
        tool_call,
        session_id: str,
    ) -> PolicyDecision:
        """Run fail-closed monotonic guards immediately before tool dispatch."""
        try:
            decision = self.policy.evaluate_guards(tool_call.name, tool_call.arguments)
        except Exception as exc:
            decision = PolicyDecision(
                "deny",
                f"tool policy guard failed: {type(exc).__name__}: {exc}",
            )
        if decision.action == "deny":
            self.hooks.emit(
                "policy_decision",
                self._payload(
                    session_id,
                    turn_state,
                    tool_name=tool_call.name,
                    arguments=tool_call.arguments,
                    decision=decision.to_dict(),
                    policy_stage="guard",
                ),
            )
        return decision

    def execute_tool_call(
        self,
        turn_state: TurnState,
        tool_call,
        session_id: str,
        on_tool=None,
        decision: PolicyDecision | None = None,
        trace_context: dict[str, str] | None = None,
    ) -> str | ToolResult:
        prepared = self.prepare_tool_call(
            turn_state,
            tool_call,
            session_id,
            on_tool=on_tool,
            decision=decision,
            trace_context=trace_context,
        )
        return self.finalize_prepared_tool_call(
            turn_state,
            tool_call,
            session_id,
            prepared,
        )

    def prepare_tool_call(
        self,
        turn_state: TurnState,
        tool_call,
        session_id: str,
        on_tool=None,
        decision: PolicyDecision | None = None,
        trace_context: dict[str, str] | None = None,
    ) -> PreparedToolExecution:
        """Run a tool without committing recovery state or its result event."""
        spec = self._concurrency_spec(tool_call)
        group = ExecutionGroup(
            group_id=1,
            call_indexes=(0,),
            mode=spec.mode.value,
        )
        guard = self.evaluate_tool_guards(turn_state, tool_call, session_id)
        if guard.action == "deny":
            return PreparedToolExecution(
                outcome=ToolExecutionOutcome(
                    result=ToolResult(
                        text=self.blocked_result(tool_call.name, guard),
                        is_error=True,
                    ),
                    duration_ms=0.0,
                ),
                group=group,
                spec=spec,
            )
        self._announce_tool_call(
            turn_state,
            tool_call,
            session_id,
            on_tool=on_tool,
            group=group,
            spec=spec,
        )
        outcome = self._run_traced_tool_call(
            turn_state,
            tool_call,
            decision=decision,
            group=group,
            spec=spec,
            trace_context=trace_context,
        )
        return PreparedToolExecution(outcome=outcome, group=group, spec=spec)

    def finalize_prepared_tool_call(
        self,
        turn_state: TurnState,
        tool_call,
        session_id: str,
        prepared: PreparedToolExecution,
    ) -> str | ToolResult:
        return self._finalize_tool_call(
            turn_state,
            tool_call,
            session_id,
            prepared.outcome,
            group=prepared.group,
            spec=prepared.spec,
        )

    def _run_traced_tool_call(
        self,
        turn_state: TurnState,
        tool_call,
        *,
        decision: PolicyDecision | None,
        group: ExecutionGroup,
        spec: ConcurrencySpec,
        trace_context: dict[str, str] | None = None,
    ) -> ToolExecutionOutcome:
        tracer = self.tracer
        if tracer is None or not tracer.enabled:
            return self._run_tool_call(
                turn_state,
                tool_call,
                decision=decision,
            )

        metadata = {
            "turn_id": turn_state.turn_id,
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
            trace_context=trace_context,
        ) as observation:
            outcome = self._run_tool_call(
                turn_state,
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
        turn_state: TurnState,
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
            execute_kwargs["_turn_id"] = turn_state.turn_id
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
        turn_state: TurnState,
        tool_call,
        session_id: str,
        outcome: ToolExecutionOutcome,
        *,
        group: ExecutionGroup,
        spec: ConcurrencySpec,
    ) -> str | ToolResult:
        result = outcome.result
        if self.recovery is not None:
            result = self.recovery.note_tool_result(turn_state, tool_call.name, result)
        result_text = result.text if isinstance(result, ToolResult) else result
        self.hooks.emit(
            "after_tool",
            self._payload(
                session_id,
                turn_state,
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
        turn_state: TurnState,
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
                turn_state,
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
        turn_state: TurnState,
        tool_calls,
        session_id: str,
        on_tool=None,
        decisions: list[PolicyDecision | None] | None = None,
        trace_context: dict[str, str] | None = None,
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
            outcomes_by_index: dict[int, ToolExecutionOutcome] = {}
            runnable_indexes: list[int] = []
            for index in group.call_indexes:
                guard = self.evaluate_tool_guards(
                    turn_state,
                    tool_calls[index],
                    session_id,
                )
                if guard.action == "deny":
                    outcomes_by_index[index] = ToolExecutionOutcome(
                        result=ToolResult(
                            text=self.blocked_result(tool_calls[index].name, guard),
                            is_error=True,
                        ),
                        duration_ms=0.0,
                    )
                    continue
                runnable_indexes.append(index)
                self._announce_tool_call(
                    turn_state,
                    tool_calls[index],
                    session_id,
                    on_tool=on_tool,
                    group=group,
                    spec=specs[index],
                )

            if len(runnable_indexes) == 1:
                index = runnable_indexes[0]
                outcomes_by_index[index] = self._run_traced_tool_call(
                    turn_state,
                    tool_calls[index],
                    decision=call_decisions[index],
                    group=group,
                    spec=specs[index],
                    trace_context=trace_context,
                )
            elif len(runnable_indexes) > 1:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(8, len(runnable_indexes))
                ) as pool:
                    futures = {
                        index: pool.submit(
                            self._run_traced_tool_call,
                            turn_state,
                            tool_calls[index],
                            decision=call_decisions[index],
                            group=group,
                            spec=specs[index],
                            trace_context=trace_context,
                        )
                        for index in runnable_indexes
                    }
                    for index, future in futures.items():
                        outcomes_by_index[index] = future.result()

            for index in group.call_indexes:
                results[index] = self._finalize_tool_call(
                    turn_state,
                    tool_calls[index],
                    session_id,
                    outcomes_by_index[index],
                    group=group,
                    spec=specs[index],
                )

        if any(result is None for result in results):
            raise RuntimeError("Tool scheduler did not produce a result for every call.")
        return list(results)

    def invalid_tool_call_result(self, turn_state: TurnState, tool_call, session_id: str) -> str:
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
            result = self.recovery.note_tool_result(turn_state, tool_name, result)
        self.hooks.emit(
            "after_tool",
            self._payload(
                session_id,
                turn_state,
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
