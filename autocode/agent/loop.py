"""Core agent loop."""

from ..context import ContextManager, MemoryManager, estimate_tokens, render_todos, system_prompt
from ..infra import BackgroundProcessManager, Sandbox, WorkspaceFS
from ..llm import LLM, ToolCall
from ..runtime import HookBus, Policy, RecoveryManager, Runtime
from ..state import (
    AuditLogger,
    LLMRoundRecorder,
    PendingApproval,
    PolicyDecision,
    SessionState,
    SessionStore,
    TaskState,
    TranscriptLogger,
    TraceRecorder,
    new_session_id,
    new_task_id,
    save_checkpoint,
)
from ..tools import ALL_TOOLS, build_tool_registry
from ..tools.agent import AgentTool
from ..tools.base import Tool
from ..tools.todo_write import TodoWriteTool


class Agent:
    _INTERRUPTED_TOOL_RESULT = (
        "Placeholder tool result: execution was interrupted by the user. "
        "If this tool is still needed, call it again."
    )
    _ROUND_LIMIT_SUMMARY_PROMPT = (
        "You have reached the maximum tool-call rounds for this task. "
        "Do not call any tools. Reply in Chinese. "
        "Give a concise wrap-up with these headings exactly: 已完成, 当前卡点, 建议下一步. "
        "In 建议下一步, tell the user they can reply “继续” if they want another pass."
    )

    def __init__(
        self,
        llm: LLM,
        tools: list[Tool] | None = None,
        max_context_tokens: int = 1_000_000,
        max_rounds: int = 50,
        workspace_root: str | None = None,
        auto_approve: bool = False,
    ):
        self.llm = llm
        self.tools = tools if tools is not None else [type(t)() for t in ALL_TOOLS]
        self.tool_registry = build_tool_registry(self.tools)
        self.messages: list[dict] = []
        self.context = ContextManager(max_tokens=max_context_tokens)
        self.max_rounds = max_rounds
        self.workspace_root = workspace_root or "."
        self.fs = WorkspaceFS(self.workspace_root)
        self.sandbox = Sandbox(self.workspace_root)
        self.processes = BackgroundProcessManager(self.workspace_root)
        self.memory = MemoryManager(self.workspace_root)
        self.sessions = SessionStore()
        self.recovery = RecoveryManager()
        self.hooks = HookBus()
        self.audit = AuditLogger()
        self.llm_rounds = LLMRoundRecorder()
        self.trace = TraceRecorder()
        self.transcript = TranscriptLogger()
        for event in (
            "user_message",
            "before_llm",
            "after_llm",
            "policy_decision",
            "before_tool",
            "after_tool",
            "approval_resolved",
            "task_status",
            "task_error",
            "todo_updated",
        ):
            self.hooks.on(event, self.audit.handle)
            self.hooks.on(event, self.trace.handle)
        self.policy = Policy(workspace_root=self.workspace_root, auto_approve=auto_approve)
        self.runtime = Runtime(self.tool_registry, policy=self.policy, hooks=self.hooks, recovery=self.recovery)
        self.session_state: SessionState | None = None

        for tool in self.tools:
            setattr(tool, "_fs", self.fs)
            setattr(tool, "_sandbox", self.sandbox)
            setattr(tool, "_process_manager", self.processes)
            if isinstance(tool, (AgentTool, TodoWriteTool)):
                tool._parent_agent = self

    @property
    def task_state(self) -> TaskState | None:
        if self.session_state is None:
            return None
        return self.session_state.current_task

    def _full_messages(self) -> list[dict]:
        return [{"role": "system", "content": self._build_system_prompt()}] + self.messages

    @staticmethod
    def _serialize_tool_call(tool_call: ToolCall) -> dict:
        return {
            "id": tool_call.id,
            "name": tool_call.name,
            "arguments": dict(tool_call.arguments),
        }

    @staticmethod
    def _deserialize_tool_calls(items: list[dict]) -> list[ToolCall]:
        return [
            ToolCall(
                id=item.get("id", ""),
                name=item.get("name", ""),
                arguments=dict(item.get("arguments", {})),
            )
            for item in items
        ]

    def _tool_schemas(self) -> list[dict]:
        return [tool.schema() for tool in self.tools]

    def _ensure_session(self) -> SessionState:
        if self.session_state is None:
            self.session_state = SessionState(session_id=new_session_id())
        return self.session_state

    def _event_payload(self, **extra) -> dict:
        payload = {}
        if self.session_state is not None:
            payload["session_id"] = self.session_state.session_id
        if self.task_state is not None:
            payload["task_id"] = self.task_state.task_id
            payload["task_title"] = self.task_state.title
        payload.update(extra)
        return payload

    def _build_system_prompt(self) -> str:
        todo_block = render_todos(self.task_state.todos) if self.task_state else ""
        task_block = self.sessions.render_task(self.task_state) if self.task_state else ""
        recovery_block = ""
        if self.task_state and self.task_state.recent_failures:
            recovery_block = "\n".join(f"- {item}" for item in self.task_state.recent_failures[-3:])
        return system_prompt(
            self.tools,
            cwd=str(self.fs.workspace_root),
            memory_block=self.memory.build_memory_block(),
            todo_block=todo_block,
            task_block=task_block,
            recovery_block=recovery_block,
        )

    def _ensure_task(self, title: str | None = None):
        session = self._ensure_session()
        if session.current_task is None or session.current_task.status in {"completed", "failed"}:
            session.set_current_task(
                TaskState(
                    task_id=new_task_id(),
                    title=(title or "").splitlines()[0][:120],
                    status="running",
                )
            )
            return
        session.current_task.touch("running")
        session.touch()

    def enable_auto_approve_for_current_task(self):
        if self.task_state is None:
            self._ensure_task()
        self.task_state.set_auto_approve(True)
        self.persist_session()

    def disable_auto_approve_for_current_task(self):
        if self.task_state is None:
            return
        self.task_state.set_auto_approve(False)
        self.persist_session()

    def persist_session(self):
        if self.session_state is None:
            return
        self.session_state.touch()
        save_checkpoint(self.session_state, self.messages, self.llm.model, workspace_root=self.workspace_root)
        self.sessions.sync(self.session_state, self.llm.model)

    def persist_task(self):
        self.persist_session()

    def _append_message(self, message: dict):
        self.messages.append(message)
        if self.session_state is not None:
            self.transcript.append_message(self.session_state.session_id, message)

    def _maybe_compress_messages(self):
        if (
            self.task_state is not None
            and hasattr(self.llm, "_call_with_retry")
            and estimate_tokens(self.messages) > int(self.context.max_tokens * 0.50)
        ):
            self.memory.schedule_project_memory_refresh(self.messages, self.llm)
        result = self.context.maybe_compress(self.messages, self.llm)
        if result.compressed and self.session_state is not None:
            self.transcript.append_compaction(
                self.session_state.session_id,
                {
                    "layers": list(result.layers),
                    "before_tokens": result.before_tokens,
                    "after_tokens": result.after_tokens,
                    "before_messages": result.before_messages,
                    "after_messages": result.after_messages,
                },
            )
        return result

    def compact_context(self):
        result = self._maybe_compress_messages()
        if result.compressed:
            self.persist_session()
        return result

    def chat(self, user_input: str, on_token=None, on_tool=None, approval_handler=None) -> str:
        if self.task_state and self.task_state.pending_approval:
            pending = self.task_state.pending_approval
            return (
                f"(task {self.task_state.task_id} is waiting for approval: "
                f"{pending.tool_name} - use /approve or /reject first)"
            )

        self._ensure_task(user_input)
        self._append_message({"role": "user", "content": user_input})
        self.hooks.emit("user_message", self._event_payload(content_preview=user_input[:200]))
        self._maybe_compress_messages()
        self.persist_session()
        return self._continue_loop(on_token=on_token, on_tool=on_tool, approval_handler=approval_handler)

    def approve_pending(
        self,
        approved: bool,
        on_tool=None,
        on_token=None,
        approval_handler=None,
        enable_auto_approve: bool = False,
    ) -> str:
        if self.task_state is None or self.task_state.pending_approval is None:
            return "No pending approval."

        pending = self.task_state.pending_approval
        remaining_tool_calls = self._deserialize_tool_calls(pending.remaining_tool_calls)
        self.task_state.clear_pending()
        if enable_auto_approve and approved:
            self.task_state.set_auto_approve(True)

        tool_call = ToolCall(
            id=pending.tool_call_id,
            name=pending.tool_name,
            arguments=pending.arguments,
        )

        if approved:
            result = self._execute_tool_call(
                tool_call,
                on_tool=on_tool,
                decision=PolicyDecision("confirm", pending.reason, requires_manual=pending.requires_manual),
            )
        else:
            result = self.runtime.blocked_result(tool_call.name, PolicyDecision("deny", "approval denied by user"))
        self.task_state.note_tool_result(tool_call.name, result)
        self.hooks.emit("approval_resolved", self._event_payload(tool_name=tool_call.name, approved=approved))

        self._append_message({"role": "tool", "tool_call_id": tool_call.id, "content": result})
        self._maybe_compress_messages()
        self.persist_session()

        if remaining_tool_calls:
            wait = self._handle_tool_calls(remaining_tool_calls, on_tool=on_tool, approval_handler=approval_handler)
            self._maybe_compress_messages()
            self.persist_session()
            if wait is not None:
                return wait

        return self._continue_loop(on_token=on_token, on_tool=on_tool, approval_handler=approval_handler)

    def restore_session(self, session_state: SessionState, messages: list[dict], model: str | None = None):
        self.session_state = session_state
        self.messages = messages
        if model:
            self.llm.model = model

    def _continue_loop(self, on_token=None, on_tool=None, approval_handler=None) -> str:
        if self.task_state is None:
            self._ensure_task()

        for _ in range(self.max_rounds):
            full_messages = self._full_messages()
            tool_schemas = self._tool_schemas()
            resp = self.runtime.call_llm(
                llm=self.llm,
                messages=full_messages,
                tools=tool_schemas,
                task_state=self.task_state,
                session_id=self.session_state.session_id,
                on_token=on_token,
            )
            self.llm_rounds.append_round(
                self.session_state.session_id,
                task_id=self.task_state.task_id,
                step_index=self.task_state.step_index,
                model=self.llm.model,
                messages=full_messages,
                tools=tool_schemas,
                response_content=resp.content,
                response_tool_calls=[self._serialize_tool_call(tool_call) for tool_call in resp.tool_calls],
                prompt_tokens=resp.prompt_tokens,
                completion_tokens=resp.completion_tokens,
            )

            if not resp.tool_calls:
                self._append_message(resp.message)
                if hasattr(self.llm, "_call_with_retry"):
                    self.memory.schedule_project_memory_refresh(self.messages, self.llm, force=True)
                self.task_state.mark_completed()
                self.hooks.emit("task_status", self._event_payload(status=self.task_state.status))
                self.persist_session()
                return resp.content

            self._append_message(resp.message)
            self.persist_session()

            wait = self._handle_tool_calls(resp.tool_calls, on_tool=on_tool, approval_handler=approval_handler)
            if wait is not None:
                return wait

            self._maybe_compress_messages()
            self.persist_session()

        summary = self._summarize_round_limit(on_token=on_token)
        self._append_message({"role": "assistant", "content": summary})
        self.task_state.mark_failed("reached maximum tool-call rounds")
        self.hooks.emit(
            "task_error",
            self._event_payload(status=self.task_state.status, error=self.task_state.last_error),
        )
        self.persist_session()
        return summary

    def _handle_tool_calls(self, tool_calls, on_tool=None, approval_handler=None) -> str | None:
        decisions = [
            self.runtime.evaluate_tool_call(self.task_state, tool_call, self.session_state.session_id)
            for tool_call in tool_calls
        ]

        if len(tool_calls) > 1 and all(decision.action == "allow" for decision in decisions):
            try:
                results = self.runtime.execute_tool_calls_parallel(
                    self.task_state,
                    tool_calls,
                    self.session_state.session_id,
                    on_tool=on_tool,
                )
            except KeyboardInterrupt:
                results = [self._interrupted_tool_result(tool_call) for tool_call in tool_calls]
            for tool_call, result in zip(tool_calls, results):
                self._append_message({"role": "tool", "tool_call_id": tool_call.id, "content": result})
            return None

        for index, (tool_call, decision) in enumerate(zip(tool_calls, decisions)):
            if decision.action == "allow":
                result = self._execute_tool_call(tool_call, on_tool=on_tool)
            elif decision.action == "deny":
                result = self.runtime.blocked_result(tool_call.name, decision)
            elif self._should_auto_approve(decision):
                result = self._execute_tool_call(tool_call, on_tool=on_tool, decision=decision)
                self.hooks.emit("approval_resolved", self._event_payload(tool_name=tool_call.name, approved=True))
            else:
                self.task_state.mark_waiting(
                    PendingApproval(
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.name,
                        arguments=tool_call.arguments,
                        reason=decision.reason,
                        requires_manual=decision.requires_manual,
                        remaining_tool_calls=[
                            self._serialize_tool_call(item)
                            for item in tool_calls[index + 1:]
                        ],
                    )
                )
                self.persist_session()

                if approval_handler is None:
                    return f"(waiting for approval: {tool_call.name} - {decision.reason or 'confirmation required'})"

                approval_response = approval_handler(self.task_state.pending_approval)
                enable_auto_approve = approval_response == "approve_all"
                approved = approval_response in {True, "approve", "approve_all"}
                self.task_state.clear_pending()
                if enable_auto_approve and approved:
                    self.task_state.set_auto_approve(True)

                if approved:
                    result = self._execute_tool_call(tool_call, on_tool=on_tool, decision=decision)
                else:
                    result = self.runtime.blocked_result(
                        tool_call.name,
                        PolicyDecision("deny", "approval denied by user"),
                    )
                self.hooks.emit(
                    "approval_resolved",
                    self._event_payload(tool_name=tool_call.name, approved=approved),
                )

            self.task_state.note_tool_result(tool_call.name, result)
            self._append_message({"role": "tool", "tool_call_id": tool_call.id, "content": result})

        return None

    def _execute_tool_call(self, tool_call: ToolCall, on_tool=None, decision: PolicyDecision | None = None) -> str:
        try:
            return self.runtime.execute_tool_call(
                self.task_state,
                tool_call,
                self.session_state.session_id,
                on_tool=on_tool,
                decision=decision,
            )
        except KeyboardInterrupt:
            return self._interrupted_tool_result(tool_call)

    def _interrupted_tool_result(self, tool_call: ToolCall) -> str:
        if self.task_state is not None:
            self.task_state.note_failure(f"{tool_call.name} interrupted by user")
        return self._INTERRUPTED_TOOL_RESULT

    def _should_auto_approve(self, decision: PolicyDecision) -> bool:
        return bool(
            self.task_state
            and self.task_state.auto_approve_for_task
            and decision.action == "confirm"
            and not decision.requires_manual
        )

    def _summarize_round_limit(self, on_token=None) -> str:
        summary_prompt = {"role": "user", "content": self._ROUND_LIMIT_SUMMARY_PROMPT}
        messages = self._full_messages() + [summary_prompt]
        try:
            resp = self.runtime.call_llm(
                llm=self.llm,
                messages=messages,
                tools=[],
                task_state=self.task_state,
                session_id=self.session_state.session_id,
                on_token=on_token,
            )
            self.llm_rounds.append_round(
                self.session_state.session_id,
                task_id=self.task_state.task_id,
                step_index=self.task_state.step_index,
                model=self.llm.model,
                messages=messages,
                tools=[],
                response_content=resp.content,
                response_tool_calls=[],
                prompt_tokens=resp.prompt_tokens,
                completion_tokens=resp.completion_tokens,
            )
            return resp.content or "已完成\n- 已达到本轮最大工具调用次数。\n\n当前卡点\n- 未能生成有效总结。\n\n建议下一步\n- 如需继续，请回复“继续”。"
        except Exception:
            return "已完成\n- 已达到本轮最大工具调用次数。\n\n当前卡点\n- 运行时在收尾总结阶段失败。\n\n建议下一步\n- 如需继续，请回复“继续”。"

    def reset(self):
        self.messages.clear()
        self.session_state = None
