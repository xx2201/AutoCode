"""Core agent loop."""

from contextlib import contextmanager

from ..context import ContextManager, MemoryManager, estimate_tokens, render_todos, runtime_state_block, static_system_prompt
from ..infra import BackgroundProcessManager, Sandbox, WorkspaceFS
from ..llm import LLM, ToolCall
from ..message_content import content_text, is_internal_visual_context, user_content
from ..message_projection import serialize_anthropic_messages, serialize_chat_completions
from ..runtime import HookBus, Policy, RecoveryManager, Runtime
from ..skills import SkillError, SkillManager
from ..state import (
    AuditLogger,
    PendingApproval,
    PolicyDecision,
    SessionState,
    SessionStore,
    TaskState,
    TranscriptLogger,
    TraceRecorder,
    TurnController,
    new_message_id,
    new_revision_id,
    new_session_id,
    new_task_id,
    save_checkpoint,
    save_turn_queue,
)
from ..tools import ALL_TOOLS, build_tool_registry
from ..tools.agent import AgentTool
from ..tools.base import Tool, ToolResult
from ..tools.edit import EditFileTool
from ..tools.file_state import FileReadTracker
from ..tools.read import ReadTool
from ..tools.write import WriteFileTool
from ..tools.skill import SkillTool
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
        tool_factory=None,
        mcp_manager=None,
        own_mcp_manager: bool = False,
        max_context_tokens: int = 1_000_000,
        max_rounds: int = 50,
        workspace_root: str | None = None,
        permission_mode: str = "ask",
        turn_controller: TurnController | None = None,
    ):
        self.llm = llm
        self._tool_factory = tool_factory
        self.mcp_manager = mcp_manager
        self._owns_mcp_manager = own_mcp_manager
        initial_tools = tools if tools is not None else self._fresh_tools()
        self._static_tools = [
            tool for tool in initial_tools
            if not (self.mcp_manager is not None and tool.name.startswith("mcp_"))
        ]
        self.tools: list[Tool] = []
        self.tool_registry: dict[str, Tool] = {}
        self.messages: list[dict] = []
        self.context = ContextManager(max_tokens=max_context_tokens)
        self._last_prompt_tokens = 0
        self.max_rounds = max_rounds
        self.workspace_root = workspace_root or "."
        self.fs = WorkspaceFS(self.workspace_root)
        self.sandbox = Sandbox(self.workspace_root)
        self.processes = BackgroundProcessManager(self.workspace_root)
        self.memory = MemoryManager(self.workspace_root)
        self.skills = SkillManager(self.workspace_root)
        self.file_reads = FileReadTracker()
        self.sessions = SessionStore()
        self.recovery = RecoveryManager()
        self.hooks = HookBus()
        self.audit = AuditLogger()
        self.trace = TraceRecorder()
        self.transcript = TranscriptLogger()
        self.turn_controller = turn_controller or TurnController()
        for event in (
            "user_message",
            "turn_started",
            "before_llm",
            "after_llm",
            "context_compaction",
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
        self.hooks.on("task_status", self._handle_lifecycle_event)
        self.hooks.on("task_error", self._handle_lifecycle_event)
        self.policy = Policy(
            workspace_root=self.workspace_root,
            permission_mode=permission_mode,
        )
        self._sync_mcp_tools()
        self.runtime = Runtime(
            self.tool_registry,
            policy=self.policy,
            hooks=self.hooks,
            recovery=self.recovery,
            tracer=getattr(self.llm, "tracer", None),
        )
        self.session_state: SessionState | None = None

    @property
    def task_state(self) -> TaskState | None:
        if self.session_state is None:
            return None
        return self.session_state.current_task

    def context_usage(self) -> dict:
        """Return the current conversation-window occupancy, not cumulative billing tokens."""
        estimated_tokens = estimate_tokens(self.messages)
        persisted_tokens = (
            self.session_state.context_used_tokens
            if self.session_state is not None
            else 0
        )
        used_tokens = max(estimated_tokens, self._last_prompt_tokens, persisted_tokens)
        window_tokens = max(1, int(self.context.max_tokens))
        used_tokens = min(used_tokens, window_tokens)
        return {
            "used_tokens": used_tokens,
            "window_tokens": window_tokens,
            "remaining_tokens": max(0, window_tokens - used_tokens),
            "used_percent": round(used_tokens / window_tokens * 100, 1),
        }

    def _fresh_tools(self) -> list[Tool]:
        if self._tool_factory is not None:
            return list(self._tool_factory())
        return [tool.clone() for tool in ALL_TOOLS]

    def _attach_tool(self, tool: Tool) -> None:
        setattr(tool, "_fs", self.fs)
        setattr(tool, "_sandbox", self.sandbox)
        setattr(tool, "_process_manager", self.processes)
        if isinstance(tool, (ReadTool, EditFileTool, WriteFileTool)):
            tool._file_read_tracker = self.file_reads
        if isinstance(tool, SkillTool):
            tool._skill_manager = self.skills
        if isinstance(tool, (AgentTool, TodoWriteTool)):
            tool._parent_agent = self

    def _sync_mcp_tools(self) -> None:
        dynamic = self.mcp_manager.snapshot_tools() if self.mcp_manager is not None else []
        self.tools = [*self._static_tools, *dynamic]
        for tool in self.tools:
            self._attach_tool(tool)
        self.tool_registry = build_tool_registry(self.tools)
        if hasattr(self, "runtime"):
            self.runtime.tool_registry = self.tool_registry

    def _request_messages(self) -> list[dict]:
        self._sync_mcp_tools()
        system_prompt = self._build_static_system_prompt()
        runtime_tail = self._build_runtime_tail()
        if getattr(self.llm, "api_format", "chat_completions") == "messages":
            return serialize_anthropic_messages(system_prompt, self.messages, runtime_tail)
        return serialize_chat_completions(system_prompt, self.messages, runtime_tail)

    @classmethod
    def _project_model_history(cls, history: list[dict]) -> list[dict]:
        """Translate canonical history into strict Chat Completions messages.

        Canonical tool results retain their typed visual content and call id, matching
        Claude's tool_use/tool_result history. Chat Completions cannot place images in
        a tool message, so the compatibility user carrier is produced only while
        serializing an explicit Chat Completions request. It never enters Messages
        requests, checkpoints, transcripts, CLI, or Web UI.
        """
        return serialize_chat_completions("", history)[1:]

    @staticmethod
    def _serialize_tool_call(tool_call: ToolCall) -> dict:
        data = {
            "id": tool_call.id,
            "name": tool_call.name,
            "arguments": dict(tool_call.arguments),
        }
        if tool_call.raw_arguments is not None:
            data["raw_arguments"] = tool_call.raw_arguments
        if tool_call.parse_error is not None:
            data["parse_error"] = tool_call.parse_error
        return data

    @staticmethod
    def _deserialize_tool_calls(items: list[dict]) -> list[ToolCall]:
        return [
            ToolCall(
                id=item.get("id", ""),
                name=item.get("name", ""),
                arguments=dict(item.get("arguments", {})),
                raw_arguments=item.get("raw_arguments"),
                parse_error=item.get("parse_error"),
            )
            for item in items
        ]

    def _tool_schemas(self) -> list[dict]:
        self._sync_mcp_tools()
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
            payload["turn_id"] = self.task_state.task_id
            payload["revision_id"] = self.task_state.revision_id
            payload["task_title"] = self.task_state.title
        payload.update(extra)
        return payload

    def _build_static_system_prompt(self) -> str:
        return static_system_prompt(
            self.tools,
            cwd=str(self.fs.workspace_root),
            rules_block=self.memory.build_rules_block(),
            skills_block=self.skills.catalog_block() if "skill" in self.tool_registry else "",
        )

    def _build_runtime_tail(self) -> str:
        todo_block = render_todos(self.task_state.todos) if self.task_state else ""
        task_block = self.sessions.render_task(self.task_state) if self.task_state else ""
        recovery_block = ""
        if self.task_state and self.task_state.recent_failures:
            recovery_block = "\n".join(f"- {item}" for item in self.task_state.recent_failures[-3:])
        return runtime_state_block(
            project_memory_block=self.memory.build_project_memory_block(),
            todo_block=todo_block,
            task_block=task_block,
            recovery_block=recovery_block,
        )

    def _handle_lifecycle_event(self, event: str, payload: dict):
        task_id = payload.get("task_id", "")
        try:
            if event == "task_status" and payload.get("status") == "completed":
                self.processes.cleanup_task_processes(task_id)
            elif event == "task_error":
                self.processes.cleanup_task_processes(task_id)
        except Exception:
            return

    def _ensure_task(self, title: str | None = None):
        session = self._ensure_session()
        if session.current_task is None or session.current_task.status in {"completed", "failed"}:
            session.set_current_task(
                TaskState(
                    task_id=new_task_id(),
                    revision_id=new_revision_id(),
                    title=(title or "").splitlines()[0][:120],
                    status="running",
                )
            )
            return
        session.current_task.touch("running")
        session.touch()

    def set_permission_mode(self, permission_mode: str) -> None:
        self.policy.set_permission_mode(permission_mode)

    def persist_session(self):
        if self.session_state is None:
            return
        self.session_state.touch()
        self.session_state.queued_inputs = [
            item.to_dict() for item in self.turn_controller.queued()
        ]
        if self.session_state.queued_inputs:
            save_turn_queue(self.session_state.session_id, self.session_state.queued_inputs)
        save_checkpoint(self.session_state, self.messages, self.llm.model, workspace_root=self.workspace_root)
        self.sessions.sync(self.session_state, self.llm.model)

    def persist_task(self):
        self.persist_session()

    def _append_message(self, message: dict):
        stored = dict(message)
        stored.setdefault("message_id", new_message_id())
        if self.task_state is not None:
            stored.setdefault("turn_id", self.task_state.task_id)
            stored.setdefault("revision_id", self.task_state.revision_id)
        stored.setdefault("message_kind", stored.get("role", "message"))
        self.messages.append(stored)
        if self.session_state is not None:
            self.transcript.append_message(self.session_state.session_id, stored)

    def _append_tool_result(self, tool_call: ToolCall, result: str | ToolResult) -> str:
        text = result.text if isinstance(result, ToolResult) else result
        message = {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "tool_name": tool_call.name,
            "tool_arguments": tool_call.arguments,
            "content": text,
        }
        if isinstance(result, ToolResult) and result.model_content:
            message["model_content"] = [dict(item) for item in result.model_content]
        self._append_message(message)
        return text

    def _maybe_compress_messages(self):
        effective_used = self.context.effective_used(
            self.messages,
            last_prompt_tokens=self._last_prompt_tokens,
        )
        if (
            self.task_state is not None
            and hasattr(self.llm, "_call_with_retry")
            and effective_used > int(self.context.max_tokens * 0.50)
        ):
            self.memory.schedule_project_memory_refresh(self.messages, self.llm)
        result = self.context.maybe_compress(
            self.messages,
            self.llm,
            last_prompt_tokens=self._last_prompt_tokens,
        )
        if result.compressed and self.session_state is not None:
            saved_tokens = max(0, result.before_tokens - result.after_tokens)
            self.transcript.append_compaction(
                self.session_state.session_id,
                {
                    "layers": list(result.layers),
                    "before_tokens": result.before_tokens,
                    "after_tokens": result.after_tokens,
                    "saved_tokens": saved_tokens,
                    "before_messages": result.before_messages,
                    "after_messages": result.after_messages,
                },
            )
            self.hooks.emit(
                "context_compaction",
                self._event_payload(
                    layers=list(result.layers),
                    before_tokens=result.before_tokens,
                    after_tokens=result.after_tokens,
                    saved_tokens=saved_tokens,
                    before_messages=result.before_messages,
                    after_messages=result.after_messages,
                ),
            )
        return result

    def compact_context(self):
        result = self._maybe_compress_messages()
        if result.compressed:
            self.persist_session()
        return result

    @contextmanager
    def _turn_trace(
        self,
        *,
        name: str,
        input_payload: dict,
        tags: list[str],
        continue_turn: bool = False,
    ):
        tracer = getattr(self.llm, "tracer", None)
        if tracer is None or not tracer.enabled or self.session_state is None:
            yield None
            return

        metadata = self._event_payload(
            workspace_root=str(self.fs.workspace_root),
            llm_backend=type(self.llm).__name__,
            permission_mode=self.policy.permission_mode,
            task_status=self.task_state.status if self.task_state else None,
            step_index=self.task_state.step_index if self.task_state else None,
        )
        task = self.task_state
        trace_context = None
        # 审批来自后续请求；复用已持久化的根观测，避免同一 turn 被拆成多条 trace。
        if continue_turn and task is not None and task.langfuse_trace_id:
            trace_context = {"trace_id": task.langfuse_trace_id}
            if task.langfuse_root_observation_id:
                trace_context["parent_span_id"] = task.langfuse_root_observation_id

        with tracer.start_agent_turn(
            name=name,
            input_payload=input_payload,
            session_id=self.session_state.session_id,
            trace_name="autocode-agent-turn",
            metadata=metadata,
            tags=tags,
            trace_context=trace_context,
        ) as observation:
            if task is not None and not task.langfuse_trace_id:
                task.langfuse_trace_id = str(getattr(observation, "trace_id", "") or "")
                task.langfuse_root_observation_id = str(getattr(observation, "id", "") or "")
            yield observation

    def _finalize_turn_trace(self, observation, response_text: str):
        if observation is None:
            return
        pending = self.task_state.pending_approval if self.task_state else None
        observation.update(
            output={
                "text": response_text,
                "status": self.task_state.status if self.task_state else None,
                "pending_tool": pending.tool_name if pending else None,
                "pending_reason": pending.reason if pending else None,
            },
            metadata=self._event_payload(
                workspace_root=str(self.fs.workspace_root),
                llm_backend=type(self.llm).__name__,
                permission_mode=self.policy.permission_mode,
                task_status=self.task_state.status if self.task_state else None,
                step_index=self.task_state.step_index if self.task_state else None,
            ),
        )

    def _record_turn_error(self, observation, exc: Exception):
        if observation is None:
            return
        observation.update(
            output={"error": str(exc)},
            metadata=self._event_payload(
                workspace_root=str(self.fs.workspace_root),
                llm_backend=type(self.llm).__name__,
                error_type=type(exc).__name__,
                task_status=self.task_state.status if self.task_state else None,
            ),
            level="ERROR",
            status_message=str(exc),
        )

    def chat(
        self,
        user_input: str,
        on_token=None,
        on_tool=None,
        approval_handler=None,
        image_parts: list[dict] | None = None,
        attachments: list[dict] | None = None,
        raw_user_prompt: str | None = None,
    ) -> str:
        if self.task_state and self.task_state.pending_approval:
            pending = self.task_state.pending_approval
            return (
                f"(task {self.task_state.task_id} is waiting for approval: "
                f"{pending.tool_name} - use /approve or /reject first)"
            )

        original_prompt = raw_user_prompt if raw_user_prompt is not None else user_input
        session = self._ensure_session()
        if not session.title:
            session.title = (user_input.strip().splitlines() or ["上传文件会话"])[0][:120]
        self._ensure_task(original_prompt)
        try:
            explicit_skill = self.skills.explicit_invocation(user_input)
        except SkillError as exc:
            return f"Error: {exc}"
        effective_input = user_input
        if explicit_skill:
            effective_input = (
                f"{user_input}\n\n"
                "[The user explicitly invoked the following skill. Treat its content as workflow instructions.]\n\n"
                f"{explicit_skill}"
            )
        message_content = effective_input
        trace_content = user_content(effective_input, image_parts)
        turn_id = self.task_state.task_id
        self.turn_controller.start_turn(turn_id)
        self.hooks.emit("turn_started", self._event_payload(status="running"))
        with self._turn_trace(
            name="agent.chat",
            input_payload={"user_message": trace_content},
            tags=["autocode", "chat", *(["multimodal"] if image_parts else [])],
        ) as observation:
            try:
                user_message = {
                    "role": "user",
                    "content": message_content,
                    "message_kind": "prompt",
                    "raw_prompt": original_prompt,
                }
                if attachments:
                    user_message["attachments"] = list(attachments)
                if image_parts:
                    user_message["model_content"] = [dict(item) for item in image_parts]
                self._append_message(user_message)
                self.hooks.emit("user_message", self._event_payload(content_preview=original_prompt[:200]))
                self._maybe_compress_messages()
                self.persist_session()
                response = self._continue_loop(on_token=on_token, on_tool=on_tool, approval_handler=approval_handler)
            except Exception as exc:
                self.turn_controller.finish_turn(turn_id)
                self._record_turn_error(observation, exc)
                raise
            self._finalize_turn_trace(observation, response)
            return response

    def edit_last_turn(
        self,
        turn_id: str,
        prompt: str,
        on_token=None,
        on_tool=None,
        approval_handler=None,
        image_parts: list[dict] | None = None,
        attachments: list[dict] | None = None,
        raw_user_prompt: str | None = None,
    ) -> str:
        """Supersede the last completed turn while leaving workspace files untouched."""
        normalized_prompt = prompt.strip()
        original_prompt = raw_user_prompt if raw_user_prompt is not None else normalized_prompt
        if not normalized_prompt:
            raise ValueError("Edited prompt is required.")
        task = self.task_state
        if task is None or task.status != "completed":
            raise ValueError("Only the last completed turn can be edited.")
        if task.task_id != turn_id:
            raise ValueError(f"Turn '{turn_id}' is not the last completed turn.")

        prompt_index = next(
            (
                index
                for index in range(len(self.messages) - 1, -1, -1)
                if self.messages[index].get("role") == "user"
                and self.messages[index].get("message_kind", "prompt") == "prompt"
                and self.messages[index].get("turn_id", turn_id) == turn_id
            ),
            None,
        )
        if prompt_index is None:
            raise ValueError(f"Prompt for turn '{turn_id}' was not found.")

        old_message = self.messages[prompt_index]
        old_revision_id = task.revision_id
        new_task = TaskState(
            task_id=new_task_id(),
            revision_id=new_revision_id(),
            parent_revision_id=old_revision_id,
            supersedes_turn_id=turn_id,
            title=original_prompt.strip().splitlines()[0][:120],
            status="running",
        )
        self.messages = self.messages[:prompt_index]
        self.session_state.set_current_task(new_task)
        self.transcript.append_turn_superseded(
            self.session_state.session_id,
            {
                "superseded_turn_id": turn_id,
                "superseded_message_id": old_message.get("message_id", ""),
                "superseded_revision_id": old_revision_id,
                "replacement_turn_id": new_task.task_id,
                "replacement_revision_id": new_task.revision_id,
            },
        )
        self.persist_session()
        return self.chat(
            normalized_prompt,
            on_token=on_token,
            on_tool=on_tool,
            approval_handler=approval_handler,
            image_parts=image_parts,
            attachments=attachments,
            raw_user_prompt=original_prompt,
        )

    def approve_pending(
        self,
        approved: bool,
        on_tool=None,
        on_token=None,
        approval_handler=None,
        grant_scope: bool = False,
    ) -> str:
        if self.task_state is None or self.task_state.pending_approval is None:
            return "No pending approval."

        with self._turn_trace(
            name="agent.approve_pending",
            input_payload={
                "approved": approved,
                "grant_scope": grant_scope,
                "pending_tool": self.task_state.pending_approval.tool_name,
            },
            tags=["autocode", "approval"],
            continue_turn=True,
        ) as observation:
            try:
                pending = self.task_state.pending_approval
                remaining_tool_calls = self._deserialize_tool_calls(pending.remaining_tool_calls)
                self.task_state.clear_pending()
                if grant_scope and approved:
                    self.task_state.grant_approval_scope(pending.approval_scope)

                tool_call = ToolCall(
                    id=pending.tool_call_id,
                    name=pending.tool_name,
                    arguments=pending.arguments,
                )

                if approved:
                    result = self._execute_tool_call(
                        tool_call,
                        on_tool=on_tool,
                        decision=PolicyDecision(
                            "confirm",
                            pending.reason,
                            requires_manual=pending.requires_manual,
                            approval_scope=pending.approval_scope,
                            approval_label=pending.approval_label,
                        ),
                    )
                else:
                    result = self.runtime.blocked_result(tool_call.name, PolicyDecision("deny", "approval denied by user"))
                result_text = result.text if isinstance(result, ToolResult) else result
                self.task_state.note_tool_result(tool_call.name, result_text)
                self.hooks.emit("approval_resolved", self._event_payload(tool_name=tool_call.name, approved=approved))

                self._append_tool_result(tool_call, result)
                self._maybe_compress_messages()
                self.persist_session()

                if remaining_tool_calls:
                    wait = self._handle_tool_calls(remaining_tool_calls, on_tool=on_tool, approval_handler=approval_handler)
                    self._maybe_compress_messages()
                    self.persist_session()
                    if wait is not None:
                        response = wait
                    else:
                        response = self._continue_loop(on_token=on_token, on_tool=on_tool, approval_handler=approval_handler)
                else:
                    response = self._continue_loop(on_token=on_token, on_tool=on_tool, approval_handler=approval_handler)
            except Exception as exc:
                self._record_turn_error(observation, exc)
                raise
            self._finalize_turn_trace(observation, response)
            return response

    def restore_session(self, session_state: SessionState, messages: list[dict], model: str | None = None):
        self.session_state = session_state
        messages = [
            message
            for message in messages
            if not is_internal_visual_context(message.get("content"))
        ]
        if session_state.current_task and not session_state.current_task.revision_id:
            session_state.current_task.revision_id = new_revision_id()
        prompt_indices = []
        for index, message in enumerate(messages):
            if message.get("role") != "user":
                continue
            kind = message.get("message_kind", "")
            raw_text = content_text(message.get("content", ""))
            if kind == "synthetic" or raw_text.startswith("Visual content loaded by tools:"):
                continue
            prompt_indices.append(index)
        last_prompt_index = prompt_indices[-1] if prompt_indices else None
        self.messages = []
        current_turn_id = ""
        current_revision_id = ""
        for index, message in enumerate(messages):
            stored = dict(message)
            stored.setdefault("message_id", new_message_id())
            if "message_kind" not in stored:
                content = content_text(stored.get("content", ""))
                stored["message_kind"] = (
                    "synthetic"
                    if stored.get("role") == "user" and content.startswith("Visual content loaded by tools:")
                    else stored.get("role", "message")
                )
            if stored.get("role") == "user" and stored.get("message_kind") == "user":
                stored["message_kind"] = "prompt"
            if stored.get("role") == "user" and stored.get("message_kind") == "prompt":
                use_current_task = index == last_prompt_index and session_state.current_task is not None
                current_turn_id = str(stored.get("turn_id") or (
                    session_state.current_task.task_id if use_current_task else new_task_id()
                ))
                current_revision_id = str(stored.get("revision_id") or (
                    session_state.current_task.revision_id if use_current_task else new_revision_id()
                ))
            stored["turn_id"] = str(stored.get("turn_id") or current_turn_id)
            stored["revision_id"] = str(stored.get("revision_id") or current_revision_id)
            self.messages.append(stored)
        self._last_prompt_tokens = max(0, session_state.context_used_tokens)
        self.turn_controller.restore_queued(session_state.queued_inputs)
        task = session_state.current_task
        if task is not None and task.status in {"running", "waiting_approval"}:
            self.turn_controller.start_turn(task.task_id)
        if model:
            self.llm.model = model

    def _continue_loop(self, on_token=None, on_tool=None, approval_handler=None) -> str:
        if self.task_state is None:
            self._ensure_task()

        for _ in range(self.max_rounds):
            self._append_pending_steer()
            full_messages = self._request_messages()
            tool_schemas = self._tool_schemas()
            resp = self.runtime.call_llm(
                llm=self.llm,
                messages=full_messages,
                tools=tool_schemas,
                task_state=self.task_state,
                session_id=self.session_state.session_id,
                on_token=on_token,
            )
            self._last_prompt_tokens = resp.prompt_tokens
            self.session_state.context_used_tokens = max(0, resp.prompt_tokens)

            if not resp.tool_calls:
                self._append_message(resp.message)
                steer_items, finished = self.turn_controller.drain_steer_or_finish(
                    self.task_state.task_id
                )
                if steer_items:
                    self._append_steer_items(steer_items)
                    self.persist_session()
                    continue
                if not finished:
                    raise RuntimeError("Turn controller did not finish an idle turn.")
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
        self.turn_controller.finish_turn(self.task_state.task_id)
        return summary

    def _append_pending_steer(self) -> bool:
        if self.task_state is None:
            return False
        items = self.turn_controller.drain_steer(self.task_state.task_id)
        self._append_steer_items(items)
        return bool(items)

    def _append_steer_items(self, items) -> None:
        for item in items:
            self._append_message(
                {
                    "role": "user",
                    "content": item.content,
                    "message_id": item.message_id,
                    "message_kind": "steer",
                    "raw_prompt": item.content,
                }
            )
            self.hooks.emit(
                "user_message",
                self._event_payload(content_preview=item.content[:200], message_kind="steer"),
            )

    def _handle_tool_calls(self, tool_calls, on_tool=None, approval_handler=None) -> str | None:
        decisions = []
        invalid_results: dict[str, str] = {}
        for tool_call in tool_calls:
            if tool_call.parse_error:
                invalid_results[tool_call.id] = self.runtime.invalid_tool_call_result(
                    self.task_state,
                    tool_call,
                    self.session_state.session_id,
                )
                decisions.append(None)
                continue
            decisions.append(
                self.runtime.evaluate_tool_call(self.task_state, tool_call, self.session_state.session_id)
            )

        decisions = [
            (
                PolicyDecision("allow", decision.reason)
                if decision is not None and self._is_scope_approved(decision)
                else decision
            )
            for decision in decisions
        ]

        if len(tool_calls) > 1 and decisions and all(
            decision is not None and decision.action == "allow" for decision in decisions
        ):
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
                self._append_tool_result(tool_call, result)
            return None

        for index, (tool_call, decision) in enumerate(zip(tool_calls, decisions)):
            if decision is None:
                result = invalid_results[tool_call.id]
            elif decision.action == "allow":
                result = self._execute_tool_call(tool_call, on_tool=on_tool)
            elif decision.action == "deny":
                result = self.runtime.blocked_result(tool_call.name, decision)
            else:
                self.task_state.mark_waiting(
                    PendingApproval(
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.name,
                        arguments=tool_call.arguments,
                        reason=decision.reason,
                        requires_manual=decision.requires_manual,
                        approval_scope=decision.approval_scope,
                        approval_label=decision.approval_label,
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
                grant_scope = approval_response == "approve_scope"
                approved = approval_response in {True, "approve", "approve_scope"}
                self.task_state.clear_pending()
                if grant_scope and approved:
                    self.task_state.grant_approval_scope(decision.approval_scope)

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

            result_text = result.text if isinstance(result, ToolResult) else result
            self.task_state.note_tool_result(tool_call.name, result_text)
            self._append_tool_result(tool_call, result)

        return None

    def _execute_tool_call(
        self,
        tool_call: ToolCall,
        on_tool=None,
        decision: PolicyDecision | None = None,
    ) -> str | ToolResult:
        self._sync_mcp_tools()
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

    def _is_scope_approved(self, decision: PolicyDecision) -> bool:
        return bool(
            self.task_state
            and decision.action == "confirm"
            and self.task_state.has_approval_scope(decision.approval_scope)
        )

    def _summarize_round_limit(self, on_token=None) -> str:
        summary_prompt = {"role": "user", "content": self._ROUND_LIMIT_SUMMARY_PROMPT}
        messages = self._request_messages() + [summary_prompt]
        try:
            resp = self.runtime.call_llm(
                llm=self.llm,
                messages=messages,
                tools=[],
                task_state=self.task_state,
                session_id=self.session_state.session_id,
                on_token=on_token,
            )
            return resp.content or "已完成\n- 已达到本轮最大工具调用次数。\n\n当前卡点\n- 未能生成有效总结。\n\n建议下一步\n- 如需继续，请回复“继续”。"
        except Exception:
            return "已完成\n- 已达到本轮最大工具调用次数。\n\n当前卡点\n- 运行时在收尾总结阶段失败。\n\n建议下一步\n- 如需继续，请回复“继续”。"

    def reset(self):
        self.close(shutdown_observability=False)
        self.messages.clear()
        self.session_state = None

    def close(self, *, shutdown_observability: bool = True):
        try:
            self.processes.cleanup_all(include_persistent=True)
        except Exception:
            pass
        finally:
            if self._owns_mcp_manager and self.mcp_manager is not None:
                try:
                    self.mcp_manager.close()
                except Exception:
                    pass
            for tool in self.tools:
                try:
                    tool.close()
                except Exception:
                    continue
            self.memory.close()
            if shutdown_observability:
                tracer = getattr(self.llm, "tracer", None)
                if tracer is not None:
                    tracer.shutdown()
