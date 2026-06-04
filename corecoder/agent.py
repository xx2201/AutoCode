"""Core agent loop.

This is the heart of CoreCoder.  The pattern is simple:

    user message -> LLM (with tools) -> tool calls? -> execute -> loop
                                      -> text reply? -> return to user

It keeps looping until the LLM responds with plain text (no tool calls),
which means it's done working and ready to report back.
"""

from .llm import LLM
from .llm import ToolCall
from .tools import ALL_TOOLS, build_tool_registry
from .tools.base import Tool
from .tools.agent import AgentTool
from .tools.todo_write import TodoWriteTool
from .filesystem import WorkspaceFS
from .sandbox import Sandbox
from .memory import MemoryManager
from .prompt import system_prompt
from .context import ContextManager
from .checkpoint import new_task_id, save_checkpoint
from .hooks import HookBus
from .journal import AuditLogger
from .policy import Policy
from .recovery import RecoveryManager
from .runtime import Runtime
from .state import PendingApproval, PolicyDecision, TaskState
from .tasks import TaskStore
from .todo import render_todos
from .trace import TraceRecorder


class Agent:
    def __init__(
        self,
        llm: LLM,
        tools: list[Tool] | None = None,
        max_context_tokens: int = 128_000,
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
        self.memory = MemoryManager(self.workspace_root)
        self.tasks = TaskStore()
        self.recovery = RecoveryManager()
        self.hooks = HookBus()
        self.audit = AuditLogger()
        self.trace = TraceRecorder()
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
        self.task_state: TaskState | None = None

        # wire up sub-agent capability
        for t in self.tools:
            setattr(t, "_fs", self.fs)
            setattr(t, "_sandbox", self.sandbox)
            if isinstance(t, (AgentTool, TodoWriteTool)):
                t._parent_agent = self

    def _full_messages(self) -> list[dict]:
        return [{"role": "system", "content": self._build_system_prompt()}] + self.messages

    def _tool_schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools]

    def _build_system_prompt(self) -> str:
        todo_block = render_todos(self.task_state.todos) if self.task_state else ""
        task_block = self.tasks.render(self.task_state) if self.task_state else ""
        recovery_block = ""
        if self.task_state and self.task_state.recent_failures:
            recovery_block = "\n".join(f"- {item}" for item in self.task_state.recent_failures[-3:])
        return system_prompt(
            self.tools,
            cwd=str(self.fs.workspace_root),
            memory_block=self.memory.build_memory_block(self.task_state.task_id if self.task_state else None),
            todo_block=todo_block,
            task_block=task_block,
            recovery_block=recovery_block,
        )

    def _ensure_task(self, title: str | None = None):
        if self.task_state is None or self.task_state.status in {"completed", "failed"}:
            self.task_state = TaskState(
                task_id=new_task_id(),
                title=(title or "").splitlines()[0][:120],
                status="running",
            )
        else:
            self.task_state.touch("running")

    def enable_auto_approve_for_current_task(self):
        if self.task_state is None:
            self._ensure_task()
        self.task_state.set_auto_approve(True)
        self.persist_task()

    def disable_auto_approve_for_current_task(self):
        if self.task_state is None:
            return
        self.task_state.set_auto_approve(False)
        self.persist_task()

    def persist_task(self):
        if self.task_state is None:
            return
        save_checkpoint(self.task_state, self.messages, self.llm.model)
        self.tasks.sync(self.task_state, self.llm.model)

    def chat(self, user_input: str, on_token=None, on_tool=None, approval_handler=None) -> str:
        """Process one user message. May involve multiple LLM/tool rounds."""
        if self.task_state and self.task_state.pending_approval:
            pending = self.task_state.pending_approval
            return (
                f"(task {self.task_state.task_id} is waiting for approval: "
                f"{pending.tool_name} - use /approve or /reject first)"
            )

        self._ensure_task(user_input)
        self.messages.append({"role": "user", "content": user_input})
        self.hooks.emit(
            "user_message",
            {"task_id": self.task_state.task_id, "content_preview": user_input[:200]},
        )
        self.context.maybe_compress(self.messages, self.llm)
        self.persist_task()

        return self._continue_loop(on_token=on_token, on_tool=on_tool, approval_handler=approval_handler)

    def approve_pending(self, approved: bool, on_tool=None, on_token=None, approval_handler=None, enable_auto_approve: bool = False) -> str:
        """Handle a pending approval and continue the task."""
        if self.task_state is None or self.task_state.pending_approval is None:
            return "No pending approval."

        pending = self.task_state.pending_approval
        self.task_state.clear_pending()
        if enable_auto_approve and approved:
            self.task_state.set_auto_approve(True)

        tc = ToolCall(
            id=pending.tool_call_id,
            name=pending.tool_name,
            arguments=pending.arguments,
        )

        if approved:
            result = self.runtime.execute_tool_call(
                self.task_state,
                tc,
                on_tool=on_tool,
                decision=PolicyDecision("confirm", pending.reason, requires_manual=pending.requires_manual),
            )
        else:
            decision = PolicyDecision("deny", "approval denied by user")
            result = self.runtime.blocked_result(tc.name, decision)
        self.hooks.emit(
            "approval_resolved",
            {"task_id": self.task_state.task_id, "tool_name": tc.name, "approved": approved},
        )

        self.messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": result,
        })
        self.context.maybe_compress(self.messages, self.llm)
        self.persist_task()

        return self._continue_loop(on_token=on_token, on_tool=on_tool, approval_handler=approval_handler)

    def restore_task(self, task_state: TaskState, messages: list[dict], model: str | None = None):
        """Restore a task from checkpoint."""
        self.task_state = task_state
        self.messages = messages
        if model:
            self.llm.model = model

    def _continue_loop(self, on_token=None, on_tool=None, approval_handler=None) -> str:
        if self.task_state is None:
            self._ensure_task()

        for _ in range(self.max_rounds):
            resp = self.runtime.call_llm(
                llm=self.llm,
                messages=self._full_messages(),
                tools=self._tool_schemas(),
                task_state=self.task_state,
                on_token=on_token,
            )

            # no tool calls -> LLM is done, return text
            if not resp.tool_calls:
                self.messages.append(resp.message)
                self.task_state.mark_completed()
                self.hooks.emit(
                    "task_status",
                    {"task_id": self.task_state.task_id, "status": self.task_state.status},
                )
                self.persist_task()
                return resp.content

            # tool calls -> execute (parallel when multiple, like Claude Code's
            # StreamingToolExecutor which runs independent tools concurrently)
            self.messages.append(resp.message)
            self.persist_task()

            wait = self._handle_tool_calls(resp.tool_calls, on_tool=on_tool, approval_handler=approval_handler)
            if wait is not None:
                return wait

            self.context.maybe_compress(self.messages, self.llm)
            self.persist_task()

        self.task_state.mark_failed("reached maximum tool-call rounds")
        self.hooks.emit(
            "task_error",
            {"task_id": self.task_state.task_id, "status": self.task_state.status, "error": self.task_state.last_error},
        )
        self.persist_task()
        return "(reached maximum tool-call rounds)"

    def _handle_tool_calls(self, tool_calls, on_tool=None, approval_handler=None) -> str | None:
        decisions = [self.runtime.evaluate_tool_call(self.task_state, tc) for tc in tool_calls]

        if len(tool_calls) > 1 and all(d.action == "allow" for d in decisions):
            results = self.runtime.execute_tool_calls_parallel(self.task_state, tool_calls, on_tool=on_tool)
            for tc, result in zip(tool_calls, results):
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
            return None

        for tc, decision in zip(tool_calls, decisions):
            if decision.action == "allow":
                result = self.runtime.execute_tool_call(self.task_state, tc, on_tool=on_tool)
            elif decision.action == "deny":
                result = self.runtime.blocked_result(tc.name, decision)
            elif self._should_auto_approve(decision):
                result = self.runtime.execute_tool_call(self.task_state, tc, on_tool=on_tool, decision=decision)
                self.hooks.emit(
                    "approval_resolved",
                    {"task_id": self.task_state.task_id, "tool_name": tc.name, "approved": True},
                )
            else:
                self.task_state.mark_waiting(PendingApproval(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    arguments=tc.arguments,
                    reason=decision.reason,
                    requires_manual=decision.requires_manual,
                ))
                self.persist_task()

                if approval_handler is None:
                    return (
                        f"(waiting for approval: {tc.name} - {decision.reason or 'confirmation required'})"
                    )

                approval_response = approval_handler(self.task_state.pending_approval)
                enable_auto_approve = approval_response == "approve_all"
                approved = approval_response in {True, "approve", "approve_all"}
                self.task_state.clear_pending()
                if enable_auto_approve and approved:
                    self.task_state.set_auto_approve(True)

                if approved:
                    result = self.runtime.execute_tool_call(self.task_state, tc, on_tool=on_tool, decision=decision)
                else:
                    result = self.runtime.blocked_result(
                        tc.name,
                        PolicyDecision("deny", "approval denied by user"),
                    )
                self.hooks.emit(
                    "approval_resolved",
                    {"task_id": self.task_state.task_id, "tool_name": tc.name, "approved": approved},
                )

            self.messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

        return None

    def _should_auto_approve(self, decision: PolicyDecision) -> bool:
        return bool(
            self.task_state
            and self.task_state.auto_approve_for_task
            and decision.action == "confirm"
            and not decision.requires_manual
        )

    def reset(self):
        """Clear conversation history."""
        self.messages.clear()
        self.task_state = None
