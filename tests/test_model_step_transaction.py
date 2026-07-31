import threading
import time

from autocode.agent import Agent
from autocode.llm import LLMResponse, ToolCall
from autocode.tools.base import ConcurrencySpec, Tool


class _StreamingReadTool(Tool):
    name = "stream_read"
    description = "Read a value without side effects"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def __init__(self, started: threading.Event):
        self.started = started
        self.calls = 0

    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        return ConcurrencySpec.resources(
            reads={f"file:{arguments['path']}"},
            reason="read-only test probe",
        )

    def execute(self, path: str) -> str:
        self.calls += 1
        self.started.set()
        return f"contents:{path}"

    def clone(self):
        return self


class _RetryingStreamingLLM:
    supports_streaming_tool_calls = True

    def __init__(self, tool_started: threading.Event):
        self.model = "fake-streaming"
        self.tool_started = tool_started
        self.calls = 0
        self.requests = []
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_cache_read_tokens = 0
        self.total_cache_miss_tokens = 0

    def chat(
        self,
        messages,
        tools=None,
        on_token=None,
        on_tool_call=None,
    ):
        self.calls += 1
        self.requests.append((messages, tools))
        if self.calls == 3:
            on_token("done")
            return LLMResponse(content="done", prompt_tokens=24, completion_tokens=2)
        call = ToolCall(
            id=f"call-{self.calls}",
            name="stream_read",
            arguments={"path": "README.md"},
        )
        self.tool_started.clear()
        on_token("partial" if self.calls == 1 else "final")
        on_tool_call(call)
        assert self.tool_started.wait(timeout=1), "streamed tool did not start before model completion"
        if self.calls == 1:
            raise RuntimeError("transient stream failure")
        return LLMResponse(
            content="",
            tool_calls=[call],
            prompt_tokens=20,
            completion_tokens=4,
            stop_reason="tool_use",
        )


def test_model_step_rolls_back_partial_stream_and_reuses_prompt_snapshot(
    tmp_path,
    monkeypatch,
):
    started = threading.Event()
    tool = _StreamingReadTool(started)
    llm = _RetryingStreamingLLM(started)
    agent = Agent(
        llm=llm,
        tools=[tool],
        workspace_root=str(tmp_path),
        permission_mode="full_access",
    )
    events = []
    agent.hooks.on("model_step_tombstone", lambda event, data: events.append((event, data)))
    monkeypatch.setattr("autocode.agent.loop.is_retryable_llm_error", lambda exc: True)

    result = agent.chat("read the file and finish", on_token=lambda text: None)

    assert result == "done"
    assert llm.calls == 3
    assert tool.calls == 2
    assert len(events) == 1
    assert events[0][1]["visible_chars"] == len("partial")
    assert events[0][1]["discarded_tool_call_ids"] == ["call-1"]
    assert llm.requests[0][0][0] == llm.requests[1][0][0]
    assert llm.requests[0][1] == llm.requests[1][1]
    assert agent.task_state.prompt_snapshot["digest"]
    tool_messages = [message for message in agent.messages if message["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call-2"


def test_prompt_snapshot_does_not_refresh_tools_mid_turn(tmp_path):
    class _FinalLLM:
        supports_streaming_tool_calls = False
        model = "fake"
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_cache_read_tokens = 0
        total_cache_miss_tokens = 0

        def __init__(self):
            self.schemas = []

        def chat(self, messages, tools=None, on_token=None):
            self.schemas.append(tools)
            if len(self.schemas) == 1:
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="one",
                            name="stream_read",
                            arguments={"path": "a"},
                        )
                    ]
                )
            return LLMResponse(content="done")

    started = threading.Event()
    llm = _FinalLLM()
    agent = Agent(
        llm=llm,
        tools=[_StreamingReadTool(started)],
        workspace_root=str(tmp_path),
        permission_mode="full_access",
    )

    assert agent.chat("run") == "done"
    assert llm.schemas[0] == llm.schemas[1]
    assert agent.task_state.prompt_snapshot["tool_names"] == ["stream_read"]
