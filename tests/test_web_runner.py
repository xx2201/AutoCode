import base64
from dataclasses import replace

import pytest

pytest.importorskip("httpx")

from autocode.config import Config
from autocode.remote.manager import RemoteTurnResult
from autocode.web.runner import LocalRunner, RunnerSettings
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
            task_id="task_1",
            status="completed",
        )
        self.calls = []

    def list_resume_candidates(self, limit=10):
        return [{"session_id": "session_1"}][:limit]

    def submit(self, client_id, prompt):
        self.calls.append(("chat", client_id, prompt))
        return self.result

    def resolve_approval(self, client_id, approved, approve_all):
        self.calls.append(("approval", client_id, approved, approve_all))
        return replace(self.result, text="approved")

    def resume_session(self, client_id, session_id):
        self.calls.append(("resume", client_id, session_id))
        return replace(self.result, text="resumed")

    def delete_session(self, session_id):
        self.calls.append(("delete_session", session_id))

    def conversation_messages(self, client_id):
        return [{"role": "user", "content": "hello", "tool_call_id": ""}]

    def current_task_summary(self, client_id):
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
    assert result["context_window_tokens"] == 1_000_000
    assert result["capabilities"]["file_download"] is True
    assert result["capabilities"]["git_workspace"] is True
    assert [item["workspace_id"] for item in result["workspaces"]] == [
        workspace.workspace_id
    ]
    assert all(item["path"] != str(unregistered) for item in result["workspaces"])


def test_runner_executes_chat_and_approval_in_selected_workspace(tmp_path):
    runner, workspace, managers = _runner(tmp_path)
    payload = {
        "workspace_id": workspace.workspace_id,
        "client_id": "web_12345678",
    }

    chat = runner.execute("chat", {**payload, "prompt": "inspect project"})
    approval = runner.execute(
        "approval",
        {**payload, "approved": True, "approve_all": True},
    )
    manager = managers[str((tmp_path / "project-a").resolve())]

    assert chat["status"] == "completed"
    assert chat["files"] == []
    assert approval["text"] == "approved"
    assert ("chat", "web_12345678", "inspect project") in manager.calls
    assert ("approval", "web_12345678", True, True) in manager.calls


def test_runner_converts_tool_hooks_to_work_events(tmp_path):
    runner, workspace, managers = _runner(tmp_path)
    manager = runner._manager(workspace.workspace_id)

    def submit(client_id, prompt, hook_handler=None, on_token=None):
        manager.calls.append(("chat", client_id, prompt))
        hook_handler(
            "before_tool",
            {
                "tool_call_id": "call_1",
                "tool_name": "bash",
                "arguments": {"command": "git status"},
            },
        )
        hook_handler(
            "after_tool",
            {
                "tool_call_id": "call_1",
                "tool_name": "bash",
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
            "phase": "started",
            "tool_call_id": "call_1",
            "tool_name": "bash",
            "arguments": {"command": "git status"},
        },
        {
            "type": "work",
            "phase": "completed",
            "tool_call_id": "call_1",
            "tool_name": "bash",
            "output": "clean",
            "duration_ms": 1250.0,
            "success": True,
        },
    ]


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
