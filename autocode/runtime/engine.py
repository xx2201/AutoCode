"""Runtime helpers for one agent step."""

from __future__ import annotations

import concurrent.futures
import contextvars
import time

from ..state import PolicyDecision, TaskState
from ..tools.base import ToolResult
from .hooks import HookBus
from .policy import Policy


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

    def call_llm(self, llm, messages: list[dict], tools: list[dict], task_state: TaskState, session_id: str, on_token=None):
        self.hooks.emit(
            "before_llm",
            self._payload(session_id, task_state, step_index=task_state.step_index + 1),
        )
        resp = llm.chat(messages=messages, tools=tools, on_token=on_token)
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
        tracer = self.tracer
        if tracer is None or not tracer.enabled:
            return self._execute_tool_call(
                task_state,
                tool_call,
                session_id,
                on_tool=on_tool,
                decision=decision,
            )

        metadata = {
            "task_id": task_state.task_id,
            "tool_call_id": tool_call.id,
            "policy_action": decision.action if decision else "allow",
        }
        with tracer.start_tool(
            name=f"tool.{tool_call.name or 'unknown'}",
            input_payload={"arguments": tool_call.arguments},
            metadata=metadata,
        ) as observation:
            result = self._execute_tool_call(
                task_state,
                tool_call,
                session_id,
                on_tool=on_tool,
                decision=decision,
            )
            result_text = result.text if isinstance(result, ToolResult) else result
            is_error = result_text.startswith("Error:")
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
            return result

    def _execute_tool_call(
        self,
        task_state: TaskState,
        tool_call,
        session_id: str,
        on_tool=None,
        decision: PolicyDecision | None = None,
    ) -> str | ToolResult:
        started_at = time.monotonic()
        tool = self.tool_registry.get(tool_call.name)
        if tool is None:
            result = f"Error: unknown tool '{tool_call.name}'"
            self.hooks.emit(
                "after_tool",
                self._payload(
                    session_id,
                    task_state,
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.name,
                    result=result,
                    duration_ms=round((time.monotonic() - started_at) * 1000, 1),
                    success=False,
                ),
            )
            return result

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
            ),
        )
        execute_kwargs = dict(tool_call.arguments)
        if tool_call.name == "start_process":
            execute_kwargs["_task_id"] = task_state.task_id
        if tool_call.name == "bash" and decision is not None and decision.requires_manual:
            execute_kwargs["_confirmed_sensitive"] = True
        try:
            result = tool.execute(**execute_kwargs)
        except TypeError as e:
            result = f"Error: bad arguments for {tool_call.name}: {e}"
        except Exception as e:
            result = f"Error executing {tool_call.name}: {e}"
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
                result=result_text[:4000],
                duration_ms=round((time.monotonic() - started_at) * 1000, 1),
                success=not result_text.startswith("Error:"),
                multimodal=bool(isinstance(result, ToolResult) and result.model_content),
            ),
        )
        return result

    def execute_tool_calls_parallel(
        self,
        task_state: TaskState,
        tool_calls,
        session_id: str,
        on_tool=None,
    ) -> list[str | ToolResult]:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(tool_calls))) as pool:
            # 每个线程复制当前 OTel 上下文，确保并行 tool span 仍归属于当前 agent。
            futures = [
                pool.submit(
                    contextvars.copy_context().run,
                    self.execute_tool_call,
                    task_state,
                    tool_call,
                    session_id,
                    on_tool,
                )
                for tool_call in tool_calls
            ]
            return [f.result() for f in futures]

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
