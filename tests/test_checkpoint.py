from autocode.state import (
    SessionState,
    TaskState,
    delete_session,
    list_sessions,
    load_checkpoint,
    save_checkpoint,
)
from autocode.state import checkpoint as checkpoint_module
from autocode.state import PendingApproval


def test_checkpoint_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)

    state = SessionState(
        session_id="session_demo",
        context_used_tokens=12_345,
        current_task=TaskState(
            task_id="task_demo",
            status="running",
            step_index=3,
            langfuse_trace_id="a" * 32,
            langfuse_root_observation_id="b" * 16,
            pending_approval=PendingApproval(
                tool_call_id="call_1",
                tool_name="bash",
                arguments={"command": "python --version"},
                reason="confirmation required",
                remaining_tool_calls=[{"id": "call_2", "name": "bash", "arguments": {"command": "python -c \"import pika\""}}],
            ),
        ),
    )
    messages = [{"role": "user", "content": "hello"}]
    save_checkpoint(state, messages, "demo-model", workspace_root=str(tmp_path))

    loaded = load_checkpoint("session_demo")
    assert loaded is not None
    loaded_state, loaded_messages, loaded_model = loaded
    assert loaded_state.session_id == "session_demo"
    assert loaded_state.context_used_tokens == 12_345
    assert loaded_state.current_task is not None
    assert loaded_state.current_task.task_id == "task_demo"
    assert loaded_state.current_task.step_index == 3
    assert loaded_state.current_task.langfuse_trace_id == "a" * 32
    assert loaded_state.current_task.langfuse_root_observation_id == "b" * 16
    assert loaded_state.current_task.pending_approval is not None
    assert loaded_state.current_task.pending_approval.remaining_tool_calls[0]["id"] == "call_2"
    assert loaded_messages == messages
    assert loaded_model == "demo-model"


def test_list_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    save_checkpoint(
        SessionState(
            session_id="session_one",
            current_task=TaskState(task_id="task_one", title="fix import", status="waiting_approval", step_index=1),
        ),
        [],
        "m1",
        workspace_root="G:/repo/a",
    )
    save_checkpoint(
        SessionState(
            session_id="session_two",
            current_task=TaskState(task_id="task_two", title="other project", status="completed", step_index=2),
        ),
        [],
        "m2",
        workspace_root="G:/repo/b",
    )
    entries = list_sessions(workspace_root="G:/repo/a")
    assert len(entries) == 1
    assert entries[0]["session_id"] == "session_one"
    assert entries[0]["task_id"] == "task_one"
    assert entries[0]["title"] == "fix import"
    assert entries[0]["status"] == "waiting_approval"


def test_session_title_comes_from_first_user_message_not_latest_task(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    save_checkpoint(
        SessionState(
            session_id="session_named",
            current_task=TaskState(task_id="task_latest", title="latest task"),
        ),
        [
            {"role": "user", "content": "首次问题\n更多内容"},
            {"role": "assistant", "content": "回答"},
            {"role": "user", "content": "后续问题"},
        ],
        "m1",
        workspace_root="G:/repo/a",
    )

    entry = list_sessions(workspace_root="G:/repo/a")[0]
    loaded_state, _, _ = load_checkpoint("session_named")

    assert entry["title"] == "首次问题"
    assert loaded_state.title == "首次问题"


def test_delete_session_enforces_workspace_and_removes_all_records(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path / "sessions")
    save_checkpoint(
        SessionState(session_id="session_delete", title="delete me"),
        [{"role": "user", "content": "delete me"}],
        "m1",
        workspace_root="G:/repo/a",
    )

    try:
        delete_session("session_delete", "G:/repo/b")
    except ValueError as exc:
        assert "not available for this workspace" in str(exc)
    else:
        raise AssertionError("expected cross-workspace delete to be rejected")

    delete_session("session_delete", "G:/repo/a")

    assert not (tmp_path / "sessions" / "session_delete").exists()
    assert list_sessions(workspace_root="G:/repo/a") == []


def test_list_sessions_reuses_unchanged_checkpoint_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    save_checkpoint(
        SessionState(
            session_id="session_cached",
            current_task=TaskState(task_id="task_cached", title="cached"),
        ),
        [],
        "m1",
        workspace_root=str(tmp_path),
    )
    checkpoint_module._CHECKPOINT_CACHE.clear()
    reads = []
    original_read_json = checkpoint_module._read_json

    def counting_read_json(path):
        reads.append(path)
        return original_read_json(path)

    monkeypatch.setattr(checkpoint_module, "_read_json", counting_read_json)

    assert len(list_sessions(workspace_root=str(tmp_path))) == 1
    assert len(list_sessions(workspace_root=str(tmp_path))) == 1
    assert len(reads) == 1


def test_checkpoint_is_written_as_utf8(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    save_checkpoint(
        SessionState(
            session_id="session_utf8",
            current_task=TaskState(task_id="task_utf8", title="帮我执行代码"),
        ),
        [],
        "m1",
        workspace_root=str(tmp_path),
    )
    raw = tmp_path.joinpath("session_utf8/checkpoint.json").read_bytes()
    assert b"\xe5\xb8\xae\xe6\x88\x91" in raw
