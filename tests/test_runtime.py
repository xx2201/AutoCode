from corecoder.agent import Agent
from corecoder import checkpoint as checkpoint_module
from corecoder.journal import load_events
from corecoder.hooks import HookBus
from corecoder.llm import LLMResponse, ToolCall
from corecoder.policy import Policy
from corecoder.runtime import Runtime
from corecoder.state import TaskState
from corecoder.trace import load_trace
from corecoder.tools.base import Tool


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


def test_runtime_emits_hooks(tmp_path):
    events = []
    hooks = HookBus()
    hooks.on("after_tool", lambda event, payload: events.append((event, payload["tool_name"])))
    runtime = Runtime({"echo": _EchoTool()}, policy=Policy(workspace_root=str(tmp_path)), hooks=hooks)
    state = TaskState(task_id="task_test", status="running")
    tc = ToolCall(id="1", name="echo", arguments={"text": "hi"})
    result = runtime.execute_tool_call(state, tc)
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
    reply = agent.chat("run echo", approval_handler=lambda pending: True)
    assert reply == "done"
    assert agent.task_state is not None
    assert agent.task_state.status == "completed"


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


def test_agent_writes_trace_and_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "TASKS_DIR", tmp_path)

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

    trace = load_trace(agent.task_state.task_id)
    events = load_events(agent.task_state.task_id)
    assert trace is not None
    assert trace["status"] == "completed"
    assert trace["tool_calls"] == 1
    assert any(e["event"] == "user_message" for e in events)
    assert any(e["event"] == "after_tool" for e in events)
