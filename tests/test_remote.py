from autocode.state import checkpoint as checkpoint_module
from autocode.config import Config
from autocode.llm import LLMResponse, ToolCall
from autocode.remote.formatting import render_turn_result, split_message
from autocode.remote.manager import RemoteManager
from autocode.state import SessionState, TaskState
from autocode.tools.base import Tool


class _DelegationTool(Tool):
    name = "agent"
    description = "Delegation"
    parameters = {
        "type": "object",
        "properties": {"task": {"type": "string"}},
        "required": ["task"],
    }

    def execute(self, task: str) -> str:
        return f"delegated:{task}"


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


def _config(tmp_path):
    return Config(
        model="fake-model",
        api_key="secret",
        workspace_root=str(tmp_path),
    )


def test_remote_manager_handles_approval_flow(tmp_path):
    llm = _FakeLLM([
        LLMResponse(content="", tool_calls=[ToolCall(id="1", name="agent", arguments={"task": "inspect"})]),
        LLMResponse(content="done"),
    ])
    manager = RemoteManager(_config(tmp_path), llm_factory=lambda: llm, tools=[_DelegationTool()])

    pending = manager.submit(101, "run delegated task")
    assert pending.status == "waiting_approval"
    assert pending.pending_tool == "agent"

    resolved = manager.resolve_approval(101, approved=True)
    assert resolved.text == "done"
    assert resolved.status == "completed"


def test_remote_manager_can_resume_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    llm = _FakeLLM([LLMResponse(content="finished")])
    manager = RemoteManager(_config(tmp_path), llm_factory=lambda: llm, tools=[])

    result = manager.submit(202, "finish task")
    assert result.status == "completed"
    session_id = result.session_id

    resumed = manager.resume_session(202, session_id)
    assert resumed.session_id == session_id
    assert resumed.status == "completed"


def test_remote_manager_lists_resume_candidates_for_current_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path / "sessions")
    checkpoint_module.save_checkpoint(
        SessionState(session_id="session_a", current_task=TaskState(task_id="task_a", title="current", status="completed")),
        [],
        "m1",
        workspace_root=str(tmp_path),
    )
    checkpoint_module.save_checkpoint(
        SessionState(session_id="session_b", current_task=TaskState(task_id="task_b", title="other", status="completed")),
        [],
        "m2",
        workspace_root="G:/other",
    )

    manager = RemoteManager(_config(tmp_path), llm_factory=lambda: _FakeLLM([LLMResponse(content="ok")]), tools=[])
    items = manager.list_resume_candidates()

    assert len(items) == 1
    assert items[0]["session_id"] == "session_a"
    assert items[0]["task_id"] == "task_a"


def test_remote_manager_reset_drops_chat_runtime(tmp_path):
    manager = RemoteManager(_config(tmp_path), llm_factory=lambda: _FakeLLM([LLMResponse(content="ok")]), tools=[])

    first = manager.submit(303, "hello")
    assert first.text == "ok"

    manager.reset_chat(303)
    try:
        manager.current_task_summary(303)
    except ValueError as exc:
        assert "No chat session yet" in str(exc)
    else:
        raise AssertionError("expected ValueError after reset")


def test_render_turn_result_includes_approval_hint():
    text = render_turn_result(
        type("Result", (), {
            "text": "waiting",
            "session_id": "session_123",
            "task_id": "task_123",
            "status": "waiting_approval",
            "pending_tool": "bash",
            "pending_reason": "confirmation required",
            "pending_arguments": {"command": "python app.py"},
            "pending_requires_manual": False,
            "auto_approve_for_task": False,
        })()
    )
    assert "/approve" in text
    assert "/approve_all" in text
    assert "session_123" in text
    assert "task_123" in text
    assert "python app.py" in text


def test_split_message_respects_limit():
    chunks = split_message("a" * 5000, limit=1000)
    assert len(chunks) > 1
    assert all(len(chunk) <= 1000 for chunk in chunks)


def test_remote_manager_approve_all_marks_task_state(tmp_path):
    llm = _FakeLLM([
        LLMResponse(content="", tool_calls=[ToolCall(id="1", name="agent", arguments={"task": "inspect"})]),
        LLMResponse(content="done"),
    ])
    manager = RemoteManager(_config(tmp_path), llm_factory=lambda: llm, tools=[_DelegationTool()])
    manager.submit(404, "run delegated task")

    result = manager.resolve_approval(404, approved=True, enable_auto_approve=True)
    assert result.auto_approve_for_task is False
    summary = manager.current_task_summary(404)
    assert "Approve_all: off" in summary


def test_remote_manager_temporary_hook_receives_events_and_unsubscribes(tmp_path):
    llm = _FakeLLM([LLMResponse(content="done")])
    manager = RemoteManager(_config(tmp_path), llm_factory=lambda: llm, tools=[])
    events = []

    def _hook(event, payload):
        events.append(event)

    result = manager.submit(505, "hello", hook_handler=_hook)
    assert result.text == "done"
    assert "before_llm" in events
    assert "after_llm" in events

    runtime = manager._require_runtime(505)
    for event in manager._HOOK_EVENTS:
        assert _hook not in runtime.agent.hooks._handlers.get(event, [])


def test_remote_manager_reuses_same_session_id_within_same_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    llm = _FakeLLM([LLMResponse(content="first"), LLMResponse(content="second")])
    manager = RemoteManager(_config(tmp_path), llm_factory=lambda: llm, tools=[])

    first = manager.submit(606, "hello")
    second = manager.submit(606, "again")

    assert first.status == "completed"
    assert second.status == "completed"
    assert second.session_id == first.session_id
    assert second.task_id != first.task_id

