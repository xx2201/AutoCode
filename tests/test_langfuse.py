import sys
import types as builtin_types

from autocode.agent import Agent
from autocode.llm import LLM, LLMResponse, ToolCall
from autocode.observability import LangfuseTracer
from autocode.runtime import Policy, Runtime
from autocode.state import PolicyDecision, TaskState, load_checkpoint
from autocode.state import checkpoint as checkpoint_module
from autocode.tools.base import Tool


class _EchoTool(Tool):
    name = "echo"
    description = "Echo text."
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def execute(self, text: str):
        return f"echo:{text}"


def _install_fake_langfuse(monkeypatch):
    events: list[dict] = []
    observation_counter = 0

    class _Observation:
        def __init__(self, kwargs):
            nonlocal observation_counter
            observation_counter += 1
            self.kwargs = dict(kwargs)
            trace_context = kwargs.get("trace_context") or {}
            self.trace_id = trace_context.get("trace_id") or f"{observation_counter:032x}"
            self.id = f"{observation_counter:016x}"

        def __enter__(self):
            events.append(
                {
                    "event": "enter",
                    "kwargs": dict(self.kwargs),
                    "trace_id": self.trace_id,
                    "observation_id": self.id,
                }
            )
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append({"event": "exit", "name": self.kwargs.get("name")})

        def update(self, **kwargs):
            events.append({"event": "update", "name": self.kwargs.get("name"), "kwargs": dict(kwargs)})

    class _Propagation:
        def __init__(self, kwargs):
            self.kwargs = dict(kwargs)

        def __enter__(self):
            events.append({"event": "propagate_enter", "kwargs": dict(self.kwargs)})

        def __exit__(self, exc_type, exc, tb):
            events.append({"event": "propagate_exit"})

    class _LangfuseClient:
        def __init__(self, **kwargs):
            events.append({"event": "client_init", "kwargs": dict(kwargs)})

        def start_as_current_observation(self, **kwargs):
            return _Observation(kwargs)

        def get_current_trace_id(self):
            events.append({"event": "get_current_trace_id"})
            return None

        def flush(self):
            events.append({"event": "flush"})

        def shutdown(self):
            events.append({"event": "shutdown"})

    fake = builtin_types.ModuleType("langfuse")
    fake.Langfuse = _LangfuseClient
    fake.propagate_attributes = lambda **kwargs: _Propagation(kwargs)
    monkeypatch.setitem(sys.modules, "langfuse", fake)
    return events


class _ConfirmPolicy(Policy):
    def evaluate_tool_call(self, tool_name: str, arguments: dict) -> PolicyDecision:
        return PolicyDecision("confirm", "test approval")


class _ScriptedLLM:
    def __init__(self, responses, tracer):
        self._responses = list(responses)
        self.tracer = tracer
        self.model = "fake-model"
        self.api_format = "chat_completions"
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def chat(self, messages, tools=None, on_token=None):
        response = self._responses.pop(0)
        if on_token and response.content:
            on_token(response.content)
        return response


def _content_chunk(content: str):
    delta = builtin_types.SimpleNamespace(content=content, tool_calls=None)
    choice = builtin_types.SimpleNamespace(delta=delta)
    return builtin_types.SimpleNamespace(choices=[choice], usage=None)


def _usage_chunk(prompt: int = 7, completion: int = 3, cached: int = 0):
    details = builtin_types.SimpleNamespace(cached_tokens=cached) if cached else None
    usage = builtin_types.SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        prompt_tokens_details=details,
    )
    return builtin_types.SimpleNamespace(choices=[], usage=usage)


def test_llm_chat_emits_langfuse_generation(monkeypatch):
    events = _install_fake_langfuse(monkeypatch)
    llm = LLM(
        model="fake-model",
        api_key="sk-test",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-lf-test",
        langfuse_base_url="https://langfuse.example",
    )
    llm._call_with_retry = lambda params: iter(
        [_content_chunk("hello"), _usage_chunk(prompt=11, completion=4, cached=3)]
    )

    messages = [{"role": "user", "content": "hi"}]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo text.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    response = llm.chat(messages=messages, tools=tools)

    assert response.content == "hello"
    assert events[0]["event"] == "client_init"
    enter = next(item for item in events if item["event"] == "enter" and item["kwargs"].get("as_type") == "generation")
    assert enter["kwargs"]["model"] == "fake-model"
    assert enter["kwargs"]["input"]["messages"] == messages
    assert enter["kwargs"]["input"]["tools"] == tools
    update = next(item for item in events if item["event"] == "update" and "usage_details" in item["kwargs"])
    assert update["kwargs"]["usage_details"]["input"] == 8
    assert update["kwargs"]["usage_details"]["output"] == 4
    assert update["kwargs"]["usage_details"]["cache_read_input_tokens"] == 3
    assert update["kwargs"]["output"]["content"] == "hello"


def test_agent_chat_batches_turn_trace_until_shutdown(monkeypatch, tmp_path):
    events = _install_fake_langfuse(monkeypatch)
    llm = LLM(
        model="fake-model",
        api_key="sk-test",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-lf-test",
    )
    llm._call_with_retry = lambda params: iter([_content_chunk("done"), _usage_chunk()])
    agent = Agent(llm=llm, tools=[], workspace_root=str(tmp_path))

    reply = agent.chat("debug this")

    assert reply == "done"
    propagate = next(item for item in events if item["event"] == "propagate_enter")
    assert propagate["kwargs"]["trace_name"] == "autocode-agent-turn"
    assert propagate["kwargs"]["session_id"]
    agent_enter = next(
        item
        for item in events
        if item["event"] == "enter" and item["kwargs"].get("name") == "agent.chat"
    )
    assert agent_enter["kwargs"]["as_type"] == "agent"
    turn_update = next(
        item
        for item in events
        if item["event"] == "update"
        and item["name"] == "agent.chat"
        and item["kwargs"].get("output", {}).get("status") == "completed"
    )
    assert turn_update["kwargs"]["output"]["text"] == "done"
    assert not any(item["event"] == "flush" for item in events)
    agent.close()
    assert any(item["event"] == "shutdown" for item in events)
    assert not any(item["event"] == "get_current_trace_id" for item in events)


def test_approval_continues_original_turn_trace_after_checkpoint_restore(monkeypatch, tmp_path):
    events = _install_fake_langfuse(monkeypatch)
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path / "sessions")
    first_tracer = LangfuseTracer(public_key="pk-test", secret_key="sk-lf-test")
    first_agent = Agent(
        llm=_ScriptedLLM(
            [
                LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id="call-1", name="echo", arguments={"text": "hello"})],
                )
            ],
            first_tracer,
        ),
        tools=[_EchoTool()],
        workspace_root=str(tmp_path),
    )
    first_agent.policy = _ConfirmPolicy(workspace_root=str(tmp_path))
    first_agent.runtime.policy = first_agent.policy

    waiting = first_agent.chat("run echo")

    assert "waiting for approval" in waiting
    assert first_agent.task_state is not None
    root_trace_id = first_agent.task_state.langfuse_trace_id
    root_observation_id = first_agent.task_state.langfuse_root_observation_id
    assert len(root_trace_id) == 32
    assert len(root_observation_id) == 16
    session_id = first_agent.session_state.session_id
    loaded = load_checkpoint(session_id)
    assert loaded is not None

    restored_state, restored_messages, _ = loaded
    continuation_tracer = LangfuseTracer(public_key="pk-test", secret_key="sk-lf-test")
    restored_agent = Agent(
        llm=_ScriptedLLM([LLMResponse(content="done")], continuation_tracer),
        tools=[_EchoTool()],
        workspace_root=str(tmp_path),
    )
    restored_agent.restore_session(restored_state, restored_messages)

    reply = restored_agent.approve_pending(True)

    assert reply == "done"
    approval_enter = next(
        item
        for item in events
        if item["event"] == "enter" and item["kwargs"].get("name") == "agent.approve_pending"
    )
    assert approval_enter["trace_id"] == root_trace_id
    assert approval_enter["kwargs"]["trace_context"] == {
        "trace_id": root_trace_id,
        "parent_span_id": root_observation_id,
    }


def test_multiple_approvals_remain_children_of_the_original_turn_trace(monkeypatch, tmp_path):
    events = _install_fake_langfuse(monkeypatch)
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path / "sessions")
    tracer = LangfuseTracer(public_key="pk-test", secret_key="sk-lf-test")
    agent = Agent(
        llm=_ScriptedLLM(
            [
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(id="call-1", name="echo", arguments={"text": "one"}),
                        ToolCall(id="call-2", name="echo", arguments={"text": "two"}),
                    ],
                ),
                LLMResponse(content="done"),
            ],
            tracer,
        ),
        tools=[_EchoTool()],
        workspace_root=str(tmp_path),
    )
    agent.policy = _ConfirmPolicy(workspace_root=str(tmp_path))
    agent.runtime.policy = agent.policy

    assert "waiting for approval" in agent.chat("run both")
    assert "waiting for approval" in agent.approve_pending(True)
    assert agent.approve_pending(True) == "done"

    assert agent.task_state is not None
    expected_context = {
        "trace_id": agent.task_state.langfuse_trace_id,
        "parent_span_id": agent.task_state.langfuse_root_observation_id,
    }
    approval_enters = [
        item
        for item in events
        if item["event"] == "enter" and item["kwargs"].get("name") == "agent.approve_pending"
    ]
    assert len(approval_enters) == 2
    assert all(item["kwargs"]["trace_context"] == expected_context for item in approval_enters)


def test_llm_clone_reuses_tracer_without_shutting_it_down(monkeypatch, tmp_path):
    events = _install_fake_langfuse(monkeypatch)
    llm = LLM(
        model="fake-model",
        api_key="sk-test",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-lf-test",
    )
    agent = Agent(llm=llm, tools=[], workspace_root=str(tmp_path))
    llm_copy = llm.clone()

    assert llm_copy.tracer is llm.tracer
    agent.close(shutdown_observability=False)
    assert not any(item["event"] == "shutdown" for item in events)
    llm.tracer.shutdown()
    llm.tracer.shutdown()
    assert [item["event"] for item in events].count("shutdown") == 1


def test_runtime_emits_nested_tool_observations_for_parallel_calls(monkeypatch):
    events = _install_fake_langfuse(monkeypatch)
    llm = LLM(
        model="fake-model",
        api_key="sk-test",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-lf-test",
    )
    runtime = Runtime({"echo": _EchoTool()}, tracer=llm.tracer)
    state = TaskState(task_id="task-observation", status="running")
    tool_calls = [
        ToolCall(id="call-1", name="echo", arguments={"text": "one"}),
        ToolCall(id="call-2", name="echo", arguments={"text": "two"}),
    ]

    with llm.tracer.start_agent_turn(
        name="agent.chat",
        input_payload={"user_message": "run tools"},
        session_id="session-observation",
        trace_name="autocode-agent-turn",
    ):
        results = runtime.execute_tool_calls_parallel(
            state,
            tool_calls,
            "session-observation",
        )

    assert results == ["echo:one", "echo:two"]
    tool_enters = [
        item
        for item in events
        if item["event"] == "enter" and item["kwargs"].get("as_type") == "tool"
    ]
    assert {item["kwargs"]["name"] for item in tool_enters} == {
        "tool.echo",
    }
    tool_updates = [
        item
        for item in events
        if item["event"] == "update"
        and item["name"] == "tool.echo"
        and "output" in item["kwargs"]
    ]
    assert {item["kwargs"]["output"]["result"] for item in tool_updates} == {
        "echo:one",
        "echo:two",
    }
