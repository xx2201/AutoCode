from autocode.agent import Agent
from autocode.llm import LLMResponse
from autocode.llm import ToolCall
from autocode.state import checkpoint as checkpoint_module
from autocode.state import load_transcript_entries, load_transcript_messages
from autocode.tools.base import Tool


class _DoneLLM:
    def __init__(self):
        self.model = "fake-model"
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def chat(self, messages, tools=None, on_token=None):
        return LLMResponse(content="done")


class _EchoTool(Tool):
    name = "echo"
    description = "echo"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def execute(self, text: str) -> str:
        return text


class _LongTaskLLM:
    def __init__(self):
        self.model = "fake-model"
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._calls = 0

    def chat(self, messages, tools=None, on_token=None):
        self._calls += 1
        if self._calls <= 6:
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(id=f"call-{self._calls}", name="echo", arguments={"text": "y" * 220})],
            )
        return LLMResponse(content="done")


def test_agent_writes_raw_transcript_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)

    agent = Agent(
        llm=_LongTaskLLM(),
        tools=[_EchoTool()],
        workspace_root=str(tmp_path),
        max_context_tokens=120,
        auto_approve=True,
    )

    reply = agent.chat("message-0-" + ("x" * 200))
    assert reply == "done"

    assert agent.task_state is not None
    entries = load_transcript_entries(agent.session_state.session_id)
    messages = load_transcript_messages(agent.session_state.session_id)

    assert len(messages) > len(agent.messages)
    assert messages[0]["role"] == "user"
    assert messages[0]["content"].startswith("message-0-")
    assert any(message.get("role") == "tool" for message in messages)
    assert any(entry.get("kind") == "compact" for entry in entries)


def test_checkpoint_and_task_record_publish_transcript_file(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)

    agent = Agent(
        llm=_DoneLLM(),
        workspace_root=str(tmp_path),
        auto_approve=True,
    )
    reply = agent.chat("hello")
    assert reply == "done"
    assert agent.task_state is not None

    checkpoint = tmp_path.joinpath(f"{agent.session_state.session_id}/checkpoint.json").read_text(encoding="utf-8")
    session_record = tmp_path.joinpath(f"{agent.session_state.session_id}/session.json").read_text(encoding="utf-8")
    session_dir = tmp_path / agent.session_state.session_id

    assert '"transcript_file": "transcript.jsonl"' in checkpoint
    assert '"transcript_file": "transcript.jsonl"' in session_record
    assert {path.name for path in session_dir.iterdir()} == {
        "audit.jsonl",
        "checkpoint.json",
        "current_task.json",
        "session.json",
        "trace.json",
        "transcript.jsonl",
    }

