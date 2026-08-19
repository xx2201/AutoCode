import json
from concurrent.futures import ThreadPoolExecutor

from autocode.state import (
    SessionState,
    TurnState,
    delete_session,
    list_sessions,
    load_checkpoint,
    save_checkpoint,
)
from autocode.state import checkpoint as checkpoint_module
from autocode.state import session_layout as session_layout_module
from autocode.state import PendingApproval, PendingToolBatch
from autocode.state.journal import AuditLogger, load_events


def test_checkpoint_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)

    state = SessionState(
        session_id="session_demo",
        sandbox_mode="read-only",
        context_used_tokens=12_345,
        context_anchor_messages=7,
        context_anchor_digest="digest-demo",
        current_turn=TurnState(
            turn_id="turn_demo",
            status="running",
            step_index=3,
            langfuse_trace_id="a" * 32,
            langfuse_root_observation_id="b" * 16,
            pending_tool_batch=PendingToolBatch(
                batch_id="batch_1",
                turn_id="turn_demo",
                tool_calls=[
                    {"id": "call_1", "name": "shell_command", "arguments": {"command": "python --version"}},
                    {"id": "call_2", "name": "shell_command", "arguments": {"command": "python -c \"import pika\""}},
                ],
                policy_decisions=[
                    {"action": "ask", "reason": "confirmation required"},
                    {"action": "ask", "reason": "confirmation required"},
                ],
                approvals=[
                    PendingApproval(
                        approval_id="approval_1",
                        tool_call_id="call_1",
                        tool_name="shell_command",
                        arguments={"command": "python --version"},
                        reason="confirmation required",
                    ),
                    PendingApproval(
                        approval_id="approval_2",
                        tool_call_id="call_2",
                        tool_name="shell_command",
                        arguments={"command": "python -c \"import pika\""},
                        reason="confirmation required",
                    ),
                ],
            ),
        ),
    )
    messages = [{"role": "user", "content": "hello"}]
    save_checkpoint(state, messages, "demo-model", workspace_root=str(tmp_path))

    loaded = load_checkpoint("session_demo")
    assert loaded is not None
    loaded_state, loaded_messages, loaded_model = loaded
    assert loaded_state.session_id == "session_demo"
    assert loaded_state.sandbox_mode == "read-only"
    assert loaded_state.context_used_tokens == 12_345
    assert loaded_state.context_anchor_messages == 7
    assert loaded_state.context_anchor_digest == "digest-demo"
    assert loaded_state.current_turn is not None
    assert loaded_state.current_turn.turn_id == "turn_demo"
    assert loaded_state.current_turn.step_index == 3
    assert loaded_state.current_turn.langfuse_trace_id == "a" * 32
    assert loaded_state.current_turn.langfuse_root_observation_id == "b" * 16
    assert loaded_state.current_turn.pending_tool_batch is not None
    assert loaded_state.current_turn.pending_tool_batch.tool_calls[1]["id"] == "call_2"
    assert loaded_state.current_turn.pending_tool_batch.approvals[1].approval_id == "approval_2"
    assert loaded_messages == messages
    assert loaded_model == "demo-model"
    assert not tmp_path.joinpath("session_demo").exists()
    stored = checkpoint_module.session_dir("session_demo")
    assert stored.parent.name == "sessions"
    assert stored.parent.parent.parent == tmp_path / "projects"
    assert stored.joinpath("checkpoint.json").exists()


def test_list_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    save_checkpoint(
        SessionState(
            session_id="session_one",
            current_turn=TurnState(turn_id="turn_one", title="fix import", status="waiting_approval", step_index=1),
        ),
        [],
        "m1",
        workspace_root="G:/repo/a",
    )
    save_checkpoint(
        SessionState(
            session_id="session_two",
            current_turn=TurnState(turn_id="turn_two", title="other project", status="completed", step_index=2),
        ),
        [],
        "m2",
        workspace_root="G:/repo/b",
    )
    entries = list_sessions(workspace_root="G:/repo/a")
    assert len(entries) == 1
    assert entries[0]["session_id"] == "session_one"
    assert entries[0]["turn_id"] == "turn_one"
    assert entries[0]["title"] == "fix import"
    assert entries[0]["status"] == "waiting_approval"


def test_project_directories_use_claude_style_readable_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    save_checkpoint(
        SessionState(session_id="session_readable"),
        [],
        "m1",
        workspace_root="G:/mycode/AutoCoder",
    )

    stored = checkpoint_module.session_dir("session_readable")

    assert stored.parent.parent.name == "G--mycode-AutoCoder"


def test_colliding_readable_project_names_remain_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    for session_id, workspace_root in (
        ("session_flat", "G:/repo/a-b"),
        ("session_nested", "G:/repo/a/b"),
    ):
        save_checkpoint(
            SessionState(session_id=session_id),
            [],
            "m1",
            workspace_root=workspace_root,
        )

    flat = checkpoint_module.session_dir("session_flat")
    nested = checkpoint_module.session_dir("session_nested")

    assert flat.parent.parent != nested.parent.parent
    assert flat.parent.parent.name == "G--repo-a-b"
    assert nested.parent.parent.name.startswith("G--repo-a-b--")
    assert [item["session_id"] for item in list_sessions("G:/repo/a-b")] == [
        "session_flat"
    ]
    assert [item["session_id"] for item in list_sessions("G:/repo/a/b")] == [
        "session_nested"
    ]


def test_session_title_comes_from_first_user_message_not_latest_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    save_checkpoint(
        SessionState(
            session_id="session_named",
            current_turn=TurnState(turn_id="turn_latest", title="latest task"),
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

    stored = checkpoint_module.session_dir("session_delete")
    delete_session("session_delete", "G:/repo/a")

    assert not stored.exists()
    assert not (tmp_path / "sessions" / ".session-locations" / "session_delete.json").exists()
    assert list_sessions(workspace_root="G:/repo/a") == []


def test_list_sessions_reuses_unchanged_checkpoint_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    save_checkpoint(
        SessionState(
            session_id="session_cached",
            current_turn=TurnState(turn_id="turn_cached", title="cached"),
        ),
        [],
        "m1",
        workspace_root=str(tmp_path),
    )
    assert len(list((tmp_path / ".session-locations").glob("*.json"))) == 1
    checkpoint_module._CHECKPOINT_CACHE.clear()
    checkpoint_reads = []
    original_read_json = checkpoint_module._read_json

    def counting_read_json(path):
        if path.name == "checkpoint.json":
            checkpoint_reads.append(path)
        return original_read_json(path)

    monkeypatch.setattr(checkpoint_module, "_read_json", counting_read_json)

    assert len(list_sessions(workspace_root=str(tmp_path))) == 1
    assert len(list_sessions(workspace_root=str(tmp_path))) == 1
    assert len(checkpoint_reads) == 1
    assert checkpoint_reads[0].parent.name == "session_cached"


def test_session_index_avoids_reading_unrelated_workspace_checkpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    save_checkpoint(
        SessionState(session_id="session_target", title="target"),
        [],
        "m1",
        workspace_root="G:/repo/target",
    )
    for index in range(20):
        save_checkpoint(
            SessionState(session_id=f"session_other_{index:02d}", title="other"),
            [],
            "m1",
            workspace_root="G:/repo/other",
        )

    checkpoint_module._CHECKPOINT_CACHE.clear()
    original_read_json = checkpoint_module._read_json

    checkpoint_reads = []

    def count_checkpoint_scan(path):
        if path.name == "checkpoint.json":
            checkpoint_reads.append(path)
        return original_read_json(path)

    monkeypatch.setattr(checkpoint_module, "_read_json", count_checkpoint_scan)

    entries = list_sessions(workspace_root="G:/repo/target")
    assert [entry["session_id"] for entry in entries] == ["session_target"]
    assert [path.parent.name for path in checkpoint_reads] == ["session_target"]


def test_corrupt_session_location_is_rebuilt_from_project_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    save_checkpoint(
        SessionState(session_id="session_recovered", title="recovered"),
        [],
        "m1",
        workspace_root="G:/repo/recovered",
    )
    location_path = tmp_path / ".session-locations" / "session_recovered.json"
    location_path.write_text("not-json", encoding="utf-8")
    session_layout_module._LOCATION_CACHE.clear()

    entries = list_sessions(workspace_root="G:/repo/recovered")
    resolved = checkpoint_module.session_dir("session_recovered")

    assert [entry["session_id"] for entry in entries] == ["session_recovered"]
    assert resolved.name == "session_recovered"
    assert "session_recovered" in location_path.read_text(encoding="utf-8")


def test_checkpoint_is_written_as_utf8(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    save_checkpoint(
        SessionState(
            session_id="session_utf8",
            current_turn=TurnState(turn_id="turn_utf8", title="帮我执行代码"),
        ),
        [],
        "m1",
        workspace_root=str(tmp_path),
    )
    raw = checkpoint_module.session_dir("session_utf8").joinpath("checkpoint.json").read_bytes()
    assert b"\xe5\xb8\xae\xe6\x88\x91" in raw


def test_legacy_flat_sessions_are_moved_into_project_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    legacy = tmp_path / "session_legacy"
    legacy.mkdir(parents=True)
    legacy.joinpath("checkpoint.json").write_text(
        json.dumps(
            {
                "session": {"session_id": "session_legacy"},
                "messages": [{"role": "user", "content": "legacy"}],
                "model": "m1",
                "workspace_root": "g:/repo/legacy",
                "saved_at": "2026-07-29 00:00:00",
            }
        ),
        encoding="utf-8",
    )
    legacy.joinpath("audit.jsonl").write_text('{"event":"kept"}\n', encoding="utf-8")
    old_index = tmp_path / ".workspace-index" / "old"
    old_index.mkdir(parents=True)
    old_index.joinpath("session_legacy.json").write_text("{}", encoding="utf-8")

    entries = list_sessions(workspace_root="G:/repo/legacy")

    assert [entry["session_id"] for entry in entries] == ["session_legacy"]
    assert not legacy.exists()
    assert not (tmp_path / ".workspace-index").exists()
    migrated = checkpoint_module.session_dir("session_legacy")
    assert migrated.joinpath("audit.jsonl").read_text(encoding="utf-8") == '{"event":"kept"}\n'
    assert migrated.parent.name == "sessions"
    project = json.loads(migrated.parent.parent.joinpath("project.json").read_text(encoding="utf-8"))
    assert project["workspace_root"] == "G:/repo/legacy"


def test_hashed_project_directories_are_renamed_to_readable_paths(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    hashed_project = tmp_path / "projects" / ("a" * 64)
    old_session = hashed_project / "sessions" / "session_hashed"
    old_session.mkdir(parents=True)
    hashed_project.joinpath("project.json").write_text(
        json.dumps({"version": 2, "workspace_root": "g:/glm"}),
        encoding="utf-8",
    )
    old_session.joinpath("checkpoint.json").write_text(
        json.dumps(
            {
                "session": {"session_id": "session_hashed"},
                "messages": [{"role": "user", "content": "kept"}],
                "model": "m1",
                "workspace_root": "g:/glm",
                "saved_at": "2026-07-29 00:00:00",
            }
        ),
        encoding="utf-8",
    )
    location = tmp_path / ".session-locations" / "session_hashed.json"
    location.parent.mkdir()
    location.write_text(
        json.dumps(
            {
                "version": 2,
                "session_id": "session_hashed",
                "workspace_root": "g:/glm",
                "relative_path": (
                    "projects/"
                    f"{'a' * 64}/sessions/session_hashed"
                ),
            }
        ),
        encoding="utf-8",
    )

    entries = list_sessions(workspace_root="G:/glm")

    readable = tmp_path / "projects" / "G--glm" / "sessions" / "session_hashed"
    assert [entry["session_id"] for entry in entries] == ["session_hashed"]
    assert readable.joinpath("checkpoint.json").exists()
    assert not hashed_project.exists()
    pointer = json.loads(location.read_text(encoding="utf-8"))
    assert pointer["relative_path"] == "projects/G--glm/sessions/session_hashed"


def test_missing_location_pointer_is_recovered_from_project_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    save_checkpoint(
        SessionState(session_id="session_recover_location"),
        [],
        "m1",
        workspace_root="G:/repo/recover",
    )
    stored = checkpoint_module.session_dir("session_recover_location")
    location = tmp_path / ".session-locations" / "session_recover_location.json"
    location.unlink()
    session_layout_module._LOCATION_CACHE.clear()

    recovered = checkpoint_module.session_dir("session_recover_location")

    assert recovered == stored
    assert location.exists()


def test_partitioned_layout_can_be_restored_to_legacy_flat_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    save_checkpoint(
        SessionState(session_id="session_restore_flat"),
        [{"role": "user", "content": "restore"}],
        "m1",
        workspace_root="G:/repo/restore",
    )

    restored = checkpoint_module.restore_flat_session_layout()

    assert restored == 1
    assert tmp_path.joinpath("session_restore_flat/checkpoint.json").exists()
    assert not (tmp_path / "projects").exists()
    assert not (tmp_path / ".session-locations").exists()


def test_staged_audit_is_moved_with_first_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    AuditLogger().append_event("session_staged", "started", {"value": 1})
    staged = tmp_path / ".staging" / "session_staged"
    assert staged.joinpath("audit.jsonl").exists()

    save_checkpoint(
        SessionState(session_id="session_staged"),
        [],
        "m1",
        workspace_root="G:/repo/staged",
    )

    assert not staged.exists()
    assert load_events("session_staged")[0]["event"] == "started"
    assert checkpoint_module.session_dir("session_staged").parent.name == "sessions"


def test_concurrent_session_creation_keeps_project_partitions_consistent(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)

    def save(index):
        workspace = f"G:/repo/{index % 3}"
        save_checkpoint(
            SessionState(session_id=f"session_concurrent_{index:02d}"),
            [],
            "m1",
            workspace_root=workspace,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save, range(24)))

    assert sum(
        len(list_sessions(workspace_root=f"G:/repo/{index}", limit=100))
        for index in range(3)
    ) == 24
    assert len(list((tmp_path / ".session-locations").glob("*.json"))) == 24


def test_location_pointer_cannot_escape_session_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    save_checkpoint(
        SessionState(session_id="session_safe_location"),
        [],
        "m1",
        workspace_root="G:/repo/safe",
    )
    location = tmp_path / ".session-locations" / "session_safe_location.json"
    location.write_text(
        json.dumps(
            {
                "session_id": "session_safe_location",
                "relative_path": "../../outside",
            }
        ),
        encoding="utf-8",
    )
    session_layout_module._LOCATION_CACHE.clear()

    resolved = checkpoint_module.session_dir("session_safe_location")

    assert tmp_path in resolved.parents
    assert resolved.name == "session_safe_location"
    repaired = json.loads(location.read_text(encoding="utf-8"))
    assert ".." not in repaired["relative_path"]


def test_repeated_session_resolution_uses_memory_location_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    save_checkpoint(
        SessionState(session_id="session_cached_location"),
        [],
        "m1",
        workspace_root="G:/repo/cache",
    )
    session_layout_module._LOCATION_CACHE.clear()
    original_read_json = session_layout_module._read_json
    reads = []

    def counting_read_json(path):
        reads.append(path)
        return original_read_json(path)

    monkeypatch.setattr(session_layout_module, "_read_json", counting_read_json)

    first = checkpoint_module.session_dir("session_cached_location")
    second = checkpoint_module.session_dir("session_cached_location")

    assert first == second
    assert [path.name for path in reads] == ["session_cached_location.json"]
