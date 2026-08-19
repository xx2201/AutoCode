import base64
import subprocess
from dataclasses import replace

import pytest

pytest.importorskip("httpx")

from autocode.config import Config
from autocode.remote.manager import RemoteTurnResult
from autocode.web import runner as runner_module
from autocode.web.runner import LocalRunner, RunnerSettings, changed_git_files
from autocode.workspaces import WorkspaceRegistry


class _FakeManager:
    def __init__(self, workspace):
        self.config = Config(
            model="fake-model",
            api_key="secret",
            workspace_root=str(workspace),
        )
        self.result = RemoteTurnResult(
            text="done",
            session_id="session_1",
            turn_id="turn_1",
            status="completed",
        )
        self.calls = []
        self.changed_files = []
        self.active_session_id = ""

    def list_resume_candidates(self, limit=10):
        return [{"session_id": "session_1"}][:limit]

    def submit(self, client_id, prompt, hook_handler=None, on_token=None, attachments=None, permission_preset=None):
        self.calls.append(("chat", client_id, prompt))
        if not self.active_session_id:
            self.active_session_id = self.result.session_id
        return replace(self.result, session_id=self.active_session_id)

    def decide_approval(self, client_id, approval_id, action, expected_turn_id, batch_id):
        self.calls.append(("approval_decision", client_id, approval_id, action, expected_turn_id, batch_id))
        return {"batch_id": batch_id, "turn_id": expected_turn_id, "ready": True}

    def continue_approval_batch(
        self,
        client_id,
        expected_turn_id,
        batch_id,
        hook_handler=None,
        on_token=None,
        on_tool=None,
    ):
        self.calls.append(("continue_turn", client_id, expected_turn_id, batch_id))
        return replace(self.result, text="approved")

    def annotate_turn_changes(self, client_id, changed_files):
        self.changed_files.extend(changed_files)

    def resume_session(self, client_id, session_id):
        self.calls.append(("resume", client_id, session_id))
        self.active_session_id = session_id
        return replace(self.result, text="resumed")

    def current_session_id(self, client_id):
        if not self.active_session_id:
            raise ValueError("No chat session yet.")
        return self.active_session_id

    def set_permission_preset(self, client_id, permission_preset):
        self.calls.append(("permission_preset", client_id, permission_preset))
        return {
            "permission_preset": permission_preset,
            "approval_policy": "never" if permission_preset == "danger-full-access" else "ask",
            "sandbox_mode": permission_preset,
        }

    def delete_session(self, session_id):
        self.calls.append(("delete_session", session_id))

    def conversation_messages(self, client_id):
        return [{"role": "user", "content": "hello", "tool_call_id": ""}]

    def current_turn_summary(self, client_id):
        return "Status: completed"

    def current_trace(self, client_id):
        return "LLM calls: 1"

    def reset_chat(self, client_id):
        self.calls.append(("reset", client_id))

    def close(self):
        return None


def _runner(tmp_path):
    project = tmp_path / "project-a"
    project.mkdir()
    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    workspace = registry.register(project)
    managers = {}

    def manager_factory(workspace_path):
        manager = _FakeManager(workspace_path)
        managers[str(workspace_path)] = manager
        return manager

    settings = RunnerSettings(
        relay_url="https://relay.example",
        token="runner-token-that-is-long-enough",
        ca_cert=str(tmp_path / "unused.pem"),
    )
    runner = LocalRunner(
        settings,
        config=Config(model="fake-model", api_key="secret"),
        registry=registry,
        manager_factory=manager_factory,
        client=object(),
    )
    return runner, workspace, managers


def test_runner_bootstrap_lists_only_cli_registered_workspaces(tmp_path):
    runner, workspace, _ = _runner(tmp_path)
    unregistered = tmp_path / "project-b"
    unregistered.mkdir()

    result = runner.execute("bootstrap", {})

    assert result["model"] == "fake-model"
    assert result["provider"] == "anthropic"
    assert result["api_format"] == "messages"
    assert result["context_window_tokens"] == 1_000_000
    assert result["capabilities"]["file_download"] is True
    assert result["capabilities"]["git_workspace"] is True
    assert result["capabilities"]["web_search"] is False
    assert [item["workspace_id"] for item in result["workspaces"]] == [
        workspace.workspace_id
    ]
    assert all(item["path"] != str(unregistered) for item in result["workspaces"])


def test_runner_bootstrap_reports_configured_web_search(tmp_path):
    runner, _, _ = _runner(tmp_path)
    runner._base_config = replace(
        runner._base_config,
        tavily_api_key="tvly-test",
    )

    result = runner.execute("bootstrap", {})

    assert result["capabilities"]["web_search"] is True


def test_changed_git_files_returns_only_entries_changed_during_turn():
    unchanged = {
        "path": "existing.py",
        "status": "modified",
        "index_status": " ",
        "worktree_status": "M",
        "staged": False,
        "unstaged": True,
        "additions": 2,
        "deletions": 0,
    }
    before = {"available": True, "changes": [unchanged]}
    after = {
        "available": True,
        "changes": [
            unchanged,
            {
                "path": "created.txt",
                "status": "untracked",
                "index_status": "?",
                "worktree_status": "?",
                "staged": False,
                "unstaged": True,
                "additions": 3,
                "deletions": 0,
            },
        ],
    }

    assert changed_git_files(before, after) == [
        {
            "path": "created.txt",
            "status": "untracked",
            "additions": 3,
            "deletions": 0,
        }
    ]


def test_changed_git_files_detects_same_size_untracked_rewrite():
    before_item = {
        "path": "draft.txt",
        "status": "untracked",
        "index_status": "?",
        "worktree_status": "?",
        "staged": False,
        "unstaged": True,
        "additions": 1,
        "deletions": 0,
        "_worktree_fingerprint": (5, 100),
    }
    after_item = {**before_item, "_worktree_fingerprint": (5, 200)}

    assert changed_git_files(
        {"available": True, "changes": [before_item]},
        {"available": True, "changes": [after_item]},
    ) == [
        {
            "path": "draft.txt",
            "status": "untracked",
            "additions": 1,
            "deletions": 0,
        }
    ]


def test_runner_executes_chat_approval_decision_and_continuation(tmp_path):
    runner, workspace, managers = _runner(tmp_path)
    payload = {
        "workspace_id": workspace.workspace_id,
        "client_id": "web_12345678",
    }

    chat = runner.execute("chat", {**payload, "prompt": "inspect project"})
    decision = runner.execute(
        "approval_decision",
        {
            **payload,
            "approval_id": "approval_1",
            "approval_action": "approve_scope",
            "expected_turn_id": "turn_1",
            "batch_id": "batch_1",
        },
    )
    approval = runner.execute(
        "continue_turn",
        {
            **payload,
            "expected_turn_id": "turn_1",
            "batch_id": "batch_1",
        },
    )
    manager = managers[str((tmp_path / "project-a").resolve())]

    assert chat["status"] == "completed"
    assert chat["files"] == []
    assert decision["ready"] is True
    assert approval["text"] == "approved"
    assert ("chat", "web_12345678", "inspect project") in manager.calls
    assert (
        "approval_decision",
        "web_12345678",
        "approval_1",
        "approve_scope",
        "turn_1",
        "batch_1",
    ) in manager.calls
    assert ("continue_turn", "web_12345678", "turn_1", "batch_1") in manager.calls


def test_runner_restores_explicit_session_before_chat_after_restart(tmp_path):
    runner, workspace, managers = _runner(tmp_path)
    payload = {
        "workspace_id": workspace.workspace_id,
        "client_id": "web_12345678",
        "session_id": "session_existing",
        "prompt": "continue the prior conversation",
    }

    first = runner.execute("chat", payload)
    manager = managers[str((tmp_path / "project-a").resolve())]

    assert first["session_id"] == "session_existing"
    assert manager.calls[:2] == [
        ("resume", "web_12345678", "session_existing"),
        ("chat", "web_12345678", "continue the prior conversation"),
    ]

    runner.execute("chat", {**payload, "prompt": "continue again"})

    assert [call for call in manager.calls if call[0] == "resume"] == [
        ("resume", "web_12345678", "session_existing")
    ]


def test_runner_captures_undo_and_reapply_for_one_turn(tmp_path, monkeypatch):
    from autocode.state.changes import ChangeSetStore as RealChangeSetStore

    runner, workspace, _ = _runner(tmp_path)
    project = tmp_path / "project-a"
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    (project / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=project, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=AutoCode Test",
            "-c",
            "user.email=autocode@example.invalid",
            "commit",
            "-m",
            "seed",
        ],
        cwd=project,
        check=True,
        capture_output=True,
    )
    changes_root = tmp_path / "turn-changes"
    monkeypatch.setattr(
        "autocode.web.runner.ChangeSetStore",
        lambda root, session_id: RealChangeSetStore(
            root,
            session_id,
            changes_root=changes_root,
        ),
    )
    manager = runner._manager(workspace.workspace_id)
    manager.current_session_id = lambda client_id: "session_1"

    def submit(client_id, prompt, hook_handler=None, on_token=None, permission_preset=None):
        hook_handler(
            "turn_started",
            {
                "session_id": "session_1",
                "turn_id": "turn_1",
                "revision_id": "revision_1",
            },
        )
        (project / "created.txt").write_text("created\n", encoding="utf-8")
        return manager.result

    manager.submit = submit
    payload = {
        "workspace_id": workspace.workspace_id,
        "client_id": "web_12345678",
    }
    chat = runner.execute("chat", {**payload, "prompt": "create it"})

    assert chat["changed_files"][0]["turn_id"] == "turn_1"
    assert chat["changed_files"][0]["can_undo"] is True
    undone = runner.execute(
        "change_action",
        {**payload, "turn_id": "turn_1", "change_action": "undo"},
    )
    assert undone["state"] == "undone"
    assert not (project / "created.txt").exists()
    reapplied = runner.execute(
        "change_action",
        {**payload, "turn_id": "turn_1", "change_action": "reapply"},
    )
    assert reapplied["state"] == "applied"
    assert (project / "created.txt").read_text(encoding="utf-8") == "created\n"


def test_runner_continues_when_undo_capture_before_exceeds_limit(tmp_path, monkeypatch):
    from autocode.state.changes import ChangeSetLimitError

    runner, workspace, _ = _runner(tmp_path)
    project = tmp_path / "project-a"
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    warnings = []

    class _Store:
        def __init__(self, root, session_id):
            pass

        def capture_before(self, turn_id):
            raise ChangeSetLimitError("Workspace contains 11677 files; limit is 10000.")

    monkeypatch.setattr("autocode.web.runner.ChangeSetStore", _Store)
    monkeypatch.setattr(
        runner_module,
        "log_event",
        lambda logger, level, message, **fields: warnings.append((message, fields)),
    )
    manager = runner._manager(workspace.workspace_id)

    def submit(client_id, prompt, hook_handler=None, on_token=None, permission_preset=None):
        hook_handler(
            "turn_started",
            {
                "session_id": "session_1",
                "turn_id": "turn_1",
                "revision_id": "revision_1",
            },
        )
        (project / "created.txt").write_text("created\n", encoding="utf-8")
        return manager.result

    manager.submit = submit
    chat = runner.execute(
        "chat",
        {
            "workspace_id": workspace.workspace_id,
            "client_id": "web_12345678",
            "prompt": "create it",
        },
    )

    assert chat["status"] == "completed"
    assert chat["changed_files"][0]["path"] == "created.txt"
    assert "can_undo" not in chat["changed_files"][0]
    assert warnings == [
        (
            "Per-turn Undo unavailable",
            {
                "phase": "capture_before",
                "error_type": "ChangeSetLimitError",
                "error": "Workspace contains 11677 files; limit is 10000.",
            },
        )
    ]


def test_runner_continues_when_undo_capture_after_exceeds_limit(tmp_path, monkeypatch):
    from autocode.state.changes import ChangeSetLimitError

    runner, workspace, _ = _runner(tmp_path)
    project = tmp_path / "project-a"
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    warnings = []

    class _Store:
        def __init__(self, root, session_id):
            pass

        def capture_before(self, turn_id):
            return object()

        def capture_after(self, turn_id, before):
            raise ChangeSetLimitError("Workspace contains 11677 files; limit is 10000.")

    monkeypatch.setattr("autocode.web.runner.ChangeSetStore", _Store)
    monkeypatch.setattr(
        runner_module,
        "log_event",
        lambda logger, level, message, **fields: warnings.append((message, fields)),
    )
    manager = runner._manager(workspace.workspace_id)

    def submit(client_id, prompt, hook_handler=None, on_token=None, permission_preset=None):
        hook_handler(
            "turn_started",
            {
                "session_id": "session_1",
                "turn_id": "turn_1",
                "revision_id": "revision_1",
            },
        )
        (project / "created.txt").write_text("created\n", encoding="utf-8")
        return manager.result

    manager.submit = submit
    chat = runner.execute(
        "chat",
        {
            "workspace_id": workspace.workspace_id,
            "client_id": "web_12345678",
            "prompt": "create it",
        },
    )

    assert chat["status"] == "completed"
    assert chat["changed_files"][0]["path"] == "created.txt"
    assert "can_undo" not in chat["changed_files"][0]
    assert warnings == [
        (
            "Per-turn Undo unavailable",
            {
                "phase": "capture_after",
                "error_type": "ChangeSetLimitError",
                "error": "Workspace contains 11677 files; limit is 10000.",
            },
        )
    ]


def test_runner_converts_tool_hooks_to_work_events(tmp_path):
    runner, workspace, managers = _runner(tmp_path)
    manager = runner._manager(workspace.workspace_id)

    def submit(client_id, prompt, hook_handler=None, on_token=None, permission_preset=None):
        manager.calls.append(("chat", client_id, prompt))
        if on_token:
            on_token("I will inspect the repository.")
        hook_handler(
            "assistant_step",
            {
                "step_index": 1,
                "content": "I will inspect the repository.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "shell_command",
                        "arguments": {"command": "git status"},
                    }
                ],
            },
        )
        hook_handler(
            "before_tool",
            {
                "tool_call_id": "call_1",
                "tool_name": "shell_command",
                "arguments": {"command": "git status"},
            },
        )
        hook_handler(
            "after_tool",
            {
                "tool_call_id": "call_1",
                "tool_name": "shell_command",
                "arguments": {"command": "git status"},
                "result": "clean",
                "duration_ms": 1250,
                "success": True,
            },
        )
        return manager.result

    manager.submit = submit
    events = []
    runner.execute(
        "chat",
        {
            "workspace_id": workspace.workspace_id,
            "client_id": "web_12345678",
            "prompt": "inspect project",
        },
        event_handler=events.append,
    )

    work_events = [event for event in events if event["type"] == "work"]
    assert work_events == [
        {
            "type": "work",
            "phase": "narrative",
            "work_id": "step-1-narrative",
            "content": "I will inspect the repository.",
        },
        {
            "type": "work",
            "phase": "planned",
            "tool_call_id": "call_1",
            "tool_name": "shell_command",
            "arguments": {"command": "git status"},
        },
        {
            "type": "work",
            "phase": "started",
            "tool_call_id": "call_1",
            "tool_name": "shell_command",
            "arguments": {"command": "git status"},
        },
        {
            "type": "work",
            "phase": "completed",
            "tool_call_id": "call_1",
            "tool_name": "shell_command",
            "arguments": {"command": "git status"},
            "output": "clean",
            "duration_ms": 1250.0,
            "success": True,
        },
    ]


def test_runner_emits_consumed_steer_event(tmp_path):
    runner, _, _ = _runner(tmp_path)
    events = []

    runner._emit_hook_event(
        events.append,
        "user_message",
        {
            "message_kind": "steer",
            "message_id": "steer-1",
            "turn_id": "turn-1",
            "content": "focus on tests",
        },
    )

    assert events == [
        {
            "type": "turn_message",
            "phase": "consumed",
            "mode": "steer",
            "message_id": "steer-1",
            "turn_id": "turn-1",
            "content": "focus on tests",
        }
    ]


def test_runner_emits_completed_snapshot_before_starting_queued_turn(tmp_path):
    runner, workspace, _ = _runner(tmp_path)
    manager = runner._manager(workspace.workspace_id)
    followups = [
        type(
            "Followup",
            (),
            {"message_id": "queue-1", "content": "second question"},
        )()
    ]
    completed_messages = [
        {"role": "user", "content": "first question", "turn_id": "turn-1"},
        {"role": "assistant", "content": "first answer", "turn_id": "turn-1"},
    ]

    def submit(client_id, prompt, hook_handler=None, on_token=None, permission_preset=None):
        turn_id = "turn-1" if prompt == "first question" else "turn-2"
        hook_handler(
            "turn_started",
            {
                "session_id": "session-1",
                "turn_id": turn_id,
                "revision_id": f"revision-{turn_id}",
            },
        )
        return replace(manager.result, text=f"answer for {prompt}", turn_id=turn_id)

    manager.submit = submit
    manager.pop_queued_followup = lambda client_id: followups.pop(0) if followups else None
    manager.conversation_messages = lambda client_id: completed_messages
    events = []

    runner.execute(
        "chat",
        {
            "workspace_id": workspace.workspace_id,
            "client_id": "web_12345678",
            "prompt": "first question",
        },
        event_handler=events.append,
    )

    queued_starting = next(
        event
        for event in events
        if event.get("type") == "turn" and event.get("phase") == "queued_starting"
    )
    queued_started = next(
        event
        for event in events
        if event.get("type") == "turn"
        and event.get("phase") == "started"
        and event.get("queued")
    )
    assert queued_starting == {
        "type": "turn",
        "phase": "queued_starting",
        "message_id": "queue-1",
        "content": "second question",
        "completed_turn_id": "turn-1",
        "messages": completed_messages,
    }
    assert queued_started["turn_id"] == "turn-2"
    assert queued_started["message_id"] == "queue-1"
    assert queued_started["content"] == "second question"


def test_runner_routes_session_delete_to_workspace_manager(tmp_path):
    runner, workspace, managers = _runner(tmp_path)

    result = runner.execute(
        "delete_session",
        {
            "workspace_id": workspace.workspace_id,
            "client_id": "web_12345678",
            "session_id": "session_1",
        },
    )
    manager = managers[str((tmp_path / "project-a").resolve())]

    assert result == {"deleted": True, "session_id": "session_1"}
    assert manager.calls == [("delete_session", "session_1")]


def test_runner_rejects_unregistered_workspace(tmp_path):
    runner, _, _ = _runner(tmp_path)

    with pytest.raises(ValueError, match="not registered by the local CLI"):
        runner.execute(
            "chat",
            {
                "workspace_id": "00000000000000000000",
                "client_id": "web_12345678",
                "prompt": "inspect project",
            },
        )


def test_runner_isolates_managers_between_registered_workspaces(tmp_path):
    runner, first, managers = _runner(tmp_path)
    second_path = tmp_path / "project-b"
    second_path.mkdir()
    second = runner.registry.register(second_path)

    runner.execute(
        "chat",
        {
            "workspace_id": first.workspace_id,
            "client_id": "web_12345678",
            "prompt": "first",
        },
    )
    runner.execute(
        "chat",
        {
            "workspace_id": second.workspace_id,
            "client_id": "web_12345678",
            "prompt": "second",
        },
    )

    assert len(managers) == 2
    assert managers[str((tmp_path / "project-a").resolve())].calls[0][-1] == "first"
    assert managers[str(second_path.resolve())].calls[0][-1] == "second"


def test_runner_rejects_unknown_action(tmp_path):
    runner, workspace, _ = _runner(tmp_path)

    with pytest.raises(ValueError, match="Unknown relay action"):
        runner.execute("unknown", {"workspace_id": workspace.workspace_id})


def test_runner_routes_git_status_without_creating_agent_manager(tmp_path):
    runner, workspace, managers = _runner(tmp_path)

    result = runner.execute(
        "git_status",
        {"workspace_id": workspace.workspace_id},
    )

    assert result["available"] is False
    assert managers == {}


def test_runner_lists_and_reads_workspace_files_without_agent_manager(tmp_path):
    runner, workspace, managers = _runner(tmp_path)
    source = tmp_path / "project-a" / "app.py"
    source.write_text("print('ok')", encoding="utf-8")

    listing = runner.execute(
        "workspace_files",
        {"workspace_id": workspace.workspace_id},
    )
    opened = runner.execute(
        "workspace_file",
        {"workspace_id": workspace.workspace_id, "path": "app.py"},
    )

    assert listing["files"] == ["app.py"]
    assert opened["content"] == "print('ok')"
    assert managers == {}


def test_runner_downloads_only_an_offered_workspace_file(tmp_path):
    runner, workspace, _ = _runner(tmp_path)
    artifact = tmp_path / "project-a" / "report.pdf"
    artifact.write_bytes(b"%PDF-runner")
    offered = runner._web_files.offer(
        workspace.workspace_id,
        artifact.parent,
        str(artifact),
    )

    result = runner.execute(
        "download",
        {
            "workspace_id": workspace.workspace_id,
            "file_id": offered["file_id"],
        },
    )

    assert result["name"] == "report.pdf"
    assert base64.b64decode(result["data_base64"]) == b"%PDF-runner"


def test_runner_collects_files_offered_during_active_web_turn(tmp_path):
    runner, workspace, _ = _runner(tmp_path)
    artifact = tmp_path / "project-a" / "result.txt"
    artifact.write_text("complete", encoding="utf-8")

    with runner._capture_output_files(workspace.workspace_id) as files:
        message = runner._offer_web_file(artifact.parent, "result.txt")

    assert message == "Attached result.txt to the current Web response."
    assert files[0]["name"] == "result.txt"
    assert files[0]["can_preview"] is False


def test_runner_drops_result_when_relay_job_expired(tmp_path):
    class _ExpiredResponse:
        status_code = 404

        def raise_for_status(self):
            raise AssertionError("404 must be handled before raise_for_status")

    class _ExpiredClient:
        def __init__(self):
            self.posts = []

        def post(self, path, json):
            self.posts.append((path, json))
            return _ExpiredResponse()

    runner, workspace, managers = _runner(tmp_path)
    client = _ExpiredClient()
    runner.client = client

    runner._run_job(
        {
            "job_id": "expired-job",
            "action": "chat",
            "payload": {
                "workspace_id": workspace.workspace_id,
                "client_id": "web_12345678",
                "prompt": "hello",
            },
        }
    )

    manager = managers[str((tmp_path / "project-a").resolve())]
    assert len(client.posts) == 1
    assert manager.calls == [("chat", "web_12345678", "hello")]


def test_runner_builds_isolated_relay_clients(tmp_path, monkeypatch):
    class _Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False

        def close(self):
            self.closed = True

    clients = []

    def build_client(**kwargs):
        client = _Client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(runner_module.ssl, "create_default_context", lambda **_: object())
    monkeypatch.setattr(runner_module.httpx, "Client", build_client)
    settings = RunnerSettings(
        relay_url="https://relay.example",
        token="runner-token-that-is-long-enough",
        ca_cert=str(tmp_path / "relay-ca.pem"),
    )
    runner = LocalRunner(
        settings,
        config=Config(model="fake-model", api_key="secret"),
        registry=WorkspaceRegistry(tmp_path / "workspaces.json"),
    )

    assert len(clients) == 3
    assert runner.client is clients[0]
    assert runner._poll_client is clients[1]
    assert runner._heartbeat_client is clients[2]

    runner.close()
    assert all(client.closed for client in clients)


def test_runner_watchdog_exits_when_all_idle_connections_are_stale(
    tmp_path, monkeypatch
):
    runner, _, _ = _runner(tmp_path)
    exits = []
    monkeypatch.setattr(runner_module, "log_event", lambda *args, **kwargs: None)
    runner._fatal_exit = exits.append
    runner._last_heartbeat_success_at = 0.0
    runner._last_poll_success_at = 0.0

    expired = runner._check_liveness(now=runner.settings.watchdog_timeout + 1.0)

    assert expired is True
    assert exits == [1]


@pytest.mark.parametrize(
    ("heartbeat_age", "poll_age"),
    [
        (121.0, 14.0),
        (14.0, 121.0),
    ],
)
def test_runner_watchdog_keeps_running_when_one_relay_channel_is_healthy(
    tmp_path, monkeypatch, heartbeat_age, poll_age
):
    runner, _, _ = _runner(tmp_path)
    exits = []
    monkeypatch.setattr(runner_module, "log_event", lambda *args, **kwargs: None)
    runner._fatal_exit = exits.append
    now = 200.0
    runner._last_heartbeat_success_at = now - heartbeat_age
    runner._last_poll_success_at = now - poll_age

    expired = runner._check_liveness(now=now)

    assert expired is False
    assert exits == []


def test_runner_watchdog_defers_exit_while_a_job_is_active(tmp_path, monkeypatch):
    runner, _, _ = _runner(tmp_path)
    exits = []
    monkeypatch.setattr(runner_module, "log_event", lambda *args, **kwargs: None)
    runner._fatal_exit = exits.append
    runner._last_heartbeat_success_at = 0.0
    runner._last_poll_success_at = 0.0
    runner._active_jobs.add("job-1")

    expired = runner._check_liveness(now=runner.settings.watchdog_timeout + 1.0)

    assert expired is False
    assert exits == []
