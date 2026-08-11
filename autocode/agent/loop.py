"""Core agent loop."""

import hashlib
import json
import uuid
from contextlib import contextmanager

from ..context import ContextManager, MemoryManager, estimate_tokens, static_system_prompt
from ..infra import BackgroundProcessManager, Sandbox, WorkspaceFS
from ..llm import LLM, ToolCall, is_retryable_llm_error
from ..message_content import content_text, is_internal_visual_context, user_content
from ..message_projection import serialize_anthropic_messages, serialize_chat_completions
from ..runtime import HookBus, Policy, RecoveryManager, Runtime, StreamingToolExecutor
from ..skills import SkillError, SkillManager
from ..state import (
    AuditLogger,
    PendingApproval,
    PendingToolBatch,
    PolicyDecision,
    SessionState,
    SessionStore,
    TurnState,
    TranscriptLogger,
    TraceRecorder,
    TurnController,
    new_message_id,
    new_revision_id,
    new_session_id,
    new_turn_id,
    save_checkpoint,
    save_turn_queue,
)
from ..tools import ALL_TOOLS, build_tool_registry
from ..tools.agent import AgentTool
from ..tools.base import Tool, ToolResult
from ..tools.edit import EditFileTool
from ..tools.file_state import FileReadTracker
from ..tools.memory import MemoryTool
from ..tools.read import ReadTool
from ..tools.write import WriteFileTool
from ..tools.skill import SkillTool
from ..tools.todo_write import TodoWriteTool
from .model_step import PromptSnapshot, new_model_step_id


class Agent:
    _INTERRUPTED_TOOL_RESULT = (
        "Placeholder tool result: execution was interrupted by the user. "
        "If this tool is still needed, call it again."
    )
    _ROUND_LIMIT_SUMMARY_PROMPT = (
        "You have reached the maximum tool-call rounds for this turn. "
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
        max_output_tokens: int = 0,
        max_rounds: int = 200,
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
        self._prompt_tool_registry: dict[str, Tool] = {}
        self._prompt_snapshot: PromptSnapshot | None = None
        self.messages: list[dict] = []
        self.context = ContextManager(
            max_tokens=max_context_tokens,
            output_reserve_tokens=max_output_tokens,
        )
        self._last_prompt_tokens = 0
        self._last_prompt_message_count = 0
        self._last_prompt_digest = ""
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
            "turn_status",
            "turn_error",
            "todo_updated",
            "model_step_started",
            "model_step_committed",
            "model_step_tombstone",
            "tool_discarded",
        ):
            self.hooks.on(event, self.audit.handle)
            self.hooks.on(event, self.trace.handle)
        self.hooks.on("turn_status", self._handle_lifecycle_event)
        self.hooks.on("turn_error", self._handle_lifecycle_event)
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
    def turn_state(self) -> TurnState | None:
        if self.session_state is None:
            return None
        return self.session_state.current_turn

    def context_usage(self) -> dict:
        """Return the current conversation-window occupancy, not cumulative billing tokens."""
        used_tokens = self._estimated_context_tokens()
        window_tokens = max(1, int(self.context.max_tokens))
        used_tokens = min(used_tokens, window_tokens)
        return {
            "used_tokens": used_tokens,
            "window_tokens": window_tokens,
            "remaining_tokens": max(0, window_tokens - used_tokens),
            "used_percent": round(used_tokens / window_tokens * 100, 1),
        }

    @staticmethod
    def _messages_digest(messages: list[dict]) -> str:
        model_fields = {
            "system": ("role", "content", "name"),
            "user": ("role", "content", "name", "model_content"),
            "assistant": ("role", "content", "name", "tool_calls"),
            "tool": (
                "role",
                "content",
                "tool_call_id",
                "tool_name",
                "model_content",
            ),
        }
        projected = []
        for message in messages:
            allowed = model_fields.get(str(message.get("role") or ""))
            if allowed is None:
                continue
            projected.append({
                key: message[key]
                for key in allowed
                if key in message
            })
        payload = json.dumps(
            projected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _record_prompt_usage(self, prompt_tokens: int) -> None:
        tokens = max(0, int(prompt_tokens or 0))
        message_count = len(self.messages)
        digest = self._messages_digest(self.messages)
        self._last_prompt_tokens = tokens
        self._last_prompt_message_count = message_count
        self._last_prompt_digest = digest
        if self.session_state is not None:
            self.session_state.context_used_tokens = tokens
            self.session_state.context_anchor_messages = message_count
            self.session_state.context_anchor_digest = digest

    def _valid_last_prompt_tokens(self) -> int:
        if (
            self._last_prompt_tokens <= 0
            or self._last_prompt_message_count <= 0
            or not self._last_prompt_digest
            or len(self.messages) < self._last_prompt_message_count
        ):
            return 0
        anchored_messages = self.messages[:self._last_prompt_message_count]
        if self._messages_digest(anchored_messages) != self._last_prompt_digest:
            return 0
        return self._last_prompt_tokens

    def _estimated_context_tokens(self) -> int:
        """Combine the last authoritative input usage with locally appended history."""
        prompt_tokens = self._valid_last_prompt_tokens()
        if prompt_tokens <= 0:
            return estimate_tokens(self.messages)
        # 上游 usage 只覆盖锚点请求；模型回复、工具结果和后续用户输入需在本地补算。
        trailing_messages = self.messages[self._last_prompt_message_count:]
        return prompt_tokens + estimate_tokens(trailing_messages)

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
        if isinstance(tool, MemoryTool):
            tool._memory_manager = self.memory
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

    def _request_messages(self, snapshot: PromptSnapshot | None = None) -> list[dict]:
        prompt_snapshot = snapshot or self._ensure_prompt_snapshot()
        if getattr(self.llm, "api_format", "chat_completions") == "messages":
            return serialize_anthropic_messages(
                prompt_snapshot.system_prompt,
                self.messages,
            )
        return serialize_chat_completions(
            prompt_snapshot.system_prompt,
            self.messages,
        )

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

    def _tool_schemas(self, snapshot: PromptSnapshot | None = None) -> list[dict]:
        prompt_snapshot = snapshot or self._ensure_prompt_snapshot()
        return [dict(schema) for schema in prompt_snapshot.tool_schemas]

    def _ensure_session(self) -> SessionState:
        if self.session_state is None:
            self.session_state = SessionState(session_id=new_session_id())
        return self.session_state

    def _event_payload(self, **extra) -> dict:
        payload = {}
        if self.session_state is not None:
            payload["session_id"] = self.session_state.session_id
        if self.turn_state is not None:
            payload["turn_id"] = self.turn_state.turn_id
            payload["revision_id"] = self.turn_state.revision_id
            payload["turn_title"] = self.turn_state.title
        payload.update(extra)
        return payload

    def _build_static_system_prompt(self) -> str:
        return static_system_prompt(
            self.tools,
            cwd=str(self.fs.workspace_root),
            rules_block=self.memory.build_rules_block(),
            skills_block=self.skills.catalog_block() if "skill" in self.tool_registry else "",
        )

    def _create_prompt_snapshot(self, query: str = "") -> PromptSnapshot:
        if self.turn_state is None:
            raise RuntimeError("Cannot create PromptSnapshot without an active turn.")
        self._sync_mcp_tools()
        self.memory.wait_for_pending_refresh()
        memory_block = self.memory.build_project_memory_block(query=query)
        system_prompt = self._build_static_system_prompt()
        if memory_block:
            system_prompt += (
                "\n\n# Retrieved Project Memory\n"
                f"{memory_block}\n\n"
                "These are fallible, file-grounded facts. If current code or tests conflict "
                "with a fact, trust current evidence and do not repeat the stale fact."
            )
        snapshot = PromptSnapshot.create(
            turn_id=self.turn_state.turn_id,
            system_prompt=system_prompt,
            tool_schemas=[tool.schema() for tool in self.tools],
            tool_names=[tool.name for tool in self.tools],
        )
        self._prompt_snapshot = snapshot
        self._prompt_tool_registry = {
            name: self.tool_registry[name]
            for name in snapshot.tool_names
            if name in self.tool_registry
        }
        self.runtime.tool_registry = self._prompt_tool_registry
        self.turn_state.prompt_snapshot = snapshot.to_dict()
        return snapshot

    def _ensure_prompt_snapshot(self, query: str = "") -> PromptSnapshot:
        turn = self.turn_state
        if turn is None:
            self._ensure_turn(query)
            turn = self.turn_state
        if turn is None:
            raise RuntimeError("Cannot load PromptSnapshot without an active turn.")
        if (
            self._prompt_snapshot is not None
            and self._prompt_snapshot.turn_id == turn.turn_id
        ):
            return self._prompt_snapshot
        if turn.prompt_snapshot:
            snapshot = PromptSnapshot.from_dict(turn.prompt_snapshot)
            if snapshot.turn_id != turn.turn_id:
                raise ValueError("Persisted PromptSnapshot belongs to another turn.")
            self._sync_mcp_tools()
            self._prompt_snapshot = snapshot
            self._prompt_tool_registry = {
                name: self.tool_registry[name]
                for name in snapshot.tool_names
                if name in self.tool_registry
            }
            self.runtime.tool_registry = self._prompt_tool_registry
            return snapshot
        return self._create_prompt_snapshot(query=query)

    def _handle_lifecycle_event(self, event: str, payload: dict):
        turn_id = payload.get("turn_id", "")
        try:
            if event == "turn_status" and payload.get("status") == "completed":
                self.processes.cleanup_turn_processes(turn_id)
            elif event == "turn_error":
                self.processes.cleanup_turn_processes(turn_id)
        except Exception:
            return

    def _ensure_turn(self, title: str | None = None):
        session = self._ensure_session()
        if session.current_turn is None or session.current_turn.status in {"completed", "failed"}:
            title_lines = (title or "").splitlines()
            session.set_current_turn(
                TurnState(
                    turn_id=new_turn_id(),
                    revision_id=new_revision_id(),
                    title=(title_lines[0] if title_lines else "")[:120],
                    status="running",
                )
            )
            self._prompt_snapshot = None
            self._prompt_tool_registry = {}
            return
        session.current_turn.touch("running")
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

    def persist_turn(self):
        self.persist_session()

    def _append_message(self, message: dict):
        stored = dict(message)
        stored.setdefault("message_id", new_message_id())
        if self.turn_state is not None:
            stored.setdefault("turn_id", self.turn_state.turn_id)
            stored.setdefault("revision_id", self.turn_state.revision_id)
        stored.setdefault("message_kind", stored.get("role", "message"))
        self.messages.append(stored)
        if self.session_state is not None:
            self.transcript.append_message(self.session_state.session_id, stored)

    def _turn_messages(self, turn_id: str) -> list[dict]:
        return [message for message in self.messages if message.get("turn_id") == turn_id]

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
        effective_used = self._estimated_context_tokens()
        messages_before_compression = [dict(message) for message in self.messages]
        result = self.context.maybe_compress(
            self.messages,
            self.llm,
            last_prompt_tokens=effective_used,
        )
        if result.compressed and hasattr(self.llm, "_call_with_retry"):
            self.memory.schedule_project_memory_refresh(
                messages_before_compression,
                self.llm,
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
            turn_status=self.turn_state.status if self.turn_state else None,
            step_index=self.turn_state.step_index if self.turn_state else None,
        )
        turn = self.turn_state
        trace_context = None
        # 审批来自后续请求；复用已持久化的根观测，避免同一 turn 被拆成多条 trace。
        if continue_turn and turn is not None and turn.langfuse_trace_id:
            trace_context = {"trace_id": turn.langfuse_trace_id}
            if turn.langfuse_root_observation_id:
                trace_context["parent_span_id"] = turn.langfuse_root_observation_id

        with tracer.start_agent_turn(
            name=name,
            input_payload=input_payload,
            session_id=self.session_state.session_id,
            trace_name="autocode-agent-turn",
            metadata=metadata,
            tags=tags,
            trace_context=trace_context,
        ) as observation:
            if turn is not None and not turn.langfuse_trace_id:
                turn.langfuse_trace_id = str(getattr(observation, "trace_id", "") or "")
                turn.langfuse_root_observation_id = str(getattr(observation, "id", "") or "")
            yield observation

    def _finalize_turn_trace(self, observation, response_text: str):
        if observation is None:
            return
        batch = self.turn_state.pending_tool_batch if self.turn_state else None
        pending = batch.unresolved()[0] if batch and batch.unresolved() else None
        observation.update(
            output={
                "text": response_text,
                "status": self.turn_state.status if self.turn_state else None,
                "pending_tool": pending.tool_name if pending else None,
                "pending_reason": pending.reason if pending else None,
            },
            metadata=self._event_payload(
                workspace_root=str(self.fs.workspace_root),
                llm_backend=type(self.llm).__name__,
                permission_mode=self.policy.permission_mode,
                turn_status=self.turn_state.status if self.turn_state else None,
                step_index=self.turn_state.step_index if self.turn_state else None,
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
                turn_status=self.turn_state.status if self.turn_state else None,
            ),
            level="ERROR",
            status_message=str(exc),
        )

    @contextmanager
    def _agent_step_trace(self, step_index: int, snapshot: PromptSnapshot, *, phase: str = "execute"):
        tracer = getattr(self.llm, "tracer", None)
        if tracer is None or not tracer.enabled:
            yield None, None
            return

        metadata = self._event_payload(
            step_index=step_index,
            phase=phase,
            turn_status=self.turn_state.status if self.turn_state else None,
        )
        with tracer.start_agent_step(
            name=f"agent.step.{step_index}",
            input_payload={
                "step_index": step_index,
                "phase": phase,
                "prompt_snapshot_digest": snapshot.digest,
            },
            metadata=metadata,
        ) as observation:
            trace_id = str(getattr(observation, "trace_id", "") or "")
            observation_id = str(getattr(observation, "id", "") or "")
            trace_context = (
                {"trace_id": trace_id, "parent_span_id": observation_id}
                if trace_id and observation_id
                else None
            )
            yield observation, trace_context

    def _finalize_step_trace(self, observation, *, status: str, **output):
        if observation is None:
            return
        observation.update(
            output={"status": status, **output},
            metadata=self._event_payload(
                step_index=self.turn_state.step_index if self.turn_state else None,
                turn_status=self.turn_state.status if self.turn_state else None,
            ),
        )

    def _record_step_error(self, observation, exc: Exception):
        if observation is None:
            return
        observation.update(
            output={"status": "failed", "error": str(exc)},
            metadata=self._event_payload(
                step_index=self.turn_state.step_index if self.turn_state else None,
                turn_status=self.turn_state.status if self.turn_state else None,
                error_type=type(exc).__name__,
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
        if self.turn_state and self.turn_state.pending_tool_batch:
            pending_items = self.turn_state.pending_tool_batch.unresolved()
            pending = pending_items[0] if pending_items else None
            return (
                f"(turn {self.turn_state.turn_id} is waiting for approval: "
                f"{pending.tool_name if pending else 'tool batch'} - "
                "resolve the pending approvals first)"
            )

        original_prompt = raw_user_prompt if raw_user_prompt is not None else user_input
        session = self._ensure_session()
        if not session.title:
            session.title = (user_input.strip().splitlines() or ["上传文件会话"])[0][:120]
        self._ensure_turn(original_prompt)
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
        turn_id = self.turn_state.turn_id
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
                self._create_prompt_snapshot(query=original_prompt)
                self.persist_session()
                response = self._continue_loop(on_token=on_token, on_tool=on_tool, approval_handler=approval_handler)
            except Exception as exc:
                self.turn_controller.finish_turn(turn_id)
                if self.turn_state is not None:
                    self.turn_state.mark_failed(str(exc))
                    self.hooks.emit(
                        "turn_error",
                        self._event_payload(
                            status=self.turn_state.status,
                            error=self.turn_state.last_error,
                        ),
                    )
                    self.persist_session()
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
        """Supersede the last finished turn while leaving workspace files untouched."""
        normalized_prompt = prompt.strip()
        original_prompt = raw_user_prompt if raw_user_prompt is not None else normalized_prompt
        if not normalized_prompt:
            raise ValueError("Edited prompt is required.")
        turn = self.turn_state
        if turn is None or turn.status not in {"completed", "failed"}:
            raise ValueError("Only the last finished turn can be edited.")
        if turn.turn_id != turn_id:
            raise ValueError(f"Turn '{turn_id}' is not the last finished turn.")

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
        old_revision_id = turn.revision_id
        new_turn = TurnState(
            turn_id=new_turn_id(),
            revision_id=new_revision_id(),
            parent_revision_id=old_revision_id,
            supersedes_turn_id=turn_id,
            title=original_prompt.strip().splitlines()[0][:120],
            status="running",
        )
        self.messages = self.messages[:prompt_index]
        self.session_state.set_current_turn(new_turn)
        self._prompt_snapshot = None
        self._prompt_tool_registry = {}
        self.transcript.append_turn_superseded(
            self.session_state.session_id,
            {
                "superseded_turn_id": turn_id,
                "superseded_message_id": old_message.get("message_id", ""),
                "superseded_revision_id": old_revision_id,
                "replacement_turn_id": new_turn.turn_id,
                "replacement_revision_id": new_turn.revision_id,
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
        batch = self.turn_state.pending_tool_batch if self.turn_state else None
        if batch is None or not batch.unresolved():
            return "No pending approval."
        pending = batch.unresolved()[0]
        action = "approve_scope" if approved and grant_scope else ("approve" if approved else "reject")
        snapshot = self.decide_approval(
            pending.approval_id,
            action,
            expected_turn_id=batch.turn_id,
            expected_batch_id=batch.batch_id,
        )
        if not snapshot["ready"]:
            return f"(waiting for approval: {snapshot['unresolved_count']} tool call(s))"
        return self.continue_pending_batch(
            expected_turn_id=batch.turn_id,
            expected_batch_id=batch.batch_id,
            on_tool=on_tool,
            on_token=on_token,
            approval_handler=approval_handler,
        )

    def decide_approval(
        self,
        approval_id: str,
        action: str,
        *,
        expected_turn_id: str,
        expected_batch_id: str,
    ) -> dict:
        turn = self.turn_state
        batch = turn.pending_tool_batch if turn else None
        if turn is None or batch is None:
            raise ValueError("No pending approval batch.")
        if turn.turn_id != expected_turn_id or batch.turn_id != expected_turn_id:
            raise ValueError("The active turn changed before this approval was applied.")
        if batch.batch_id != expected_batch_id:
            raise ValueError("The approval batch is stale.")
        if action not in {"approve", "approve_scope", "reject"}:
            raise ValueError(f"Unsupported approval action: {action}")

        pending = next(
            (item for item in batch.approvals if item.approval_id == approval_id),
            None,
        )
        if pending is None:
            raise ValueError(f"Approval '{approval_id}' was not found.")
        decision = "rejected" if action == "reject" else "approved"
        if pending.decision != "pending":
            if pending.decision != decision:
                raise ValueError("This approval was already resolved with a different decision.")
            return self._approval_batch_snapshot(batch)

        targets = [pending]
        if action == "approve_scope" and pending.approval_scope:
            turn.grant_approval_scope(pending.approval_scope)
            targets = [
                item
                for item in batch.approvals
                if item.decision == "pending"
                and item.approval_scope == pending.approval_scope
            ]
        for item in targets:
            item.decision = decision
            self.hooks.emit(
                "approval_resolved",
                self._event_payload(
                    approval_id=item.approval_id,
                    tool_call_id=item.tool_call_id,
                    tool_name=item.tool_name,
                    approved=decision == "approved",
                    grant_scope=action == "approve_scope",
                ),
            )
        if batch.is_ready():
            batch.state = "ready"
        turn.touch("waiting_approval")
        self.persist_session()
        return self._approval_batch_snapshot(batch)

    def continue_pending_batch(
        self,
        *,
        expected_turn_id: str,
        expected_batch_id: str,
        on_tool=None,
        on_token=None,
        approval_handler=None,
    ) -> str:
        turn = self.turn_state
        batch = turn.pending_tool_batch if turn else None
        if turn is None or batch is None:
            raise ValueError("No pending approval batch.")
        if turn.turn_id != expected_turn_id or batch.turn_id != expected_turn_id:
            raise ValueError("The active turn changed before continuation.")
        if batch.batch_id != expected_batch_id:
            raise ValueError("The approval batch is stale.")
        if not batch.is_ready():
            raise ValueError("All approvals must be resolved before continuing.")
        self._ensure_prompt_snapshot(turn.title)

        with self._turn_trace(
            name="agent.continue_pending_batch",
            input_payload={
                "turn_id": batch.turn_id,
                "batch_id": batch.batch_id,
                "approval_count": len(batch.approvals),
            },
            tags=["autocode", "approval", "continuation"],
            continue_turn=True,
        ) as observation:
            try:
                batch.state = "running"
                turn.touch("running")
                self.persist_session()
                snapshot = self._ensure_prompt_snapshot(turn.title)
                with self._agent_step_trace(
                    max(1, turn.step_index),
                    snapshot,
                    phase="approval_resume",
                ) as (step_observation, trace_context):
                    try:
                        wait = self._execute_pending_batch(
                            batch,
                            on_tool=on_tool,
                            tool_trace_context=trace_context,
                        )
                    except Exception as exc:
                        self._record_step_error(step_observation, exc)
                        raise
                    self._finalize_step_trace(
                        step_observation,
                        status="waiting_approval" if wait is not None else "resumed",
                        tool_calls=len(batch.tool_calls),
                    )
                if wait is not None:
                    response = wait
                else:
                    response = self._continue_loop(
                        on_token=on_token,
                        on_tool=on_tool,
                        approval_handler=approval_handler,
                    )
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
        if session_state.current_turn and not session_state.current_turn.revision_id:
            session_state.current_turn.revision_id = new_revision_id()
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
                use_current_turn = index == last_prompt_index and session_state.current_turn is not None
                current_turn_id = str(stored.get("turn_id") or (
                    session_state.current_turn.turn_id if use_current_turn else new_turn_id()
                ))
                current_revision_id = str(stored.get("revision_id") or (
                    session_state.current_turn.revision_id if use_current_turn else new_revision_id()
                ))
            stored["turn_id"] = str(stored.get("turn_id") or current_turn_id)
            stored["revision_id"] = str(stored.get("revision_id") or current_revision_id)
            self.messages.append(stored)
        self._last_prompt_tokens = max(0, session_state.context_used_tokens)
        self._last_prompt_message_count = max(0, session_state.context_anchor_messages)
        self._last_prompt_digest = session_state.context_anchor_digest
        self.turn_controller.restore_queued(session_state.queued_inputs)
        turn = session_state.current_turn
        if turn is not None and turn.status in {"running", "waiting_approval"}:
            self.turn_controller.start_turn(turn.turn_id)
        if model:
            self.llm.model = model
        self._prompt_snapshot = None
        self._prompt_tool_registry = {}
        if turn is not None and turn.prompt_snapshot:
            self._ensure_prompt_snapshot(turn.title)

    def _sample_model_step(
        self,
        *,
        snapshot: PromptSnapshot,
        on_token=None,
        on_tool=None,
        max_attempts: int = 2,
        tool_trace_context: dict[str, str] | None = None,
    ):
        """Run a retryable model-step transaction against one immutable prompt snapshot."""
        for attempt in range(1, max_attempts + 1):
            step_id = new_model_step_id()
            executor = (
                StreamingToolExecutor(
                    runtime=self.runtime,
                    turn_state=self.turn_state,
                    session_id=self.session_state.session_id,
                    on_tool=on_tool,
                    trace_context=tool_trace_context,
                )
                if getattr(self.llm, "supports_streaming_tool_calls", False)
                else None
            )
            visible_chars = 0
            started_payload = self._event_payload(
                model_step_id=step_id,
                attempt=attempt,
                prompt_snapshot_digest=snapshot.digest,
            )
            self.hooks.emit("model_step_started", started_payload)
            self.transcript.append_model_step(
                self.session_state.session_id,
                "model_step_started",
                started_payload,
            )

            def provisional_token(text: str) -> None:
                nonlocal visible_chars
                visible_chars += len(text)
                if on_token:
                    on_token(text)

            def streamed_tool_call(tool_call: ToolCall) -> None:
                if executor is not None:
                    executor.add_tool(tool_call)

            try:
                resp = self.runtime.call_llm(
                    llm=self.llm,
                    messages=self._request_messages(snapshot),
                    tools=self._tool_schemas(snapshot),
                    turn_state=self.turn_state,
                    session_id=self.session_state.session_id,
                    on_token=provisional_token,
                    on_tool_call=streamed_tool_call,
                )
            except Exception as exc:
                discarded_tool_call_ids = executor.discard() if executor is not None else []
                tombstone_payload = self._event_payload(
                    model_step_id=step_id,
                    attempt=attempt,
                    visible_chars=visible_chars,
                    discarded_tool_call_ids=discarded_tool_call_ids,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                self.hooks.emit("model_step_tombstone", tombstone_payload)
                self.transcript.append_model_step(
                    self.session_state.session_id,
                    "model_step_tombstone",
                    tombstone_payload,
                )
                for tool_call_id in discarded_tool_call_ids:
                    self.hooks.emit(
                        "tool_discarded",
                        self._event_payload(
                            model_step_id=step_id,
                            tool_call_id=tool_call_id,
                            reason="model step rolled back",
                        ),
                    )
                if attempt < max_attempts and is_retryable_llm_error(exc):
                    continue
                raise

            committed_payload = self._event_payload(
                model_step_id=step_id,
                attempt=attempt,
                visible_chars=visible_chars,
                tool_calls=len(resp.tool_calls),
                prompt_snapshot_digest=snapshot.digest,
            )
            self.hooks.emit("model_step_committed", committed_payload)
            self.transcript.append_model_step(
                self.session_state.session_id,
                "model_step_committed",
                committed_payload,
            )
            return resp, executor, {
                "attempt": attempt,
                "model_step_id": step_id,
                "tombstones": attempt - 1,
            }
        raise RuntimeError("Model step exhausted all transaction attempts.")

    def _continue_loop(self, on_token=None, on_tool=None, approval_handler=None) -> str:
        if self.turn_state is None:
            self._ensure_turn()
        snapshot = self._ensure_prompt_snapshot(self.turn_state.title)

        for _ in range(self.max_rounds):
            action, response = self._run_agent_step(
                snapshot=snapshot,
                on_token=on_token,
                on_tool=on_tool,
                approval_handler=approval_handler,
            )
            if action == "continue":
                continue
            return response

        step_index = self.turn_state.step_index + 1
        with self._agent_step_trace(step_index, snapshot, phase="round_limit_summary") as (
            observation,
            _,
        ):
            try:
                summary = self._summarize_round_limit(on_token=on_token)
            except Exception as exc:
                self._record_step_error(observation, exc)
                raise
            self._finalize_step_trace(
                observation,
                status="round_limit",
                tool_calls=0,
            )
        self._append_message({"role": "assistant", "content": summary})
        self.turn_state.mark_failed("reached maximum tool-call rounds")
        self.hooks.emit(
            "turn_error",
            self._event_payload(status=self.turn_state.status, error=self.turn_state.last_error),
        )
        self.persist_session()
        self.turn_controller.finish_turn(self.turn_state.turn_id)
        return summary

    def _run_agent_step(self, *, snapshot, on_token=None, on_tool=None, approval_handler=None):
        self._append_pending_steer()
        step_index = self.turn_state.step_index + 1
        with self._agent_step_trace(step_index, snapshot) as (observation, trace_context):
            try:
                resp, streaming_executor, transaction = self._sample_model_step(
                    snapshot=snapshot,
                    on_token=on_token,
                    on_tool=on_tool,
                    tool_trace_context=trace_context,
                )
                self._record_prompt_usage(resp.prompt_tokens)
                if resp.stop_reason in {"max_tokens", "length"}:
                    if streaming_executor is not None:
                        streaming_executor.discard()
                    raise RuntimeError(
                        "模型输出达到 token 上限，回答未完成。"
                        f"当前 AUTOCODE_MAX_TOKENS={self.context.output_reserve_tokens}，"
                        "请提高输出预算后重试。"
                    )

                if resp.tool_calls:
                    self.hooks.emit(
                        "assistant_step",
                        self._event_payload(
                            step_index=self.turn_state.step_index,
                            content=resp.content,
                            tool_calls=[
                                {
                                    "id": tool_call.id,
                                    "name": tool_call.name,
                                    "arguments": tool_call.arguments,
                                }
                                for tool_call in resp.tool_calls
                            ],
                        ),
                    )

                if not resp.tool_calls:
                    if streaming_executor is not None:
                        streaming_executor.commit([])
                    self._append_message(resp.message)
                    steer_items, finished = self.turn_controller.drain_steer_or_finish(
                        self.turn_state.turn_id
                    )
                    if steer_items:
                        self._append_steer_items(steer_items)
                        self.persist_session()
                        self._finalize_step_trace(
                            observation,
                            status="steered",
                            tool_calls=0,
                            **transaction,
                        )
                        return "continue", ""
                    if not finished:
                        raise RuntimeError("Turn controller did not finish an idle turn.")
                    self.turn_state.mark_completed()
                    self.hooks.emit("turn_status", self._event_payload(status=self.turn_state.status))
                    self.persist_session()
                    self._finalize_step_trace(
                        observation,
                        status="completed",
                        tool_calls=0,
                        stop_reason=resp.stop_reason,
                        **transaction,
                    )
                    return "return", resp.content

                self._append_message(resp.message)
                self.persist_session()
                wait = self._handle_tool_calls(
                    resp.tool_calls,
                    on_tool=on_tool,
                    approval_handler=approval_handler,
                    streaming_executor=streaming_executor,
                    tool_trace_context=trace_context,
                )
                status = "waiting_approval" if wait is not None else "committed"
                self._finalize_step_trace(
                    observation,
                    status=status,
                    tool_calls=len(resp.tool_calls),
                    stop_reason=resp.stop_reason,
                    **transaction,
                )
                if wait is not None:
                    return "return", wait

                self._maybe_compress_messages()
                self.persist_session()
                return "continue", ""
            except Exception as exc:
                self._record_step_error(observation, exc)
                raise

    def _append_pending_steer(self) -> bool:
        if self.turn_state is None:
            return False
        items = self.turn_controller.drain_steer(self.turn_state.turn_id)
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

    def _handle_tool_calls(
        self,
        tool_calls,
        on_tool=None,
        approval_handler=None,
        streaming_executor: StreamingToolExecutor | None = None,
        tool_trace_context: dict[str, str] | None = None,
    ) -> str | None:
        decisions: list[PolicyDecision | None] = []
        for tool_call in tool_calls:
            if tool_call.parse_error:
                decisions.append(None)
                continue
            streamed_decision = (
                streaming_executor.decision_for(tool_call)
                if streaming_executor is not None
                else None
            )
            decisions.append(
                streamed_decision
                or self.runtime.evaluate_tool_call(
                    self.turn_state,
                    tool_call,
                    self.session_state.session_id,
                )
            )

        decisions = [
            (
                PolicyDecision("allow", decision.reason)
                if decision is not None and self._is_scope_approved(decision)
                else decision
            )
            for decision in decisions
        ]

        approvals = [
            PendingApproval(
                approval_id=uuid.uuid4().hex,
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                arguments=tool_call.arguments,
                reason=decision.reason,
                requires_manual=decision.requires_manual,
                approval_scope=decision.approval_scope,
                approval_label=decision.approval_label,
            )
            for tool_call, decision in zip(tool_calls, decisions)
            if decision is not None and decision.action == "confirm"
        ]
        batch = PendingToolBatch(
            batch_id=uuid.uuid4().hex,
            turn_id=self.turn_state.turn_id,
            tool_calls=[self._serialize_tool_call(item) for item in tool_calls],
            policy_decisions=[
                decision.to_dict() if decision is not None else None
                for decision in decisions
            ],
            approvals=approvals,
            state="waiting" if approvals else "ready",
        )

        if approvals:
            self.turn_state.mark_waiting(batch)
            self.persist_session()
            if approval_handler is None:
                if streaming_executor is not None:
                    streaming_executor.discard()
                return f"(waiting for approval: {len(approvals)} tool call(s))"

            for approval in list(batch.approvals):
                if approval.decision != "pending":
                    continue
                approval_response = approval_handler(approval)
                action = (
                    "approve_scope"
                    if approval_response == "approve_scope"
                    else "approve"
                    if approval_response in {True, "approve"}
                    else "reject"
                )
                self.decide_approval(
                    approval.approval_id,
                    action,
                    expected_turn_id=batch.turn_id,
                    expected_batch_id=batch.batch_id,
                )

        prefetched_results = (
            streaming_executor.commit(tool_calls)
            if streaming_executor is not None
            else {}
        )
        return self._execute_pending_batch(
            batch,
            on_tool=on_tool,
            prefetched_results=prefetched_results,
            tool_trace_context=tool_trace_context,
        )

    def _execute_pending_batch(
        self,
        batch: PendingToolBatch,
        *,
        on_tool=None,
        prefetched_results: dict[str, str | ToolResult] | None = None,
        tool_trace_context: dict[str, str] | None = None,
    ) -> str | None:
        tool_calls = self._deserialize_tool_calls(batch.tool_calls)
        decision_data = list(batch.policy_decisions)
        if len(decision_data) != len(tool_calls):
            raise ValueError("Pending tool batch is corrupt.")

        decisions: list[PolicyDecision | None] = [
            PolicyDecision.from_dict(item) if item is not None else None
            for item in decision_data
        ]
        approval_by_call = {
            item.tool_call_id: item
            for item in batch.approvals
        }
        executable_calls: list[ToolCall] = []
        executable_decisions: list[PolicyDecision | None] = []
        results_by_call: dict[str, str | ToolResult] = dict(prefetched_results or {})

        for index, (tool_call, decision) in enumerate(zip(tool_calls, decisions)):
            if tool_call.parse_error:
                results_by_call[tool_call.id] = self.runtime.invalid_tool_call_result(
                    self.turn_state,
                    tool_call,
                    self.session_state.session_id,
                )
                continue
            if decision is None:
                decision = self.runtime.evaluate_tool_call(
                    self.turn_state,
                    tool_call,
                    self.session_state.session_id,
                )
                decisions[index] = decision
            if decision.action == "deny":
                results_by_call[tool_call.id] = self.runtime.blocked_result(tool_call.name, decision)
                continue
            if decision.action == "confirm":
                approval = approval_by_call.get(tool_call.id)
                if approval is None:
                    legacy_batch = PendingToolBatch(
                        batch_id=uuid.uuid4().hex,
                        turn_id=self.turn_state.turn_id,
                        tool_calls=[
                            self._serialize_tool_call(item)
                            for item in tool_calls[index:]
                        ],
                        policy_decisions=[
                            item.to_dict() if item is not None else None
                            for item in decisions[index:]
                        ],
                        approvals=[
                            PendingApproval(
                                approval_id=uuid.uuid4().hex,
                                tool_call_id=tool_call.id,
                                tool_name=tool_call.name,
                                arguments=tool_call.arguments,
                                reason=decision.reason,
                                requires_manual=decision.requires_manual,
                                approval_scope=decision.approval_scope,
                                approval_label=decision.approval_label,
                            )
                        ],
                    )
                    self.turn_state.mark_waiting(legacy_batch)
                    self.persist_session()
                    return f"(waiting for approval: {tool_call.name})"
                if approval.decision == "pending":
                    raise ValueError("Pending tool batch is not fully resolved.")
                if approval.decision == "rejected":
                    results_by_call[tool_call.id] = self.runtime.blocked_result(
                        tool_call.name,
                        PolicyDecision("deny", "approval denied by user"),
                    )
                    continue
            if tool_call.id in results_by_call:
                continue
            executable_calls.append(tool_call)
            executable_decisions.append(decision)

        if executable_calls:
            try:
                executed = self.runtime.execute_tool_calls_parallel(
                    self.turn_state,
                    executable_calls,
                    self.session_state.session_id,
                    on_tool=on_tool,
                    decisions=executable_decisions,
                    trace_context=tool_trace_context,
                )
            except KeyboardInterrupt:
                executed = [
                    self._interrupted_tool_result(tool_call)
                    for tool_call in executable_calls
                ]
            results_by_call.update(
                (tool_call.id, result)
                for tool_call, result in zip(executable_calls, executed)
            )

        for tool_call in tool_calls:
            result = results_by_call[tool_call.id]
            result_text = result.text if isinstance(result, ToolResult) else result
            self.turn_state.note_tool_result(tool_call.name, result_text)
            self._append_tool_result(tool_call, result)

        self.turn_state.clear_pending()
        self._maybe_compress_messages()
        self.persist_session()
        return None

    @staticmethod
    def _approval_batch_snapshot(batch: PendingToolBatch) -> dict:
        return {
            "batch_id": batch.batch_id,
            "turn_id": batch.turn_id,
            "state": batch.state,
            "ready": batch.is_ready(),
            "unresolved_count": len(batch.unresolved()),
            "approvals": [item.to_dict() for item in batch.approvals],
        }

    def _execute_tool_call(
        self,
        tool_call: ToolCall,
        on_tool=None,
        decision: PolicyDecision | None = None,
    ) -> str | ToolResult:
        try:
            return self.runtime.execute_tool_call(
                self.turn_state,
                tool_call,
                self.session_state.session_id,
                on_tool=on_tool,
                decision=decision,
            )
        except KeyboardInterrupt:
            return self._interrupted_tool_result(tool_call)

    def _interrupted_tool_result(self, tool_call: ToolCall) -> str:
        if self.turn_state is not None:
            self.turn_state.note_failure(f"{tool_call.name} interrupted by user")
        return self._INTERRUPTED_TOOL_RESULT

    def _is_scope_approved(self, decision: PolicyDecision) -> bool:
        return bool(
            self.turn_state
            and decision.action == "confirm"
            and self.turn_state.has_approval_scope(decision.approval_scope)
        )

    def _summarize_round_limit(self, on_token=None) -> str:
        summary_prompt = {"role": "user", "content": self._ROUND_LIMIT_SUMMARY_PROMPT}
        messages = self._request_messages() + [summary_prompt]
        try:
            resp = self.runtime.call_llm(
                llm=self.llm,
                messages=messages,
                tools=[],
                turn_state=self.turn_state,
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
        self._prompt_snapshot = None
        self._prompt_tool_registry = {}

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
