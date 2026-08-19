import sys
import types as builtin_types

from opentelemetry import context as otel_context

from autocode.agent import Agent
from autocode.llm import LLM, LLMResponse, ToolCall
from autocode.observability import LangfuseTracer
from autocode.runtime import Policy, Runtime, StreamingToolExecutor
from autocode.state import PolicyDecision, TurnState, load_checkpoint
from autocode.state import checkpoint as checkpoint_module
from autocode.tools.base import ConcurrencySpec, Tool


_FAKE_OBSERVATION_KEY = otel_context.create_key("autocode-test-current-observation")


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


class _StreamingReadTool(_EchoTool):
    name = "read"
    parameters = {
        "type": "object",
        "properties": {"file_path": {"type": "string"}},
        "required": ["file_path"],
    }

    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        return ConcurrencySpec.resources(
            reads={f"file:{arguments['file_path']}"},
            reason="read-only observability probe",
        )

    def execute(self, file_path: str):
        return f"read:{file_path}"


class _ParallelEchoTool(_EchoTool):
    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        return ConcurrencySpec.parallel("parallel observability probe")


def _install_fake_langfuse(monkeypatch):
    events: list[dict] = []
    observation_counter = 0

    class _Observation:
        def __init__(self, kwargs):
            nonlocal observation_counter
            observation_counter += 1
            self.kwargs = dict(kwargs)
            trace_context = kwargs.get("trace_context") or {}
            self.parent = otel_context.get_value(_FAKE_OBSERVATION_KEY)
            self.trace_id = (
                trace_context.get("trace_id")
                or getattr(self.parent, "trace_id", None)
                or f"{observation_counter:032x}"
            )
            self.parent_id = (
                trace_context.get("parent_span_id")
                or getattr(self.parent, "id", None)
            )
            self.id = f"{observation_counter:016x}"
            self._token = None

        def __enter__(self):
            events.append(
                {
                    "event": "enter",
                    "kwargs": dict(self.kwargs),
                    "trace_id": self.trace_id,
                    "observation_id": self.id,
                    "parent_observation_id": self.parent_id,
                }
            )
            self._token = otel_context.attach(
                otel_context.set_value(_FAKE_OBSERVATION_KEY, self)
            )
            return self

        def __exit__(self, exc_type, exc, tb):
            if self._token is not None:
                otel_context.detach(self._token)
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
        return PolicyDecision("ask", "test approval")


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


def _tool_chunk(*, call_id: str, name: str, arguments: str):
    function = builtin_types.SimpleNamespace(name=name, arguments=arguments)
    tool_call = builtin_types.SimpleNamespace(index=0, id=call_id, function=function)
    delta = builtin_types.SimpleNamespace(content=None, tool_calls=[tool_call])
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


def test_agent_turn_groups_generations_and_tools_by_step(monkeypatch, tmp_path):
    events = _install_fake_langfuse(monkeypatch)
    llm = LLM(
        model="fake-model",
        api_key="sk-test",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-lf-test",
    )
    streams = [
        iter(
            [
                _tool_chunk(
                    call_id="call-1",
                    name="echo",
                    arguments='{"text":"hello"}',
                ),
                _usage_chunk(),
            ]
        ),
        iter([_content_chunk("done"), _usage_chunk()]),
    ]
    llm._call_with_retry = lambda params: streams.pop(0)
    agent = Agent(
        llm=llm,
        tools=[_EchoTool()],
        workspace_root=str(tmp_path),
        approval_policy="never",
    )

    assert agent.chat("use echo") == "done"

    enters = [item for item in events if item["event"] == "enter"]
    by_name = {
        item["kwargs"]["name"]: item
        for item in enters
        if item["kwargs"]["name"] in {"agent.chat", "agent.step.1", "agent.step.2", "tool.echo"}
    }
    generations = [item for item in enters if item["kwargs"]["name"] == "llm.chat"]
    assert by_name["agent.step.1"]["kwargs"]["as_type"] == "chain"
    assert by_name["agent.step.2"]["kwargs"]["as_type"] == "chain"
    assert by_name["agent.step.1"]["parent_observation_id"] == by_name["agent.chat"]["observation_id"]
    assert by_name["agent.step.2"]["parent_observation_id"] == by_name["agent.chat"]["observation_id"]
    assert generations[0]["parent_observation_id"] == by_name["agent.step.1"]["observation_id"]
    assert by_name["tool.echo"]["parent_observation_id"] == by_name["agent.step.1"]["observation_id"]
    assert generations[1]["parent_observation_id"] == by_name["agent.step.2"]["observation_id"]


class _TracedStreamingLLM:
    supports_streaming_tool_calls = True

    def __init__(self, tracer):
        self.tracer = tracer
        self.model = "fake-streaming"
        self.api_format = "messages"
        self.calls = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def chat(self, messages, tools=None, on_token=None, on_tool_call=None):
        self.calls += 1
        with self.tracer.start_generation(
            name="llm.chat",
            input_payload={"messages": messages, "tools": tools or []},
            model=self.model,
        ) as generation:
            if self.calls == 1:
                call = ToolCall(
                    id="call-read",
                    name="read",
                    arguments={"file_path": "README.md"},
                )
                on_tool_call(call)
                response = LLMResponse(content="", tool_calls=[call], stop_reason="tool_use")
            else:
                response = LLMResponse(content="done")
            generation.update(output={"content": response.content})
            return response


def test_speculative_tool_is_sibling_of_generation_inside_step(monkeypatch, tmp_path):
    events = _install_fake_langfuse(monkeypatch)
    tracer = LangfuseTracer(public_key="pk-test", secret_key="sk-lf-test")
    agent = Agent(
        llm=_TracedStreamingLLM(tracer),
        tools=[_StreamingReadTool()],
        workspace_root=str(tmp_path),
        approval_policy="never",
    )

    assert agent.chat("read then finish") == "done"

    enters = [item for item in events if item["event"] == "enter"]
    step = next(item for item in enters if item["kwargs"]["name"] == "agent.step.1")
    generation = next(item for item in enters if item["kwargs"]["name"] == "llm.chat")
    tool = next(item for item in enters if item["kwargs"]["name"] == "tool.read")
    assert generation["parent_observation_id"] == step["observation_id"]
    assert tool["parent_observation_id"] == step["observation_id"]
    assert tool["parent_observation_id"] != generation["observation_id"]


class _RetryingTracedLLM:
    supports_streaming_tool_calls = False

    def __init__(self, tracer):
        self.tracer = tracer
        self.model = "fake-retrying"
        self.api_format = "chat_completions"
        self.calls = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def chat(self, messages, tools=None, on_token=None):
        self.calls += 1
        with self.tracer.start_generation(
            name="llm.chat",
            input_payload={"messages": messages, "tools": tools or []},
            model=self.model,
        ) as generation:
            if self.calls == 1:
                raise RuntimeError("transient generation failure")
            generation.update(output={"content": "done"})
            return LLMResponse(content="done")


def test_generation_retry_stays_in_same_agent_step(monkeypatch, tmp_path):
    events = _install_fake_langfuse(monkeypatch)
    tracer = LangfuseTracer(public_key="pk-test", secret_key="sk-lf-test")
    monkeypatch.setattr("autocode.agent.loop.is_retryable_llm_error", lambda exc: True)
    agent = Agent(
        llm=_RetryingTracedLLM(tracer),
        tools=[],
        workspace_root=str(tmp_path),
    )

    assert agent.chat("retry once") == "done"

    enters = [item for item in events if item["event"] == "enter"]
    steps = [item for item in enters if item["kwargs"]["name"].startswith("agent.step.")]
    generations = [item for item in enters if item["kwargs"]["name"] == "llm.chat"]
    assert len(steps) == 1
    assert len(generations) == 2
    assert all(
        item["parent_observation_id"] == steps[0]["observation_id"]
        for item in generations
    )
    committed = next(
        item
        for item in events
        if item["event"] == "update"
        and item["name"] == "agent.step.1"
        and item["kwargs"].get("output", {}).get("status") == "completed"
    )
    assert committed["kwargs"]["output"]["attempt"] == 2
    assert committed["kwargs"]["output"]["tombstones"] == 1


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
    assert first_agent.turn_state is not None
    root_trace_id = first_agent.turn_state.langfuse_trace_id
    root_observation_id = first_agent.turn_state.langfuse_root_observation_id
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
        if item["event"] == "enter"
        and item["kwargs"].get("name") == "agent.continue_pending_batch"
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

    assert agent.turn_state is not None
    expected_context = {
        "trace_id": agent.turn_state.langfuse_trace_id,
        "parent_span_id": agent.turn_state.langfuse_root_observation_id,
    }
    approval_enters = [
        item
        for item in events
        if item["event"] == "enter"
        and item["kwargs"].get("name") == "agent.continue_pending_batch"
    ]
    assert len(approval_enters) == 1
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
    runtime = Runtime({"echo": _ParallelEchoTool()}, tracer=llm.tracer)
    state = TurnState(turn_id="turn-observation", status="running")
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
    agent_enter = next(
        item
        for item in events
        if item["event"] == "enter" and item["kwargs"].get("name") == "agent.chat"
    )
    assert {item["trace_id"] for item in tool_enters} == {agent_enter["trace_id"]}
    assert {item["parent_observation_id"] for item in tool_enters} == {
        agent_enter["observation_id"]
    }


def test_streaming_speculative_tool_observation_stays_in_agent_trace(monkeypatch):
    events = _install_fake_langfuse(monkeypatch)
    tracer = LangfuseTracer(public_key="pk-test", secret_key="sk-lf-test")
    runtime = Runtime({"read": _StreamingReadTool()}, tracer=tracer)
    state = TurnState(turn_id="turn-streaming-observation", status="running")
    tool_call = ToolCall(
        id="call-read",
        name="read",
        arguments={"file_path": "README.md"},
    )

    with tracer.start_agent_turn(
        name="agent.chat",
        input_payload={"user_message": "read while streaming"},
        session_id="session-streaming-observation",
        trace_name="autocode-agent-turn",
    ):
        executor = StreamingToolExecutor(
            runtime=runtime,
            turn_state=state,
            session_id="session-streaming-observation",
        )
        assert executor.add_tool(tool_call) is True
        assert executor.commit([tool_call]) == {"call-read": "read:README.md"}

    agent_enter = next(
        item
        for item in events
        if item["event"] == "enter" and item["kwargs"].get("name") == "agent.chat"
    )
    tool_enter = next(
        item
        for item in events
        if item["event"] == "enter" and item["kwargs"].get("name") == "tool.read"
    )
    assert tool_enter["trace_id"] == agent_enter["trace_id"]
    assert tool_enter["parent_observation_id"] == agent_enter["observation_id"]
