import pytest

from autocode.agent import Agent
from autocode.runtime import HookBus, Policy, Runtime
from autocode.state import checkpoint as checkpoint_module
from autocode.state import load_events
from autocode.llm import LLMResponse, ToolCall
from autocode.state import PolicyDecision, TaskState
from autocode.state import load_trace
from autocode.tools.base import Tool


@pytest.fixture(autouse=True)
def _isolate_sessions_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path / "sessions")


class _EchoTool(Tool):
    name = "echo"
    description = "Echoes input"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def execute(self, text: str) -> str:
        return f"echo:{text}"


class _InterruptTool(Tool):
    name = "echo"
    description = "Interrupts execution"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def execute(self, text: str) -> str:
        raise KeyboardInterrupt()


class _InterruptBashTool(Tool):
    name = "bash"
    description = "Interrupts execution"
    parameters = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }

    def execute(self, command: str, _confirmed_sensitive: bool = False) -> str:
        raise KeyboardInterrupt()


class _SafeBashTool(Tool):
    name = "bash"
    description = "Runs a fake bash command"
    parameters = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }

    def execute(self, command: str, timeout: int = 120, _confirmed_sensitive: bool = False) -> str:
        return f"ran:{command}|confirmed={_confirmed_sensitive}"


class _FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.model = "fake-model"
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def chat(self, messages, tools=None, on_token=None):
        response = self._responses.pop(0)
        if on_token and response.content:
            on_token(response.content)
        return response


class _RecordingLLM(_FakeLLM):
    def __init__(self, responses):
        super().__init__(responses)
        self.calls = []

    def chat(self, messages, tools=None, on_token=None):
        self.calls.append(messages)
        return super().chat(messages, tools=tools, on_token=on_token)


class _ConfirmAllPolicy(Policy):
    def evaluate_tool_call(self, tool_name: str, arguments: dict) -> PolicyDecision:
        return PolicyDecision("confirm", "test approval")


def test_runtime_emits_hooks(tmp_path):
    events = []
    hooks = HookBus()
    hooks.on("after_tool", lambda event, payload: events.append((event, payload["tool_name"])))
    runtime = Runtime({"echo": _EchoTool()}, policy=Policy(workspace_root=str(tmp_path)), hooks=hooks)
    state = TaskState(task_id="task_test", status="running")
    tc = ToolCall(id="1", name="echo", arguments={"text": "hi"})
    result = runtime.execute_tool_call(state, tc, "session_test")
    assert result == "echo:hi"
    assert events == [("after_tool", "echo")]


def test_agent_waits_for_approval(tmp_path):
    responses = [
        LLMResponse(content="", tool_calls=[ToolCall(id="1", name="bash", arguments={"command": "python manage.py migrate"})]),
    ]
    agent = Agent(
        llm=_FakeLLM(responses),
        tools=[],
        workspace_root=str(tmp_path),
    )
    agent.policy = _ConfirmAllPolicy(workspace_root=str(tmp_path))
    agent.runtime.policy = agent.policy
    reply = agent.chat("run migration")
    assert "waiting for approval" in reply
    assert agent.task_state is not None
    assert agent.task_state.status == "waiting_approval"
    assert agent.task_state.pending_approval is not None


def test_agent_approves_pending_tool(tmp_path):
    responses = [
        LLMResponse(content="", tool_calls=[ToolCall(id="1", name="echo", arguments={"text": "hi"})]),
        LLMResponse(content="done"),
    ]
    agent = Agent(
        llm=_FakeLLM(responses),
        tools=[_EchoTool()],
        workspace_root=str(tmp_path),
    )
    agent.policy = _ConfirmAllPolicy(workspace_root=str(tmp_path))
    agent.runtime.policy = agent.policy
    reply = agent.chat("run echo", approval_handler=lambda pending: True)
    assert reply == "done"
    assert agent.task_state is not None
    assert agent.task_state.status == "completed"


def test_agent_approve_all_completes_remaining_tool_calls_before_next_llm(tmp_path):
    llm = _RecordingLLM([
        LLMResponse(content="", tool_calls=[
            ToolCall(id="1", name="bash", arguments={"command": "python --version"}),
            ToolCall(id="2", name="bash", arguments={"command": "python -c \"import pika\""}),
        ]),
        LLMResponse(content="done"),
    ])
    agent = Agent(
        llm=llm,
        tools=[_SafeBashTool()],
        workspace_root=str(tmp_path),
    )
    agent.policy = _ConfirmAllPolicy(workspace_root=str(tmp_path))
    agent.runtime.policy = agent.policy

    waiting = agent.chat("check environment", approval_handler=None)
    assert "waiting for approval" in waiting
    assert agent.task_state is not None
    assert agent.task_state.pending_approval is not None
    assert len(agent.task_state.pending_approval.remaining_tool_calls) == 1

    reply = agent.approve_pending(True, approval_handler=None, enable_auto_approve=True)

    assert reply == "done"
    assert len(llm.calls) == 2
    second_call_tool_messages = [m for m in llm.calls[1] if m.get("role") == "tool"]
    assert len(second_call_tool_messages) == 2
    assert second_call_tool_messages[0]["tool_call_id"] == "1"
    assert second_call_tool_messages[1]["tool_call_id"] == "2"


def test_agent_requeues_next_pending_tool_from_same_batch(tmp_path):
    llm = _RecordingLLM([
        LLMResponse(content="", tool_calls=[
            ToolCall(id="1", name="bash", arguments={"command": "python --version"}),
            ToolCall(id="2", name="bash", arguments={"command": "python -c \"import pika\""}),
        ]),
        LLMResponse(content="done"),
    ])
    agent = Agent(
        llm=llm,
        tools=[_SafeBashTool()],
        workspace_root=str(tmp_path),
    )
    agent.policy = _ConfirmAllPolicy(workspace_root=str(tmp_path))
    agent.runtime.policy = agent.policy

    waiting = agent.chat("check environment", approval_handler=None)
    assert "waiting for approval" in waiting

    still_waiting = agent.approve_pending(True, approval_handler=None)
    assert "waiting for approval" in still_waiting
    assert len(llm.calls) == 1
    assert agent.task_state is not None
    assert agent.task_state.pending_approval is not None
    assert agent.task_state.pending_approval.tool_call_id == "2"
    assert agent.task_state.pending_approval.remaining_tool_calls == []

    reply = agent.approve_pending(True, approval_handler=None)
    assert reply == "done"
    assert len(llm.calls) == 2


def test_agent_todo_tool_updates_task_state(tmp_path):
    responses = [
        LLMResponse(content="", tool_calls=[ToolCall(id="1", name="todo_write", arguments={"todos": [{"content": "Read file", "status": "pending"}]})]),
        LLMResponse(content="planned"),
    ]
    agent = Agent(
        llm=_FakeLLM(responses),
        workspace_root=str(tmp_path),
        auto_approve=True,
    )
    reply = agent.chat("plan the work")
    assert reply == "planned"
    assert agent.task_state is not None
    assert agent.task_state.todos[0]["content"] == "Read file"


def test_agent_summarizes_when_max_rounds_reached(tmp_path):
    responses = [
        LLMResponse(content="", tool_calls=[ToolCall(id="1", name="echo", arguments={"text": "hi"})]),
        LLMResponse(content="已完成\n- 调用了 echo。\n\n当前卡点\n- 达到最大轮数。\n\n建议下一步\n- 如需继续，请回复“继续”。"),
    ]
    agent = Agent(
        llm=_FakeLLM(responses),
        tools=[_EchoTool()],
        workspace_root=str(tmp_path),
        auto_approve=True,
        max_rounds=1,
    )

    reply = agent.chat("run echo")

    assert "已完成" in reply
    assert "如需继续，请回复“继续”" in reply
    assert agent.task_state is not None
    assert agent.task_state.status == "failed"
    assert agent.task_state.last_error == "reached maximum tool-call rounds"


def test_todo_write_rejects_completed_after_blocked_or_failed_tool(tmp_path):
    agent = Agent(
        llm=_FakeLLM([]),
        workspace_root=str(tmp_path),
        auto_approve=True,
    )
    agent._ensure_task("plan the work")
    assert agent.task_state is not None
    agent.task_state.set_todos([{"content": "Move config to .env", "status": "pending"}])
    agent.task_state.note_tool_result("write_file", "Blocked by policy for write_file: .env is protected")

    todo_tool = next(tool for tool in agent.tools if tool.name == "todo_write")
    result = todo_tool.execute([{"content": "Move config to .env", "status": "completed"}])

    assert result.startswith("Error:")
    assert agent.task_state.todos[0]["status"] == "pending"


def test_agent_backfills_placeholder_tool_result_after_interrupt(tmp_path):
    responses = [
        LLMResponse(content="", tool_calls=[ToolCall(id="1", name="echo", arguments={"text": "hi"})]),
        LLMResponse(content="tool interrupt handled"),
    ]
    agent = Agent(
        llm=_FakeLLM(responses),
        tools=[_InterruptTool()],
        workspace_root=str(tmp_path),
        auto_approve=True,
    )

    reply = agent.chat("run echo")

    assert reply == "tool interrupt handled"
    assert agent.task_state is not None
    assert "interrupted by user" in agent.task_state.last_error
    tool_messages = [m for m in agent.messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert "Placeholder tool result" in tool_messages[0]["content"]


def test_agent_backfills_placeholder_after_approved_interrupt(tmp_path):
    responses = [
        LLMResponse(content="", tool_calls=[ToolCall(id="1", name="bash", arguments={"command": "echo hi > out.txt"})]),
        LLMResponse(content="approval path handled"),
    ]
    agent = Agent(
        llm=_FakeLLM(responses),
        tools=[_InterruptBashTool()],
        workspace_root=str(tmp_path),
    )
    agent.policy = _ConfirmAllPolicy(workspace_root=str(tmp_path))
    agent.runtime.policy = agent.policy

    waiting = agent.chat("run bash", approval_handler=None)
    assert "waiting for approval" in waiting

    reply = agent.approve_pending(True, approval_handler=None)

    assert reply == "approval path handled"
    assert agent.task_state is not None
    tool_messages = [m for m in agent.messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert "Placeholder tool result" in tool_messages[0]["content"]


def test_agent_writes_trace_and_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)

    responses = [
        LLMResponse(content="", tool_calls=[ToolCall(id="1", name="echo", arguments={"text": "hi"})]),
        LLMResponse(content="done"),
    ]
    agent = Agent(
        llm=_FakeLLM(responses),
        tools=[_EchoTool()],
        workspace_root=str(tmp_path),
        auto_approve=True,
    )
    reply = agent.chat("run echo")
    assert reply == "done"
    assert agent.task_state is not None

    trace = load_trace(agent.session_state.session_id)
    events = load_events(agent.session_state.session_id)
    assert trace is not None
    assert trace["status"] == "completed"
    assert trace["tool_calls"] == 1
    assert any(e["event"] == "user_message" for e in events)
    assert any(e["event"] == "after_tool" for e in events)


def test_agent_reuses_same_session_id_and_rotates_current_task_after_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)

    responses = [
        LLMResponse(content="first done"),
        LLMResponse(content="second done"),
    ]
    agent = Agent(
        llm=_FakeLLM(responses),
        workspace_root=str(tmp_path),
        auto_approve=True,
    )

    first = agent.chat("first prompt")
    assert first == "first done"
    assert agent.task_state is not None
    assert agent.session_state is not None
    first_session_id = agent.session_state.session_id
    first_task_id = agent.task_state.task_id

    second = agent.chat("second prompt")
    assert second == "second done"
    assert agent.session_state.session_id == first_session_id
    assert agent.task_state.task_id != first_task_id

    checkpoint = tmp_path.joinpath(f"{first_session_id}/checkpoint.json").read_text(encoding="utf-8")
    assert '"content": "first prompt"' in checkpoint
    assert '"content": "second prompt"' in checkpoint


def test_agent_schedules_project_memory_refresh_in_background(tmp_path):
    responses = [LLMResponse(content="done")]
    agent = Agent(
        llm=_FakeLLM(responses),
        workspace_root=str(tmp_path),
        auto_approve=True,
    )
    agent.llm._call_with_retry = object()
    calls = []

    def _schedule(messages, llm, force=False):
        calls.append(force)
        return True

    agent.memory.schedule_project_memory_refresh = _schedule

    reply = agent.chat("hello")

    assert reply == "done"
    assert calls == [True]

