"""Channel-agnostic remote control manager."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Hashable

from ..agent import Agent
from ..attachments import prepare_attachments
from ..config import Config
from ..llm import LLM, LiteLLM
from ..message_content import content_text, is_internal_visual_context
from ..mcp import get_shared_mcp_manager
from ..observability import LangfuseTracer
from ..state import (
    delete_session,
    format_trace,
    list_sessions,
    load_checkpoint,
    load_trace,
    load_transcript_messages,
)
from ..tools.factory import build_agent_tools

_PRESENTATION_ARGUMENT_KEYS = (
    "command",
    "file_path",
    "path",
    "query",
    "url",
    "pattern",
    "task",
    "process_id",
)


def presentation_tool_arguments(arguments: object) -> dict:
    """Return bounded operation descriptors without sending full tool payloads."""
    if not isinstance(arguments, dict):
        return {}
    visible = {}
    for key in _PRESENTATION_ARGUMENT_KEYS:
        if key not in arguments:
            continue
        value = arguments[key]
        if isinstance(value, (str, int, float, bool)) or value is None:
            visible[key] = str(value)[:500] if isinstance(value, str) else value
    return visible


@dataclass
class RemoteTurnResult:
    text: str
    session_id: str = ""
    task_id: str = ""
    status: str = ""
    pending_tool: str = ""
    pending_reason: str = ""
    pending_arguments: dict | None = None
    pending_requires_manual: bool = False
    auto_approve_for_task: bool = False
    context_used_tokens: int = 0
    context_window_tokens: int = 0


@dataclass
class _ChatRuntime:
    agent: Agent
    lock: threading.RLock


class RemoteManager:
    """Owns one in-memory agent session per remote chat."""

    _HOOK_EVENTS = (
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
    )

    def __init__(self, config: Config, llm_factory=None, tools: list | None = None, tool_factory=None):
        self.config = config
        self._llm_factory = llm_factory
        self._tools = tools
        self._tool_factory = tool_factory
        self._shared_tracer = (
            None
            if llm_factory is not None
            else LangfuseTracer(
                public_key=config.langfuse_public_key,
                secret_key=config.langfuse_secret_key,
                base_url=config.langfuse_base_url,
            )
        )
        self._mcp_manager = get_shared_mcp_manager(config.workspace_root, config.mcp_config_path)
        self._mcp_manager.start_background()
        self._state_lock = threading.Lock()
        self._chats: dict[Hashable, _ChatRuntime] = {}

    def submit(
        self,
        chat_id: Hashable,
        prompt: str,
        hook_handler=None,
        attachments: list[dict] | None = None,
        on_token=None,
        on_tool=None,
    ) -> RemoteTurnResult:
        runtime = self._get_or_create_runtime(chat_id)
        with runtime.lock:
            message_start = len(runtime.agent.messages)
            started_at = time.monotonic()
            prepared = prepare_attachments(
                self.config.workspace_root,
                str(chat_id),
                prompt,
                attachments,
            )
            with self._temporary_hook(runtime.agent, hook_handler):
                reply = runtime.agent.chat(
                    prepared.prompt,
                    approval_handler=None,
                    on_token=on_token,
                    on_tool=on_tool,
                    image_parts=prepared.image_parts,
                    attachments=prepared.files,
                )
            self._annotate_turn(
                runtime.agent,
                message_start,
                round((time.monotonic() - started_at) * 1000, 1),
            )
            return self._result_from_agent(runtime.agent, reply)

    def resolve_approval(
        self,
        chat_id: Hashable,
        approved: bool,
        enable_auto_approve: bool = False,
        hook_handler=None,
    ) -> RemoteTurnResult:
        runtime = self._require_runtime(chat_id)
        with runtime.lock:
            message_start = len(runtime.agent.messages)
            started_at = time.monotonic()
            with self._temporary_hook(runtime.agent, hook_handler):
                reply = runtime.agent.approve_pending(
                    approved=approved,
                    approval_handler=None,
                    enable_auto_approve=enable_auto_approve,
                )
            self._annotate_turn(
                runtime.agent,
                message_start,
                round((time.monotonic() - started_at) * 1000, 1),
            )
            return self._result_from_agent(runtime.agent, reply)

    def reset_chat(self, chat_id: Hashable) -> None:
        with self._state_lock:
            runtime = self._chats.pop(chat_id, None)
        if runtime is not None:
            with runtime.lock:
                self._close_agent(runtime.agent)

    def close(self) -> None:
        """Close every chat runtime and the shared MCP manager."""
        with self._state_lock:
            runtimes = list(self._chats.values())
            self._chats.clear()
        for runtime in runtimes:
            with runtime.lock:
                self._close_agent(runtime.agent)
        if self._shared_tracer is not None:
            self._shared_tracer.shutdown()
        self._mcp_manager.close()

    def conversation_messages(self, chat_id: Hashable, limit: int | None = None) -> list[dict]:
        """Return a presentation-safe snapshot of the current conversation."""
        runtime = self._require_runtime(chat_id)
        with runtime.lock:
            messages = []
            tool_calls: dict[str, dict] = {}
            source_messages = self._presentation_source_messages(runtime.agent)
            if limit is not None:
                source_messages = source_messages[-max(1, limit):]
            for message in source_messages:
                role = message.get("role", "")
                raw_content = message.get("content", "")
                if role not in {"user", "assistant", "tool"}:
                    continue
                if is_internal_visual_context(raw_content):
                    continue
                safe_tool_calls = []
                if role == "assistant":
                    for item in message.get("tool_calls") or []:
                        function = item.get("function") or {}
                        tool_call_id = str(item.get("id", ""))
                        raw_arguments = function.get("arguments", "")
                        try:
                            arguments = json.loads(raw_arguments) if raw_arguments else {}
                        except (TypeError, json.JSONDecodeError):
                            arguments = {"raw": str(raw_arguments)[:2000]}
                        tool_call = {
                            "id": tool_call_id,
                            "name": str(function.get("name", "")),
                            "arguments": presentation_tool_arguments(arguments),
                        }
                        safe_tool_calls.append(tool_call)
                        if tool_call_id:
                            tool_calls[tool_call_id] = tool_call
                content = content_text(raw_content)
                tool_call_id = str(message.get("tool_call_id", ""))
                known_tool = tool_calls.get(tool_call_id, {})
                messages.append(
                    {
                        "role": role,
                        "content": content[:20_000],
                        "tool_call_id": tool_call_id,
                        "tool_name": str(
                            message.get("tool_name")
                            or known_tool.get("name")
                            or ""
                        ),
                        "tool_arguments": (
                            presentation_tool_arguments(message.get("tool_arguments"))
                            or known_tool.get("arguments")
                            or {}
                        ),
                        "tool_calls": safe_tool_calls,
                        "turn_id": str(message.get("turn_id", "")),
                        "turn_elapsed_ms": float(message.get("turn_elapsed_ms", 0) or 0),
                        "changed_files": self._presentation_changed_files(
                            message.get("changed_files")
                        ),
                        "attachments": self._presentation_attachments(
                            message.get("attachments")
                        ),
                    }
                )
            return messages

    @staticmethod
    def _presentation_source_messages(agent: Agent) -> list[dict]:
        """Use the raw transcript for UI history, retaining checkpoint metadata."""
        current = list(agent.messages)
        if agent.session_state is None:
            return current
        transcript = load_transcript_messages(agent.session_state.session_id)
        if len(transcript) <= len(current):
            return current

        metadata_by_content: dict[str, list[dict]] = {}
        for message in current:
            if message.get("role") != "user":
                continue
            key = content_text(message.get("content", ""), include_media_labels=False)
            metadata_by_content.setdefault(key, []).append(message)

        restored = [dict(message) for message in transcript]
        for message in reversed(restored):
            if message.get("role") != "user":
                continue
            key = content_text(message.get("content", ""), include_media_labels=False)
            matches = metadata_by_content.get(key)
            if not matches:
                continue
            current_message = matches.pop()
            for field in ("turn_id", "turn_elapsed_ms", "changed_files", "attachments"):
                if field in current_message:
                    message[field] = current_message[field]
        return restored

    @staticmethod
    def _presentation_attachments(items) -> list[dict]:
        safe = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            safe.append(
                {
                    "name": str(item.get("name", ""))[:180],
                    "media_type": str(item.get("media_type", "application/octet-stream"))[:120],
                    "size": max(0, int(item.get("size", 0) or 0)),
                }
            )
        return safe

    def annotate_turn_changes(self, chat_id: Hashable, changed_files: list[dict]) -> None:
        """Persist Git changes on the latest user turn for presentation clients."""
        safe_changes = self._presentation_changed_files(changed_files)
        if not safe_changes:
            return
        runtime = self._require_runtime(chat_id)
        with runtime.lock:
            user_message = next(
                (
                    message
                    for message in reversed(runtime.agent.messages)
                    if message.get("role") == "user"
                ),
                None,
            )
            if user_message is None:
                return
            merged = {
                item["path"]: item
                for item in self._presentation_changed_files(
                    user_message.get("changed_files")
                )
            }
            merged.update({item["path"]: item for item in safe_changes})
            user_message["changed_files"] = list(merged.values())[:200]
            runtime.agent.persist_session()

    @staticmethod
    def _presentation_changed_files(value: object) -> list[dict]:
        if not isinstance(value, list):
            return []
        files = []
        for item in value[:200]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).strip().replace("\\", "/")[:1000]
            if not path:
                continue
            try:
                additions = max(0, int(item.get("additions", 0) or 0))
                deletions = max(0, int(item.get("deletions", 0) or 0))
            except (TypeError, ValueError):
                additions = 0
                deletions = 0
            files.append(
                {
                    "path": path,
                    "status": str(item.get("status", "modified"))[:32],
                    "additions": additions,
                    "deletions": deletions,
                }
            )
        return files

    @staticmethod
    def _annotate_turn(agent: Agent, message_start: int, elapsed_ms: float) -> None:
        """Persist presentation metadata on the user turn without changing model content."""
        messages = agent.messages
        if not messages:
            return
        user_index = next(
            (
                index
                for index in range(min(message_start, len(messages) - 1), -1, -1)
                if messages[index].get("role") == "user"
            ),
            None,
        )
        if user_index is None:
            return
        task_id = agent.task_state.task_id if agent.task_state is not None else ""
        for message in messages[user_index:]:
            message["turn_id"] = task_id
        user_message = messages[user_index]
        user_message["turn_elapsed_ms"] = round(
            float(user_message.get("turn_elapsed_ms", 0) or 0) + elapsed_ms,
            1,
        )
        agent.persist_session()

    def observability_status(self) -> dict:
        with self._state_lock:
            runtimes = list(self._chats.values())
        if not runtimes:
            return {
                "provider": "langfuse",
                "configured": bool(
                    self.config.langfuse_public_key and self.config.langfuse_secret_key
                ),
                "enabled": False,
                "status": "not-initialized",
            }
        tracer = getattr(runtimes[0].agent.llm, "tracer", None)
        return tracer.status if tracer is not None else {
            "provider": "langfuse",
            "configured": False,
            "enabled": False,
            "status": "unavailable",
        }

    def current_task_summary(self, chat_id: Hashable) -> str:
        runtime = self._require_runtime(chat_id)
        with runtime.lock:
            agent = runtime.agent
            if agent.session_state is None:
                return "No active session."
            task = agent.task_state
            if task is None:
                return f"Session: {agent.session_state.session_id}\nCurrent Task: (none)"

            suffix = ""
            if task.pending_approval:
                suffix = f"\nPending approval: {task.pending_approval.tool_name} - {task.pending_approval.reason}"
            return (
                f"Session: {agent.session_state.session_id}\n"
                f"Task: {task.task_id}\n"
                f"Title: {task.title or '(untitled)'}\n"
                f"Status: {task.status}\n"
                f"Steps: {task.step_index}\n"
                f"Approve_all: {'on' if task.auto_approve_for_task else 'off'}{suffix}"
            )

    def current_trace(self, chat_id: Hashable) -> str:
        runtime = self._require_runtime(chat_id)
        with runtime.lock:
            agent = runtime.agent
            if agent.session_state is None:
                return "No active session."

            trace = load_trace(agent.session_state.session_id)
            if trace is None:
                return "No trace recorded yet."
            return format_trace(trace)

    def list_recent_tasks(self) -> list[dict]:
        return list_sessions()

    def list_resume_candidates(self, limit: int = 10) -> list[dict]:
        return list_sessions(workspace_root=self.config.workspace_root, limit=limit)

    def resume_session(self, chat_id: Hashable, session_id: str) -> RemoteTurnResult:
        allowed_session_ids = {
            item["session_id"]
            for item in self.list_resume_candidates(limit=200)
        }
        if session_id not in allowed_session_ids:
            raise ValueError(f"Session '{session_id}' is not available for this workspace.")
        loaded = load_checkpoint(session_id)
        if loaded is None:
            raise ValueError(f"Session '{session_id}' not found.")

        session_state, messages, _saved_model = loaded
        runtime = self._get_or_create_runtime(chat_id, replace=True)
        with runtime.lock:
            # 远程会话恢复历史上下文，但始终使用当前 Runner 配置的模型。
            runtime.agent.restore_session(session_state, messages)
            current_task = session_state.current_task
            status = current_task.status if current_task else "idle"
            return self._result_from_agent(
                runtime.agent,
                f"Resumed session {session_state.session_id} ({status}).",
            )

    def delete_session(self, session_id: str) -> None:
        allowed_session_ids = {
            item["session_id"]
            for item in self.list_resume_candidates(limit=200)
        }
        if session_id not in allowed_session_ids:
            raise ValueError(f"Session '{session_id}' is not available for this workspace.")
        with self._state_lock:
            matching = [
                (chat_id, runtime)
                for chat_id, runtime in self._chats.items()
                if runtime.agent.session_state is not None
                and runtime.agent.session_state.session_id == session_id
            ]
            for chat_id, _ in matching:
                self._chats.pop(chat_id, None)
        for _, runtime in matching:
            with runtime.lock:
                self._close_agent(runtime.agent)
        delete_session(session_id, self.config.workspace_root)

    def _get_or_create_runtime(self, chat_id: Hashable, replace: bool = False) -> _ChatRuntime:
        previous = None
        with self._state_lock:
            if not replace and chat_id in self._chats:
                return self._chats[chat_id]
            if replace:
                previous = self._chats.pop(chat_id, None)
            runtime = _ChatRuntime(agent=self._build_agent(), lock=threading.RLock())
            self._chats[chat_id] = runtime
        if previous is not None:
            with previous.lock:
                self._close_agent(previous.agent)
        return runtime

    def _close_agent(self, agent: Agent) -> None:
        if self._shared_tracer is None:
            agent.close()
            return
        agent.close(shutdown_observability=False)

    def _require_runtime(self, chat_id: Hashable) -> _ChatRuntime:
        with self._state_lock:
            runtime = self._chats.get(chat_id)
        if runtime is None:
            raise ValueError("No chat session yet. Send a task first or resume a session.")
        return runtime

    def _build_agent(self) -> Agent:
        llm = self._llm_factory() if self._llm_factory is not None else self._build_llm()
        return Agent(
            llm=llm,
            tools=self._build_tools(),
            tool_factory=self._build_tools,
            mcp_manager=self._mcp_manager,
            own_mcp_manager=False,
            max_context_tokens=self.config.max_context_tokens,
            workspace_root=self.config.workspace_root,
            auto_approve=self.config.auto_approve,
        )

    def _build_tools(self):
        if self._tool_factory is not None:
            return list(self._tool_factory())
        if self._tools is None:
            return build_agent_tools(self.config, mcp_manager=self._mcp_manager)
        built = []
        for tool in self._tools:
            built.append(tool.clone())
        built.extend(self._mcp_manager.snapshot_tools())
        return built

    def _build_llm(self):
        llm_cls = LiteLLM if self.config.provider == "litellm" else LLM
        return llm_cls(
            model=self.config.model,
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            langfuse_public_key=self.config.langfuse_public_key,
            langfuse_secret_key=self.config.langfuse_secret_key,
            langfuse_base_url=self.config.langfuse_base_url,
            tracer=self._shared_tracer,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )

    @contextmanager
    def _temporary_hook(self, agent: Agent, hook_handler):
        if hook_handler is None:
            yield
            return
        for event in self._HOOK_EVENTS:
            agent.hooks.on(event, hook_handler)
        try:
            yield
        finally:
            for event in self._HOOK_EVENTS:
                agent.hooks.off(event, hook_handler)

    @staticmethod
    def _result_from_agent(agent: Agent, text: str) -> RemoteTurnResult:
        context = agent.context_usage()
        if agent.session_state is None:
            return RemoteTurnResult(
                text=text,
                context_used_tokens=context["used_tokens"],
                context_window_tokens=context["window_tokens"],
            )
        task = agent.task_state
        if task is None:
            return RemoteTurnResult(
                text=text,
                session_id=agent.session_state.session_id,
                context_used_tokens=context["used_tokens"],
                context_window_tokens=context["window_tokens"],
            )
        pending_tool = ""
        pending_reason = ""
        pending_arguments = None
        pending_requires_manual = False
        if task.pending_approval:
            pending_tool = task.pending_approval.tool_name
            pending_reason = task.pending_approval.reason
            pending_arguments = task.pending_approval.arguments
            pending_requires_manual = task.pending_approval.requires_manual
        return RemoteTurnResult(
            text=text,
            session_id=agent.session_state.session_id,
            task_id=task.task_id,
            status=task.status,
            pending_tool=pending_tool,
            pending_reason=pending_reason,
            pending_arguments=pending_arguments,
            pending_requires_manual=pending_requires_manual,
            auto_approve_for_task=task.auto_approve_for_task,
            context_used_tokens=context["used_tokens"],
            context_window_tokens=context["window_tokens"],
        )
