"""Runtime helpers for one agent step."""

from __future__ import annotations

import concurrent.futures

from .hooks import HookBus
from .policy import Policy
from .state import PolicyDecision, TaskState


class Runtime:
    def __init__(self, tool_registry: dict, policy: Policy | None = None, hooks: HookBus | None = None, recovery=None):
        self.tool_registry = tool_registry
        self.policy = policy or Policy()
        self.hooks = hooks or HookBus()
        self.recovery = recovery

    def call_llm(self, llm, messages: list[dict], tools: list[dict], task_state: TaskState, on_token=None):
        self.hooks.emit(
            "before_llm",
            {"task_id": task_state.task_id, "step_index": task_state.step_index + 1},
        )
        resp = llm.chat(messages=messages, tools=tools, on_token=on_token)
        task_state.next_step()
        self.hooks.emit(
            "after_llm",
            {
                "task_id": task_state.task_id,
                "step_index": task_state.step_index,
                "prompt_tokens": resp.prompt_tokens,
                "completion_tokens": resp.completion_tokens,
                "tool_calls": len(resp.tool_calls),
            },
        )
        return resp

    def evaluate_tool_call(self, task_state: TaskState, tool_call) -> PolicyDecision:
        decision = self.policy.evaluate_tool_call(tool_call.name, tool_call.arguments)
        self.hooks.emit(
            "policy_decision",
            {
                "task_id": task_state.task_id,
                "tool_name": tool_call.name,
                "arguments": tool_call.arguments,
                "decision": decision.to_dict(),
            },
        )
        return decision

    def execute_tool_call(self, task_state: TaskState, tool_call, on_tool=None, decision: PolicyDecision | None = None) -> str:
        tool = self.tool_registry.get(tool_call.name)
        if tool is None:
            result = f"Error: unknown tool '{tool_call.name}'"
            self.hooks.emit(
                "after_tool",
                {"task_id": task_state.task_id, "tool_name": tool_call.name, "result": result},
            )
            return result

        if on_tool:
            on_tool(tool_call.name, tool_call.arguments)

        self.hooks.emit(
            "before_tool",
            {"task_id": task_state.task_id, "tool_name": tool_call.name, "arguments": tool_call.arguments},
        )
        execute_kwargs = dict(tool_call.arguments)
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
        self.hooks.emit(
            "after_tool",
            {"task_id": task_state.task_id, "tool_name": tool_call.name, "result": result[:500]},
        )
        return result

    def execute_tool_calls_parallel(self, task_state: TaskState, tool_calls, on_tool=None) -> list[str]:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(tool_calls))) as pool:
            futures = [pool.submit(self.execute_tool_call, task_state, tc, on_tool) for tc in tool_calls]
            return [f.result() for f in futures]

    @staticmethod
    def blocked_result(tool_name: str, decision: PolicyDecision) -> str:
        label = "Blocked by policy" if decision.action == "deny" else "Approval required"
        return f"{label} for {tool_name}: {decision.reason or 'no reason provided'}"
