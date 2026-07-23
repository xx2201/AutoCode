import sys
import types as builtin_types

from autocode.agent import Agent
from autocode.llm import LLM


def _install_fake_langfuse(monkeypatch):
    events: list[dict] = []

    class _Observation:
        def __init__(self, kwargs):
            self.kwargs = dict(kwargs)

        def __enter__(self):
            events.append({"event": "enter", "kwargs": dict(self.kwargs)})
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


def _content_chunk(content: str):
    delta = builtin_types.SimpleNamespace(content=content, tool_calls=None)
    choice = builtin_types.SimpleNamespace(delta=delta)
    return builtin_types.SimpleNamespace(choices=[choice], usage=None)


def _usage_chunk(prompt: int = 7, completion: int = 3):
    usage = builtin_types.SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)
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
    llm._call_with_retry = lambda params: iter([_content_chunk("hello"), _usage_chunk(prompt=11, completion=4)])

    response = llm.chat(messages=[{"role": "user", "content": "hi"}])

    assert response.content == "hello"
    assert events[0]["event"] == "client_init"
    enter = next(item for item in events if item["event"] == "enter" and item["kwargs"].get("as_type") == "generation")
    assert enter["kwargs"]["model"] == "fake-model"
    update = next(item for item in events if item["event"] == "update" and "usage_details" in item["kwargs"])
    assert update["kwargs"]["usage_details"]["prompt_tokens"] == 11
    assert update["kwargs"]["usage_details"]["completion_tokens"] == 4
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
