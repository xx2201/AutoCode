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
from ..llm import llm_class_for_provider
from ..message_content import content_text, is_internal_visual_context
from ..mcp import get_shared_mcp_manager
from ..observability import LangfuseTracer
from ..state import (
    delete_session,
    format_trace,
    list_sessions,
    load_checkpoint,
    load_trace,
    load_transcript_entries,
    save_turn_queue,
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
    pending_approval_scope: str = ""
    pending_approval_label: str = ""
    approval_batch_id: str = ""
    pending_approvals: list[dict] | None = None
    permission_mode: str = "ask"
    context_used_tokens: int = 0
    context_window_tokens: int = 0


@dataclass
class _ChatRuntime:
    agent: Agent
    lock: threading.RLock
    approval_lock: threading.RLock


class RemoteManager:
    """Owns one in-memory agent session per remote chat."""

    _HOOK_EVENTS = (
        "turn_started",
        "before_llm",
        "after_llm",
        "assistant_step",
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
        permission_mode: str | None = None,
    ) -> RemoteTurnResult:
        runtime = self._get_or_create_runtime(chat_id)
        with runtime.lock:
            if permission_mode:
                runtime.agent.set_permission_mode(permission_mode)
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
                    raw_user_prompt=prompt,
                )
            self._annotate_turn(
                runtime.agent,
                message_start,
                round((time.monotonic() - started_at) * 1000, 1),
            )
            return self._result_from_agent(runtime.agent, reply)

    def edit_last_turn(
        self,
        chat_id: Hashable,
        turn_id: str,
        prompt: str,
        hook_handler=None,
        attachments: list[dict] | None = None,
        on_token=None,
        on_tool=None,
        permission_mode: str | None = None,
    ) -> RemoteTurnResult:
        """Edit and rerun only the last completed turn in the same session."""
        runtime = self._require_runtime(chat_id)
        with runtime.lock:
            if permission_mode:
                runtime.agent.set_permission_mode(permission_mode)
            started_at = time.monotonic()
            prepared = prepare_attachments(
                self.config.workspace_root,
                str(chat_id),
                prompt,
                attachments,
            )
            with self._temporary_hook(runtime.agent, hook_handler):
                reply = runtime.agent.edit_last_turn(
                    turn_id,
                    prepared.prompt,
                    on_token=on_token,
                    on_tool=on_tool,
                    approval_handler=None,
                    image_parts=prepared.image_parts,
                    attachments=prepared.files,
                    raw_user_prompt=prompt,
                )
            self._annotate_turn(
                runtime.agent,
                0,
                round((time.monotonic() - started_at) * 1000, 1),
            )
            return self._result_from_agent(runtime.agent, reply)

    def steer(
        self,
        chat_id: Hashable,
        expected_turn_id: str,
        prompt: str,
        message_id: str | None = None,
    ) -> dict:
        """Inject guidance into an active turn without waiting for the agent lock."""
        runtime = self._require_runtime(chat_id)
        item = runtime.agent.turn_controller.steer(
            prompt,
            expected_turn_id=expected_turn_id,
            message_id=message_id,
        )
        return {"mode": "steer", **item.to_dict()}

    def enqueue_followup(
        self,
        chat_id: Hashable,
        expected_turn_id: str,
        prompt: str,
        message_id: str | None = None,
    ) -> dict:
        """Add one FIFO follow-up while an expected turn is active."""
        runtime = self._require_runtime(chat_id)
        item = runtime.agent.turn_controller.queue(
            prompt,
            expected_turn_id=expected_turn_id,
            message_id=message_id,
        )
        self._persist_turn_queue(runtime)
        return {"mode": "queue", **item.to_dict()}

    def queued_followups(self, chat_id: Hashable) -> list[dict]:
        runtime = self._require_runtime(chat_id)
        return [item.to_dict() for item in runtime.agent.turn_controller.queued()]

    def update_queued_followup(self, chat_id: Hashable, message_id: str, prompt: str) -> dict:
        runtime = self._require_runtime(chat_id)
        item = runtime.agent.turn_controller.update_queued(message_id, prompt)
        self._persist_turn_queue(runtime)
        return item.to_dict()

    def delete_queued_followup(self, chat_id: Hashable, message_id: str) -> None:
        runtime = self._require_runtime(chat_id)
        runtime.agent.turn_controller.delete_queued(message_id)
        self._persist_turn_queue(runtime)

    def pop_queued_followup(self, chat_id: Hashable):
        runtime = self._require_runtime(chat_id)
        item = runtime.agent.turn_controller.pop_queued()
        self._persist_turn_queue(runtime)
        return item

    @staticmethod
    def _persist_turn_queue(runtime: _ChatRuntime) -> None:
        session = runtime.agent.session_state
        if session is None:
            raise ValueError("No active session.")
        queued_inputs = [item.to_dict() for item in runtime.agent.turn_controller.queued()]
        save_turn_queue(session.session_id, queued_inputs)

    def decide_approval(
        self,
        chat_id: Hashable,
        approval_id: str,
        action: str,
        expected_turn_id: str,
        expected_batch_id: str,
        hook_handler=None,
    ) -> dict:
        runtime = self._require_runtime(chat_id)
        with runtime.approval_lock:
            with self._temporary_hook(runtime.agent, hook_handler):
                return runtime.agent.decide_approval(
                    approval_id,
                    action,
                    expected_turn_id=expected_turn_id,
                    expected_batch_id=expected_batch_id,
                )

    def continue_approval_batch(
        self,
        chat_id: Hashable,
        expected_turn_id: str,
        expected_batch_id: str,
        hook_handler=None,
        on_token=None,
        on_tool=None,
    ) -> RemoteTurnResult:
        runtime = self._require_runtime(chat_id)
        with runtime.lock, runtime.approval_lock:
            message_start = len(runtime.agent.messages)
            started_at = time.monotonic()
            with self._temporary_hook(runtime.agent, hook_handler):
                reply = runtime.agent.continue_pending_batch(
                    expected_turn_id=expected_turn_id,
                    expected_batch_id=expected_batch_id,
                    on_token=on_token,
                    on_tool=on_tool,
                )
            self._annotate_turn(
                runtime.agent,
                message_start,
                round((time.monotonic() - started_at) * 1000, 1),
            )
            return self._result_from_agent(runtime.agent, reply)

    def resolve_next_approval(
        self,
        chat_id: Hashable,
        *,
        approved: bool,
        grant_scope: bool = False,
        hook_handler=None,
        on_token=None,
        on_tool=None,
    ) -> RemoteTurnResult:
        """Resolve the next batch item for command-based remote channels."""
        runtime = self._require_runtime(chat_id)
        with runtime.approval_lock:
            task = runtime.agent.task_state
            batch = task.pending_tool_batch if task else None
            unresolved = batch.unresolved() if batch else []
            if batch is None or not unresolved:
                raise ValueError("No pending approval.")
            action = (
                "approve_scope"
                if approved and grant_scope
                else "approve"
                if approved
                else "reject"
            )
            with self._temporary_hook(runtime.agent, hook_handler):
                snapshot = runtime.agent.decide_approval(
                    unresolved[0].approval_id,
                    action,
                    expected_turn_id=batch.turn_id,
                    expected_batch_id=batch.batch_id,
                )
        if not snapshot["ready"]:
            return self._result_from_agent(
                runtime.agent,
                f"Approval recorded. {snapshot['unresolved_count']} approval(s) remain.",
            )
        return self.continue_approval_batch(
            chat_id,
            batch.turn_id,
            batch.batch_id,
            hook_handler=hook_handler,
            on_token=on_token,
            on_tool=on_tool,
        )

    def set_permission_mode(self, chat_id: Hashable, permission_mode: str) -> str:
        runtime = self._get_or_create_runtime(chat_id)
        runtime.agent.set_permission_mode(permission_mode)
        return runtime.agent.policy.permission_mode

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
                content = content_text(message.get("raw_prompt", raw_content))
                tool_call_id = str(message.get("tool_call_id", ""))
                known_tool = tool_calls.get(tool_call_id, {})
                messages.append(
                    {
                        "role": role,
                        "content": content[:20_000],
                        "message_id": str(message.get("message_id", "")),
                        "message_kind": str(message.get("message_kind", role)),
                        "revision_id": str(message.get("revision_id", "")),
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
        entries = load_transcript_entries(agent.session_state.session_id)
        superseded_turns = {
            str(entry.get("payload", {}).get("superseded_turn_id", ""))
            for entry in entries
            if entry.get("kind") == "turn_superseded"
        }
        transcript = [
            entry["message"]
            for entry in entries
            if entry.get("kind") == "message"
            and "message" in entry
            and str(entry["message"].get("turn_id", "")) not in superseded_turns
        ]
        if superseded_turns and any(
            not str(entry["message"].get("turn_id", ""))
            for entry in entries
            if entry.get("kind") == "message" and "message" in entry
        ):
            return current
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
                    and message.get("message_kind", "prompt") == "prompt"
                    and (
                        runtime.agent.task_state is None
                        or message.get("turn_id") == runtime.agent.task_state.task_id
                    )
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

    def current_session_id(self, chat_id: Hashable) -> str:
        """Return the active session identity used by persisted per-turn artifacts."""
        runtime = self._require_runtime(chat_id)
        with runtime.lock:
            if runtime.agent.session_state is None:
                raise ValueError("No active session.")
            return runtime.agent.session_state.session_id

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
            presented = {
                "path": path,
                "status": str(item.get("status", "modified"))[:32],
                "additions": additions,
                "deletions": deletions,
            }
            for key in ("turn_id", "state", "blocked_reason"):
                if key in item:
                    presented[key] = str(item.get(key, ""))[:1000]
            for key in ("can_undo", "can_reapply"):
                if key in item:
                    presented[key] = bool(item[key])
            files.append(presented)
        return files

    @staticmethod
    def _annotate_turn(agent: Agent, message_start: int, elapsed_ms: float) -> None:
        """Persist presentation metadata on the user turn without changing model content."""
        messages = agent.messages
        if not messages:
            return
        task_id = agent.task_state.task_id if agent.task_state is not None else ""
        user_index = next(
            (
                index for index in range(len(messages) - 1, -1, -1)
                if messages[index].get("role") == "user"
                and messages[index].get("message_kind", "prompt") == "prompt"
                and (not task_id or messages[index].get("turn_id") == task_id)
            ),
            None,
        )
        if user_index is None:
            return
        for message in messages[user_index:]:
            message.setdefault("turn_id", task_id)
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
        if not runtime.lock.acquire(blocking=False):
            return "Current task is still running; detailed status is temporarily unavailable."
        try:
            agent = runtime.agent
            if agent.session_state is None:
                return "No active session."
            task = agent.task_state
            if task is None:
                return f"Session: {agent.session_state.session_id}\nCurrent Task: (none)"

            suffix = ""
            batch = task.pending_tool_batch
            if batch and batch.unresolved():
                suffix = (
                    f"\nPending approvals: {len(batch.unresolved())} "
                    f"(next: {batch.unresolved()[0].tool_name})"
                )
            return (
                f"Session: {agent.session_state.session_id}\n"
                f"Task: {task.task_id}\n"
                f"Title: {task.title or '(untitled)'}\n"
                f"Status: {task.status}\n"
                f"Steps: {task.step_index}\n"
                f"Permission mode: {agent.policy.permission_mode}{suffix}"
            )
        finally:
            runtime.lock.release()

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

    def resume_session(
        self,
        chat_id: Hashable,
        session_id: str,
        permission_mode: str | None = None,
    ) -> RemoteTurnResult:
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
            if permission_mode:
                runtime.agent.set_permission_mode(permission_mode)
            # 远程会话恢复历史上下文，但始终使用当前 Runner 配置的模型。
            runtime.agent.restore_session(session_state, messages)
            # 首次恢复旧 checkpoint 时立即固化补齐的 message/turn/revision ID。
            runtime.agent.persist_session()
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
        previous_locked = False
        with self._state_lock:
            if not replace and chat_id in self._chats:
                return self._chats[chat_id]
            if replace:
                previous = self._chats.get(chat_id)
                if previous is not None and not previous.lock.acquire(blocking=False):
                    raise ValueError(
                        "当前会话仍在执行任务，请等待完成或取消后再恢复历史会话。"
                    )
                previous_locked = previous is not None
            try:
                runtime = _ChatRuntime(
                    agent=self._build_agent(),
                    lock=threading.RLock(),
                    approval_lock=threading.RLock(),
                )
            except Exception:
                if previous_locked:
                    previous.lock.release()
                raise
            self._chats[chat_id] = runtime
        if previous is not None:
            try:
                self._close_agent(previous.agent)
            finally:
                previous.lock.release()
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
            max_output_tokens=self.config.max_tokens,
            workspace_root=self.config.workspace_root,
            permission_mode=self.config.permission_mode,
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
        llm_cls = llm_class_for_provider(self.config.provider)
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
        pending_approval_scope = ""
        pending_approval_label = ""
        approval_batch_id = ""
        pending_approvals: list[dict] = []
        batch = task.pending_tool_batch
        if batch:
            approval_batch_id = batch.batch_id
            pending_approvals = [
                {
                    **item.to_dict(),
                    "arguments": presentation_tool_arguments(item.arguments),
                }
                for item in batch.approvals
            ]
            current = next(
                (item for item in batch.approvals if item.decision == "pending"),
                batch.approvals[0] if batch.approvals else None,
            )
            if current is not None:
                pending_tool = current.tool_name
                pending_reason = current.reason
                pending_arguments = presentation_tool_arguments(current.arguments)
                pending_requires_manual = current.requires_manual
                pending_approval_scope = current.approval_scope
                pending_approval_label = current.approval_label
        return RemoteTurnResult(
            text=text,
            session_id=agent.session_state.session_id,
            task_id=task.task_id,
            status=task.status,
            pending_tool=pending_tool,
            pending_reason=pending_reason,
            pending_arguments=pending_arguments,
            pending_requires_manual=pending_requires_manual,
            pending_approval_scope=pending_approval_scope,
            pending_approval_label=pending_approval_label,
            approval_batch_id=approval_batch_id,
            pending_approvals=pending_approvals,
            permission_mode=agent.policy.permission_mode,
            context_used_tokens=context["used_tokens"],
            context_window_tokens=context["window_tokens"],
        )
