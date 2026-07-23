"""Channel-agnostic remote control manager."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Hashable

from ..agent import Agent
from ..attachments import prepare_attachments
from ..config import Config
from ..llm import LLM, LiteLLM
from ..message_content import content_text
from ..mcp import get_shared_mcp_manager
from ..state import format_trace, list_sessions, load_checkpoint, load_trace
from ..tools.factory import build_agent_tools


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
            with self._temporary_hook(runtime.agent, hook_handler):
                reply = runtime.agent.approve_pending(
                    approved=approved,
                    approval_handler=None,
                    enable_auto_approve=enable_auto_approve,
                )
            return self._result_from_agent(runtime.agent, reply)

    def reset_chat(self, chat_id: Hashable) -> None:
        with self._state_lock:
            runtime = self._chats.pop(chat_id, None)
        if runtime is not None:
            with runtime.lock:
                runtime.agent.close()

    def close(self) -> None:
        """Close every chat runtime and the shared MCP manager."""
        with self._state_lock:
            runtimes = list(self._chats.values())
            self._chats.clear()
        for runtime in runtimes:
            with runtime.lock:
                runtime.agent.close()
        self._mcp_manager.close()

    def conversation_messages(self, chat_id: Hashable, limit: int = 100) -> list[dict]:
        """Return a presentation-safe snapshot of the current conversation."""
        runtime = self._require_runtime(chat_id)
        with runtime.lock:
            messages = []
            for message in runtime.agent.messages[-max(1, limit):]:
                role = message.get("role", "")
                raw_content = message.get("content", "")
                if role not in {"user", "assistant", "tool"}:
                    continue
                content = content_text(raw_content)
                messages.append(
                    {
                        "role": role,
                        "content": content[:20_000],
                        "tool_call_id": str(message.get("tool_call_id", "")),
                    }
                )
            return messages

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

        session_state, messages, model = loaded
        runtime = self._get_or_create_runtime(chat_id, replace=True)
        with runtime.lock:
            runtime.agent.restore_session(session_state, messages, model)
            runtime.agent.llm.model = model
            current_task = session_state.current_task
            status = current_task.status if current_task else "idle"
            return self._result_from_agent(
                runtime.agent,
                f"Resumed session {session_state.session_id} ({status}).",
            )

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
                previous.agent.close()
        return runtime

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
        if agent.session_state is None:
            return RemoteTurnResult(text=text)
        task = agent.task_state
        if task is None:
            return RemoteTurnResult(text=text, session_id=agent.session_state.session_id)
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
        )
