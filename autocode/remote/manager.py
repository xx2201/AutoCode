"""Channel-agnostic remote control manager."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Hashable

from ..agent import Agent
from ..config import Config
from ..llm import LLM, LiteLLM
from ..state import format_trace, list_sessions, load_checkpoint, load_trace


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
        "policy_decision",
        "before_tool",
        "after_tool",
        "approval_resolved",
        "task_status",
        "task_error",
        "todo_updated",
    )

    def __init__(self, config: Config, llm_factory=None, tools: list | None = None):
        self.config = config
        self._llm_factory = llm_factory
        self._tools = tools
        self._state_lock = threading.Lock()
        self._chats: dict[Hashable, _ChatRuntime] = {}

    def submit(self, chat_id: Hashable, prompt: str, hook_handler=None) -> RemoteTurnResult:
        runtime = self._get_or_create_runtime(chat_id)
        with runtime.lock:
            with self._temporary_hook(runtime.agent, hook_handler):
                reply = runtime.agent.chat(prompt, approval_handler=None)
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
            self._chats.pop(chat_id, None)

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
        with self._state_lock:
            if not replace and chat_id in self._chats:
                return self._chats[chat_id]
            runtime = _ChatRuntime(agent=self._build_agent(), lock=threading.RLock())
            self._chats[chat_id] = runtime
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
            max_context_tokens=self.config.max_context_tokens,
            workspace_root=self.config.workspace_root,
            auto_approve=self.config.auto_approve,
        )

    def _build_tools(self):
        if self._tools is None:
            return None
        built = []
        for tool in self._tools:
            if hasattr(tool, "clone"):
                built.append(tool.clone())
            else:
                built.append(type(tool)())
        return built

    def _build_llm(self):
        llm_cls = LiteLLM if self.config.provider == "litellm" else LLM
        return llm_cls(
            model=self.config.model,
            api_key=self.config.api_key,
            base_url=self.config.base_url,
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
