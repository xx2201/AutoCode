import pytest

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
        permission_mode="full_access",
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
        permission_mode="full_access",
    )
    reply = agent.chat("hello")
    assert reply == "done"
    assert agent.task_state is not None

    session_dir = checkpoint_module.session_dir(agent.session_state.session_id)
    checkpoint = session_dir.joinpath("checkpoint.json").read_text(encoding="utf-8")
    session_record = session_dir.joinpath("session.json").read_text(encoding="utf-8")

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


def test_edit_last_completed_turn_supersedes_context_but_not_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path / "sessions")
    llm = type(
        "TwoRepliesLLM",
        (),
        {
            "model": "fake-model",
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "responses": ["original answer", "edited answer"],
            "chat": lambda self, messages, tools=None, on_token=None: LLMResponse(
                content=self.responses.pop(0)
            ),
        },
    )()
    agent = Agent(llm=llm, workspace_root=str(tmp_path), permission_mode="full_access")

    assert agent.chat("original prompt") == "original answer"
    original_turn_id = agent.task_state.task_id
    original_message_id = agent.messages[0]["message_id"]
    untouched = tmp_path / "already-changed.txt"
    untouched.write_text("keep this workspace state", encoding="utf-8")

    assert agent.edit_last_turn(original_turn_id, "edited prompt") == "edited answer"

    assert untouched.read_text(encoding="utf-8") == "keep this workspace state"
    assert [message["content"] for message in agent.messages] == [
        "edited prompt",
        "edited answer",
    ]
    assert agent.messages[0]["raw_prompt"] == "edited prompt"
    assert agent.messages[0]["message_kind"] == "prompt"
    assert agent.messages[0]["message_id"] != original_message_id
    assert agent.task_state.supersedes_turn_id == original_turn_id
    assert agent.task_state.parent_revision_id
    marker = next(
        entry
        for entry in load_transcript_entries(agent.session_state.session_id)
        if entry.get("kind") == "turn_superseded"
    )
    assert marker["payload"]["superseded_message_id"] == original_message_id
    assert marker["payload"]["replacement_turn_id"] == agent.task_state.task_id


def test_edit_last_failed_turn_retries_from_the_original_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path / "sessions")

    class _FailThenSucceedLLM:
        model = "fake-model"
        total_prompt_tokens = 0
        total_completion_tokens = 0

        def __init__(self):
            self.responses = [
                LLMResponse(completion_tokens=32_000, stop_reason="max_tokens"),
                LLMResponse(content="recovered answer", stop_reason="end_turn"),
            ]

        def chat(self, messages, tools=None, on_token=None):
            return self.responses.pop(0)

    agent = Agent(
        llm=_FailThenSucceedLLM(),
        workspace_root=str(tmp_path),
        permission_mode="full_access",
    )

    with pytest.raises(RuntimeError, match="token 上限"):
        agent.chat("original prompt")
    failed_turn_id = agent.task_state.task_id
    assert agent.task_state.status == "failed"

    assert agent.edit_last_turn(failed_turn_id, "edited prompt") == "recovered answer"
    assert [message["content"] for message in agent.messages] == [
        "edited prompt",
        "recovered answer",
    ]
    assert agent.task_state.status == "completed"
    assert agent.task_state.supersedes_turn_id == failed_turn_id


def test_edit_rejects_non_latest_or_incomplete_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path / "sessions")
    agent = Agent(llm=_DoneLLM(), workspace_root=str(tmp_path), permission_mode="full_access")
    agent.chat("hello")

    try:
        agent.edit_last_turn("older-turn", "edited")
    except ValueError as exc:
        assert "not the last finished turn" in str(exc)
    else:
        raise AssertionError("expected stale turn edit to fail")

    agent.task_state.touch("running")
    try:
        agent.edit_last_turn(agent.task_state.task_id, "edited")
    except ValueError as exc:
        assert "last finished turn" in str(exc)
    else:
        raise AssertionError("expected active turn edit to fail")

