from autocode.state import checkpoint as checkpoint_module
from autocode.config import Config
from autocode.llm import LLMResponse, ToolCall
from autocode.remote.formatting import render_turn_result, split_message
from autocode.remote.manager import RemoteManager
from autocode.runtime import Policy
from autocode.state import SessionState, TaskState
from autocode.state import PolicyDecision
from autocode.tools.base import Tool
from autocode.state.transcript import TranscriptLogger


class _DelegationTool(Tool):
    name = "agent"
    description = "Delegation"
    parameters = {
        "type": "object",
        "properties": {"task": {"type": "string"}},
        "required": ["task"],
    }

    def execute(self, task: str, content: str | None = None) -> str:
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


class _ConfirmDelegationPolicy(Policy):
    def evaluate_tool_call(self, tool_name: str, arguments: dict) -> PolicyDecision:
        if tool_name == "agent":
            return PolicyDecision("confirm", "approval required for delegation")
        return super().evaluate_tool_call(tool_name, arguments)


class _ConfirmingRemoteManager(RemoteManager):
    def _build_agent(self):
        agent = super()._build_agent()
        agent.policy = _ConfirmDelegationPolicy(workspace_root=self.config.workspace_root)
        agent.runtime.policy = agent.policy
        return agent


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
    manager = _ConfirmingRemoteManager(_config(tmp_path), llm_factory=lambda: llm, tools=[_DelegationTool()])

    pending = manager.submit(101, "run delegated task")
    assert pending.status == "waiting_approval"
    assert pending.pending_tool == "agent"

    resolved = manager.resolve_approval(101, approved=True)
    assert resolved.text == "done"
    assert resolved.status == "completed"


def test_remote_manager_can_resume_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    llm = _FakeLLM([LLMResponse(content="finished", prompt_tokens=125)])
    manager = RemoteManager(_config(tmp_path), llm_factory=lambda: llm, tools=[])

    result = manager.submit(202, "finish task")
    assert result.status == "completed"
    assert result.context_used_tokens == 125
    assert result.context_window_tokens == 1_000_000
    session_id = result.session_id

    resumed = manager.resume_session(202, session_id)
    assert resumed.session_id == session_id
    assert resumed.status == "completed"
    assert resumed.context_used_tokens == 125


def test_remote_manager_resume_uses_current_configured_model(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", sessions_dir)
    checkpoint_module.save_checkpoint(
        SessionState(session_id="session_legacy_model", title="legacy conversation"),
        [{"role": "user", "content": "earlier message"}],
        "gpt-5.5",
        workspace_root=str(tmp_path),
    )
    llm = _FakeLLM([LLMResponse(content="continued with current model")])
    manager = RemoteManager(_config(tmp_path), llm_factory=lambda: llm, tools=[])

    resumed = manager.resume_session(202, "session_legacy_model")
    continued = manager.submit(202, "continue the old conversation")

    assert resumed.session_id == "session_legacy_model"
    assert llm.model == "fake-model"
    assert continued.text == "continued with current model"
    loaded = checkpoint_module.load_checkpoint("session_legacy_model")
    assert loaded is not None
    _, messages, saved_model = loaded
    assert messages[-1]["content"] == "continued with current model"
    assert saved_model == "fake-model"


def test_remote_manager_deletes_saved_and_active_session(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path / "sessions")
    manager = RemoteManager(
        _config(tmp_path),
        llm_factory=lambda: _FakeLLM([LLMResponse(content="finished")]),
        tools=[],
    )
    result = manager.submit(202, "first message names the session")

    manager.delete_session(result.session_id)

    assert manager.list_resume_candidates() == []
    try:
        manager.conversation_messages(202)
    except ValueError as exc:
        assert "No chat session yet" in str(exc)
    else:
        raise AssertionError("expected active runtime to be removed")


def test_remote_manager_refuses_checkpoint_from_other_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path / "sessions")
    checkpoint_module.save_checkpoint(
        SessionState(
            session_id="session_other",
            current_task=TaskState(task_id="task_other", status="completed"),
        ),
        [],
        "fake-model",
        workspace_root=str(tmp_path / "other"),
    )
    manager = RemoteManager(
        _config(tmp_path),
        llm_factory=lambda: _FakeLLM([LLMResponse(content="finished")]),
        tools=[],
    )

    try:
        manager.resume_session(202, "session_other")
    except ValueError as exc:
        assert "not available for this workspace" in str(exc)
    else:
        raise AssertionError("expected cross-workspace resume to be rejected")


def test_remote_manager_exposes_bounded_conversation_snapshot(tmp_path):
    llm = _FakeLLM([LLMResponse(content="finished")])
    manager = RemoteManager(_config(tmp_path), llm_factory=lambda: llm, tools=[])
    manager.submit(808, "hello")

    messages = manager.conversation_messages(808)

    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "hello"
    assert messages[1]["content"] == "finished"
    assert messages[0]["turn_id"].startswith("task_")
    assert messages[0]["turn_elapsed_ms"] >= 0


def test_remote_manager_exposes_complete_cli_transcript_and_hides_visual_carriers(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path / "sessions")
    manager = RemoteManager(
        _config(tmp_path),
        llm_factory=lambda: _FakeLLM([LLMResponse(content="finished")]),
        tools=[],
    )
    result = manager.submit(808, "first prompt")
    runtime = manager._require_runtime(808)
    transcript = TranscriptLogger()
    for index in range(120):
        transcript.append_message(
            result.session_id,
            {"role": "assistant", "content": f"history-{index}"},
        )
    transcript.append_message(
        result.session_id,
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Visual content loaded by tools: read."},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,abc"},
                },
            ],
        },
    )

    messages = manager.conversation_messages(808)

    assert messages[0]["content"] == "first prompt"
    assert messages[-1]["content"] == "history-119"
    assert len(messages) == 122
    assert all("Visual content loaded by tools" not in message["content"] for message in messages)


def test_remote_manager_presents_uploaded_attachment_metadata(tmp_path):
    llm = _FakeLLM([LLMResponse(content="finished")])
    manager = RemoteManager(_config(tmp_path), llm_factory=lambda: llm, tools=[])
    runtime = manager._get_or_create_runtime(808)
    runtime.agent.chat(
        "describe upload",
        image_parts=[
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,abc", "detail": "auto"},
            }
        ],
        attachments=[
            {
                "name": "screen.png",
                "path": ".autocode/uploads/screen.png",
                "media_type": "image/png",
                "size": 123,
                "is_image": True,
            }
        ],
    )

    messages = manager.conversation_messages(808)

    assert messages[0]["content"] == "describe upload"
    assert messages[0]["attachments"] == [
        {"name": "screen.png", "media_type": "image/png", "size": 123}
    ]
    checkpoint = checkpoint_module.load_checkpoint(runtime.agent.session_state.session_id)
    assert checkpoint is not None
    assert "base64" not in str(checkpoint[1])


def test_remote_manager_persists_changed_files_on_latest_turn(tmp_path):
    llm = _FakeLLM([LLMResponse(content="finished")])
    manager = RemoteManager(_config(tmp_path), llm_factory=lambda: llm, tools=[])
    manager.submit(808, "create a file")

    manager.annotate_turn_changes(
        808,
        [
            {
                "path": "result.txt",
                "status": "untracked",
                "additions": 2,
                "deletions": 0,
            }
        ],
    )

    messages = manager.conversation_messages(808)
    assert messages[0]["changed_files"] == [
        {
            "path": "result.txt",
            "status": "untracked",
            "additions": 2,
            "deletions": 0,
        }
    ]
    assert messages[1]["changed_files"] == []


def test_remote_manager_exposes_tool_metadata_for_collapsible_work(tmp_path):
    llm = _FakeLLM([
        LLMResponse(
            content="I will delegate this.",
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="agent",
                    arguments={"task": "inspect", "content": "private payload"},
                ),
            ],
        ),
        LLMResponse(content="finished"),
    ])
    manager = RemoteManager(
        _config(tmp_path),
        llm_factory=lambda: llm,
        tools=[_DelegationTool()],
    )

    manager.submit(909, "inspect")
    messages = manager.conversation_messages(909)

    intermediate = messages[1]
    tool_result = messages[2]
    assert intermediate["tool_calls"] == [
        {
            "id": "call_1",
            "name": "agent",
            "arguments": {"task": "inspect"},
        }
    ]
    assert tool_result["tool_call_id"] == "call_1"
    assert tool_result["tool_name"] == "agent"
    assert tool_result["tool_arguments"] == {"task": "inspect"}
    assert "content" not in intermediate["tool_calls"][0]["arguments"]
    assert tool_result["content"] == "delegated:inspect"
    assert messages[-1]["content"] == "finished"


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
    runtime = manager._require_runtime(303)
    closed = []
    runtime.agent.close = lambda: closed.append(True)

    manager.reset_chat(303)
    assert closed == [True]
    try:
        manager.current_task_summary(303)
    except ValueError as exc:
        assert "No chat session yet" in str(exc)
    else:
        raise AssertionError("expected ValueError after reset")


def test_remote_manager_replace_runtime_closes_previous_agent(tmp_path):
    manager = RemoteManager(_config(tmp_path), llm_factory=lambda: _FakeLLM([LLMResponse(content="ok")]), tools=[])
    runtime = manager._get_or_create_runtime(909)
    closed = []
    runtime.agent.close = lambda: closed.append(True)

    replaced = manager._get_or_create_runtime(909, replace=True)

    assert closed == [True]
    assert replaced is not runtime


def test_remote_manager_replace_keeps_shared_observability_alive(tmp_path):
    manager = RemoteManager(_config(tmp_path), tools=[])
    runtime = manager._get_or_create_runtime(909)
    close_calls = []
    runtime.agent.close = lambda **kwargs: close_calls.append(kwargs)

    replaced = manager._get_or_create_runtime(909, replace=True)

    assert close_calls == [{"shutdown_observability": False}]
    assert replaced.agent.llm.tracer is manager._shared_tracer
    manager.close()


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
    manager = _ConfirmingRemoteManager(_config(tmp_path), llm_factory=lambda: llm, tools=[_DelegationTool()])
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

