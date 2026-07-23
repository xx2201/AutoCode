from dataclasses import replace

import pytest

pytest.importorskip("httpx")

from autocode.config import Config
from autocode.remote.manager import RemoteTurnResult
from autocode.web.runner import LocalRunner, RunnerSettings


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

    def conversation_messages(self, client_id):
        return [{"role": "user", "content": "hello", "tool_call_id": ""}]

    def current_task_summary(self, client_id):
        return "Status: completed"

    def current_trace(self, client_id):
        return "LLM calls: 1"

    def reset_chat(self, client_id):
        self.calls.append(("reset", client_id))


def _runner(tmp_path):
    manager = _FakeManager(tmp_path)
    settings = RunnerSettings(
        relay_url="https://relay.example",
        token="runner-token-that-is-long-enough",
        ca_cert=str(tmp_path / "unused.pem"),
        workspace_root=str(tmp_path),
    )
    return LocalRunner(settings, manager=manager, client=object()), manager


def test_runner_bootstrap_uses_local_workspace(tmp_path):
    runner, _ = _runner(tmp_path)

    result = runner.execute("bootstrap", {})

    assert result["model"] == "fake-model"
    assert result["workspace"] == tmp_path.name
    assert result["workspace_path"] == str(tmp_path.resolve())


def test_runner_executes_chat_and_approval_locally(tmp_path):
    runner, manager = _runner(tmp_path)

    chat = runner.execute(
        "chat",
        {"client_id": "web_12345678", "prompt": "inspect project"},
    )
    approval = runner.execute(
        "approval",
        {
            "client_id": "web_12345678",
            "approved": True,
            "approve_all": True,
        },
    )

    assert chat["status"] == "completed"
    assert approval["text"] == "approved"
    assert ("chat", "web_12345678", "inspect project") in manager.calls
    assert ("approval", "web_12345678", True, True) in manager.calls


def test_runner_rejects_unknown_action(tmp_path):
    runner, _ = _runner(tmp_path)

    with pytest.raises(ValueError, match="Unknown relay action"):
        runner.execute("unknown", {})


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

    runner, manager = _runner(tmp_path)
    client = _ExpiredClient()
    runner.client = client

    runner._run_job(
        {
            "job_id": "expired-job",
            "action": "chat",
            "payload": {
                "client_id": "web_12345678",
                "prompt": "hello",
            },
        }
    )

    assert len(client.posts) == 1
    assert manager.calls == [("chat", "web_12345678", "hello")]
