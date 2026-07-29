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


class _WriteToolShouldNotRun(Tool):
    name = "write_file"
    description = "Should not execute when arguments are invalid"
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["file_path", "content"],
    }

    def execute(self, file_path: str, content: str) -> str:
        raise AssertionError("write_file should not execute for invalid tool-call JSON")


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
        return PolicyDecision(
            "confirm",
            "test approval",
            approval_scope=f"tool:{tool_name}",
            approval_label=f"allow {tool_name}",
        )


def test_runtime_emits_hooks(tmp_path):
    events = []
    hooks = HookBus()
    hooks.on("before_tool", lambda event, payload: events.append((event, payload)))
    hooks.on("after_tool", lambda event, payload: events.append((event, payload)))
    runtime = Runtime({"echo": _EchoTool()}, policy=Policy(workspace_root=str(tmp_path)), hooks=hooks)
    state = TaskState(task_id="task_test", status="running")
    tc = ToolCall(id="1", name="echo", arguments={"text": "hi"})
    result = runtime.execute_tool_call(state, tc, "session_test")

    assert result == "echo:hi"
    assert events[0][0] == "before_tool"
    assert events[0][1] == {
        "session_id": "session_test",
        "task_id": "task_test",
        "task_title": "",
        "tool_call_id": "1",
        "tool_name": "echo",
        "arguments": {"text": "hi"},
        "execution_group_id": 1,
        "execution_group_size": 1,
        "concurrency_mode": "exclusive",
        "concurrency_reason": "tool does not declare concurrency safety",
    }
    assert events[1][0] == "after_tool"
    assert events[1][1]["tool_call_id"] == "1"
    assert events[1][1]["tool_name"] == "echo"
    assert events[1][1]["arguments"] == {"text": "hi"}
    assert events[1][1]["result"] == "echo:hi"
    assert events[1][1]["duration_ms"] >= 0
    assert events[1][1]["success"] is True


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
    assert agent.task_state.pending_tool_batch is not None
    assert len(agent.task_state.pending_tool_batch.approvals) == 1


def test_agent_emits_ordered_assistant_step_before_tool_execution(tmp_path):
    response = LLMResponse(
        content="I will inspect the repository.",
        tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hi"})],
    )
    agent = Agent(
        llm=_FakeLLM([response, LLMResponse(content="done")]),
        tools=[_EchoTool()],
        workspace_root=str(tmp_path),
        permission_mode="full_access",
    )
    events = []
    agent.hooks.on("assistant_step", lambda event, payload: events.append(payload))

    assert agent.chat("inspect") == "done"
    assert events[0]["content"] == "I will inspect the repository."
    assert events[0]["tool_calls"] == [
        {"id": "call_1", "name": "echo", "arguments": {"text": "hi"}}
    ]


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


def test_agent_returns_explicit_error_for_invalid_tool_call_json(tmp_path):
    llm = _RecordingLLM([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="1",
                    name="write_file",
                    arguments={},
                    raw_arguments='{"file_path":"demo.txt","content":"hello"',
                    parse_error="tool-call arguments were not valid JSON: Expecting ',' delimiter at char 41",
                )
            ],
        ),
        LLMResponse(content="done"),
    ])
    agent = Agent(
        llm=llm,
        tools=[_WriteToolShouldNotRun()],
        workspace_root=str(tmp_path),
    )

    reply = agent.chat("write file")

    assert reply == "done"
    assert len(llm.calls) == 2
    second_call_tool_messages = [m for m in llm.calls[1] if m.get("role") == "tool"]
    assert len(second_call_tool_messages) == 1
    tool_result = second_call_tool_messages[0]["content"]
    assert "Error: invalid arguments for write_file" in tool_result
    assert "Raw arguments:" in tool_result
    assert "Resend the same tool call" in tool_result
    assert "[recovery]" in tool_result


def test_agent_scope_approval_completes_matching_tool_calls_before_next_llm(tmp_path):
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
    assert agent.task_state.pending_tool_batch is not None
    assert len(agent.task_state.pending_tool_batch.approvals) == 2

    reply = agent.approve_pending(True, approval_handler=None, grant_scope=True)

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
    assert agent.task_state.pending_tool_batch is not None
    unresolved = agent.task_state.pending_tool_batch.unresolved()
    assert len(unresolved) == 1
    assert unresolved[0].tool_call_id == "2"

    reply = agent.approve_pending(True, approval_handler=None)
    assert reply == "done"
    assert len(llm.calls) == 2


def test_agent_records_independent_decisions_before_executing_batch(tmp_path):
    llm = _RecordingLLM([
        LLMResponse(content="", tool_calls=[
            ToolCall(id="1", name="bash", arguments={"command": "first"}),
            ToolCall(id="2", name="bash", arguments={"command": "second"}),
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

    assert "waiting for approval" in agent.chat("run both")
    batch = agent.task_state.pending_tool_batch
    first, second = batch.approvals

    first_result = agent.decide_approval(
        first.approval_id,
        "approve",
        expected_turn_id=batch.turn_id,
        expected_batch_id=batch.batch_id,
    )
    assert first_result["ready"] is False
    assert first_result["unresolved_count"] == 1
    assert len(llm.calls) == 1

    second_result = agent.decide_approval(
        second.approval_id,
        "reject",
        expected_turn_id=batch.turn_id,
        expected_batch_id=batch.batch_id,
    )
    assert second_result["ready"] is True
    assert len(llm.calls) == 1

    reply = agent.continue_pending_batch(
        expected_turn_id=batch.turn_id,
        expected_batch_id=batch.batch_id,
    )
    assert reply == "done"
    tool_messages = [
        message
        for message in llm.calls[1]
        if message.get("role") == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_messages] == ["1", "2"]
    assert "ran:first" in tool_messages[0]["content"]
    assert "approval denied by user" in tool_messages[1]["content"]


def test_agent_todo_tool_updates_task_state(tmp_path):
    responses = [
        LLMResponse(content="", tool_calls=[ToolCall(id="1", name="todo_write", arguments={"todos": [{"content": "Read file", "status": "pending"}]})]),
        LLMResponse(content="planned"),
    ]
    agent = Agent(
        llm=_FakeLLM(responses),
        workspace_root=str(tmp_path),
        permission_mode="full_access",
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
        permission_mode="full_access",
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
        permission_mode="full_access",
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
        permission_mode="full_access",
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
        permission_mode="full_access",
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
        permission_mode="full_access",
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

    checkpoint = checkpoint_module.session_dir(first_session_id).joinpath(
        "checkpoint.json"
    ).read_text(encoding="utf-8")
    assert '"content": "first prompt"' in checkpoint
    assert '"content": "second prompt"' in checkpoint


def test_agent_schedules_project_memory_refresh_in_background(tmp_path):
    responses = [LLMResponse(content="done")]
    agent = Agent(
        llm=_FakeLLM(responses),
        workspace_root=str(tmp_path),
        permission_mode="full_access",
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


def test_agent_cleans_temporary_processes_when_task_completes(tmp_path):
    agent = Agent(
        llm=_FakeLLM([LLMResponse(content="done")]),
        workspace_root=str(tmp_path),
        permission_mode="full_access",
    )
    cleaned = []
    agent.processes.cleanup_task_processes = lambda task_id: cleaned.append(task_id) or []

    reply = agent.chat("finish work")

    assert reply == "done"
    assert agent.task_state is not None
    assert cleaned == [agent.task_state.task_id]


def test_agent_cleans_temporary_processes_when_task_fails(tmp_path):
    agent = Agent(
        llm=_FakeLLM([
            LLMResponse(content="", tool_calls=[ToolCall(id="1", name="echo", arguments={"text": "hi"})]),
            LLMResponse(content="已完成\n- 已达到本轮最大工具调用次数。\n\n当前卡点\n- 达到最大轮数。\n\n建议下一步\n- 如需继续，请回复“继续”。"),
        ]),
        tools=[_EchoTool()],
        workspace_root=str(tmp_path),
        permission_mode="full_access",
        max_rounds=1,
    )
    cleaned = []
    agent.processes.cleanup_task_processes = lambda task_id: cleaned.append(task_id) or []

    reply = agent.chat("run echo")

    assert "已完成" in reply
    assert agent.task_state is not None
    assert agent.task_state.status == "failed"
    assert cleaned == [agent.task_state.task_id]


def test_agent_reset_cleans_all_managed_processes(tmp_path):
    agent = Agent(
        llm=_FakeLLM([]),
        workspace_root=str(tmp_path),
        permission_mode="full_access",
    )
    calls = []
    agent.processes.cleanup_all = lambda include_persistent=True: calls.append(include_persistent) or []

    agent.reset()

    assert calls == [True]
    assert agent.session_state is None
    assert agent.messages == []

