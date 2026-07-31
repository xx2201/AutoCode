import threading

from autocode.state import checkpoint as checkpoint_module
from autocode.config import Config
from autocode.context import estimate_tokens
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

    resolved = manager.resolve_next_approval(101, approved=True)
    assert resolved.text == "done"
    assert resolved.status == "completed"


def test_remote_manager_can_resume_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    llm = _FakeLLM([LLMResponse(content="finished", prompt_tokens=125)])
    manager = RemoteManager(_config(tmp_path), llm_factory=lambda: llm, tools=[])

    result = manager.submit(202, "finish task")
    assert result.status == "completed"
    expected_tokens = 125 + estimate_tokens([
        {"role": "assistant", "content": "finished"},
    ])
    assert result.context_used_tokens == expected_tokens
    assert result.context_window_tokens == 1_000_000
    session_id = result.session_id

    resumed = manager.resume_session(202, session_id)
    assert resumed.session_id == session_id
    assert resumed.status == "completed"
    assert resumed.context_used_tokens == expected_tokens


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
    stored_user = checkpoint[1][0]
    assert stored_user["model_content"][0]["type"] == "image_url"
    assert "model_content" not in messages[0]
    assert "base64" not in str(messages)


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
            "pending_tool": "shell_command",
            "pending_reason": "confirmation required",
            "pending_arguments": {"command": "python app.py"},
            "pending_requires_manual": False,
            "pending_approval_scope": "tool:shell_command",
            "pending_approval_label": "本任务允许运行 shell_command",
            "permission_mode": "ask",
        })()
    )
    assert "/approve" in text
    assert "/approve_scope" in text
    assert "session_123" in text
    assert "task_123" in text
    assert "python app.py" in text


def test_split_message_respects_limit():
    chunks = split_message("a" * 5000, limit=1000)
    assert len(chunks) > 1
    assert all(len(chunk) <= 1000 for chunk in chunks)


def test_remote_manager_scope_approval_marks_task_grant(tmp_path):
    llm = _FakeLLM([
        LLMResponse(content="", tool_calls=[ToolCall(id="1", name="agent", arguments={"task": "inspect"})]),
        LLMResponse(content="done"),
    ])
    manager = _ConfirmingRemoteManager(_config(tmp_path), llm_factory=lambda: llm, tools=[_DelegationTool()])
    manager.submit(404, "run delegated task")

    result = manager.resolve_next_approval(404, approved=True, grant_scope=True)
    assert result.permission_mode == "ask"
    summary = manager.current_task_summary(404)
    assert "Permission mode: ask" in summary


def test_replacing_busy_runtime_fails_fast_instead_of_blocking(tmp_path):
    manager = RemoteManager(
        _config(tmp_path),
        llm_factory=lambda: _FakeLLM([]),
        tools=[],
    )
    runtime = manager._get_or_create_runtime(505)
    locked = threading.Event()
    release = threading.Event()

    def hold_runtime():
        with runtime.lock:
            locked.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_runtime)
    holder.start()
    assert locked.wait(timeout=2)
    try:
        try:
            manager._get_or_create_runtime(505, replace=True)
        except ValueError as exc:
            assert "仍在执行任务" in str(exc)
        else:
            raise AssertionError("busy runtime replacement must fail immediately")
    finally:
        release.set()
        holder.join(timeout=2)
        manager.close()


def test_current_task_summary_does_not_wait_for_busy_runtime(tmp_path):
    manager = RemoteManager(
        _config(tmp_path),
        llm_factory=lambda: _FakeLLM([]),
        tools=[],
    )
    runtime = manager._get_or_create_runtime(506)
    locked = threading.Event()
    release = threading.Event()

    def hold_runtime():
        with runtime.lock:
            locked.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_runtime)
    holder.start()
    assert locked.wait(timeout=2)
    try:
        summary = manager.current_task_summary(506)
        assert summary == "Current task is still running; detailed status is temporarily unavailable."
    finally:
        release.set()
        holder.join(timeout=2)
        manager.close()


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

    with manager._temporary_hook(runtime.agent, _hook):
        runtime.agent.hooks.emit("assistant_step", {"content": "inspect", "tool_calls": []})
    assert "assistant_step" in events
    assert _hook not in runtime.agent.hooks._handlers.get("assistant_step", [])


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


def test_remote_manager_edit_last_turn_preserves_session_and_exposes_revision_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path / "sessions")
    llm = _FakeLLM([LLMResponse(content="first"), LLMResponse(content="revised")])
    manager = RemoteManager(_config(tmp_path), llm_factory=lambda: llm, tools=[])
    first = manager.submit(707, "original")

    revised = manager.edit_last_turn(707, first.task_id, "edited")
    messages = manager.conversation_messages(707)

    assert revised.session_id == first.session_id
    assert revised.task_id != first.task_id
    assert [message["content"] for message in messages] == ["edited", "revised"]
    assert all(message["message_id"] for message in messages)
    assert all(message["turn_id"] == revised.task_id for message in messages)
    assert all(message["revision_id"] for message in messages)
    assert messages[0]["message_kind"] == "prompt"


def test_remote_manager_edit_last_turn_keeps_earlier_turn_and_annotates_replacement(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path / "sessions")
    llm = _FakeLLM([
        LLMResponse(content="first answer"),
        LLMResponse(content="second answer"),
        LLMResponse(content="replacement answer"),
    ])
    manager = RemoteManager(_config(tmp_path), llm_factory=lambda: llm, tools=[])
    first = manager.submit(708, "first prompt")
    second = manager.submit(708, "second prompt")

    replacement = manager.edit_last_turn(708, second.task_id, "replacement prompt")
    messages = manager.conversation_messages(708)

    assert [message["content"] for message in messages] == [
        "first prompt",
        "first answer",
        "replacement prompt",
        "replacement answer",
    ]
    assert [message["turn_id"] for message in messages[:2]] == [first.task_id, first.task_id]
    assert [message["turn_id"] for message in messages[2:]] == [replacement.task_id, replacement.task_id]
    assert messages[2]["turn_elapsed_ms"] >= 0


def test_remote_manager_accepts_steer_and_queue_while_submit_holds_agent_lock(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class _BlockingLLM:
        model = "fake-model"
        total_prompt_tokens = 0
        total_completion_tokens = 0

        def __init__(self):
            self.calls = 0

        def chat(self, messages, tools=None, on_token=None):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                assert release.wait(timeout=5)
                return LLMResponse(content="draft")
            assert any(
                message.get("role") == "user"
                and message.get("content") == "guide the active answer"
                for message in messages
            )
            return LLMResponse(content="guided answer")

    llm = _BlockingLLM()
    manager = RemoteManager(_config(tmp_path), llm_factory=lambda: llm, tools=[])
    result_holder = []
    worker = threading.Thread(target=lambda: result_holder.append(manager.submit(818, "start")))
    worker.start()
    assert entered.wait(timeout=5)
    active_turn_id = manager._require_runtime(818).agent.task_state.task_id

    steer = manager.steer(818, active_turn_id, "guide the active answer")
    queued = manager.enqueue_followup(818, active_turn_id, "answer this next")
    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert steer["mode"] == "steer"
    assert queued["mode"] == "queue"
    assert result_holder[0].text == "guided answer"
    assert manager.queued_followups(818)[0]["content"] == "answer this next"
    assert manager.pop_queued_followup(818).message_id == queued["message_id"]
    messages = manager.conversation_messages(818)
    assert [message["message_kind"] for message in messages] == [
        "prompt",
        "assistant",
        "steer",
        "assistant",
    ]


def test_remote_manager_rejects_stale_expected_turn_id(tmp_path):
    manager = RemoteManager(
        _config(tmp_path),
        llm_factory=lambda: _FakeLLM([LLMResponse(content="done")]),
        tools=[],
    )
    result = manager.submit(919, "complete immediately")

    try:
        manager.steer(919, result.task_id, "too late")
    except ValueError as exc:
        assert "no active turn" in str(exc).lower()
    else:
        raise AssertionError("expected completed turn steer to fail")


def test_remote_manager_emits_turn_started_before_model_response(tmp_path):
    events = []
    manager = RemoteManager(
        _config(tmp_path),
        llm_factory=lambda: _FakeLLM([LLMResponse(content="done")]),
        tools=[],
    )

    result = manager.submit(929, "start", hook_handler=lambda event, payload: events.append((event, payload)))
    started = next(payload for event, payload in events if event == "turn_started")

    assert started["turn_id"] == result.task_id
    assert started["task_id"] == result.task_id
    assert started["revision_id"]


def test_queued_followup_survives_checkpoint_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path / "sessions")
    entered = threading.Event()
    release = threading.Event()

    class _BlockingOnceLLM:
        model = "fake-model"
        total_prompt_tokens = 0
        total_completion_tokens = 0

        def chat(self, messages, tools=None, on_token=None):
            entered.set()
            assert release.wait(timeout=5)
            return LLMResponse(content="done")

    manager = RemoteManager(_config(tmp_path), llm_factory=_BlockingOnceLLM, tools=[])
    results = []
    worker = threading.Thread(target=lambda: results.append(manager.submit(939, "start")))
    worker.start()
    assert entered.wait(timeout=5)
    active_turn = manager._require_runtime(939).agent.task_state.task_id
    queued = manager.enqueue_followup(939, active_turn, "persist me")
    release.set()
    worker.join(timeout=5)
    session_id = results[0].session_id

    resumed = RemoteManager(
        _config(tmp_path),
        llm_factory=lambda: _FakeLLM([LLMResponse(content="unused")]),
        tools=[],
    )
    resumed.resume_session(940, session_id)

    restored = resumed.queued_followups(940)
    assert restored[0]["message_id"] == queued["message_id"]
    assert restored[0]["content"] == "persist me"

    assert resumed.pop_queued_followup(940).message_id == queued["message_id"]
    restarted = RemoteManager(
        _config(tmp_path),
        llm_factory=lambda: _FakeLLM([LLMResponse(content="unused")]),
        tools=[],
    )
    restarted.resume_session(941, session_id)
    assert restarted.queued_followups(941) == []


def test_legacy_checkpoint_message_ids_are_persisted_on_first_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path / "sessions")
    checkpoint_module.save_checkpoint(
        SessionState(
            session_id="session_legacy_ids",
            current_task=TaskState(task_id="task_latest", status="completed"),
        ),
        [
            {"role": "user", "content": "legacy prompt"},
            {"role": "assistant", "content": "legacy answer"},
        ],
        "old-model",
        workspace_root=str(tmp_path),
    )
    first = RemoteManager(
        _config(tmp_path),
        llm_factory=lambda: _FakeLLM([LLMResponse(content="unused")]),
        tools=[],
    )
    first.resume_session(951, "session_legacy_ids")
    first_messages = first.conversation_messages(951)

    second = RemoteManager(
        _config(tmp_path),
        llm_factory=lambda: _FakeLLM([LLMResponse(content="unused")]),
        tools=[],
    )
    second.resume_session(952, "session_legacy_ids")
    second_messages = second.conversation_messages(952)

    assert [message["message_id"] for message in second_messages] == [
        message["message_id"] for message in first_messages
    ]
    assert all(message["turn_id"] == "task_latest" for message in second_messages)
    assert all(message["revision_id"] for message in second_messages)

