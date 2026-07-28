import base64
import threading
import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from autocode.web import create_app
from autocode.web.relay import RelayBroker


BROWSER_TOKEN = "browser-token-that-is-long-enough"
RUNNER_TOKEN = "runner-token-that-is-different"
CLIENT_ID = "web_12345678"
WORKSPACE_ID = "12345678901234567890"


@pytest.fixture
def relay_client():
    broker = RelayBroker(runner_ttl=5)
    app = create_app(
        broker=broker,
        browser_token=BROWSER_TOKEN,
        runner_token=RUNNER_TOKEN,
    )
    with TestClient(app) as client:
        yield client, broker


def _browser_headers():
    return {"Authorization": f"Bearer {BROWSER_TOKEN}"}


def _runner_headers():
    return {"Authorization": f"Bearer {RUNNER_TOKEN}"}


def _connect_runner(client):
    response = client.get(
        "/api/runner/next",
        params={"wait": 0},
        headers=_runner_headers(),
    )
    assert response.status_code == 204


def _round_trip(client, request, expected_action, result):
    holder = {}

    def browser_request():
        holder["response"] = request()

    thread = threading.Thread(target=browser_request)
    thread.start()
    runner_job = client.get(
        "/api/runner/next",
        params={"wait": 1},
        headers=_runner_headers(),
    )
    assert runner_job.status_code == 200
    job = runner_job.json()
    assert job["action"] == expected_action
    completed = client.post(
        f"/api/runner/result/{job['job_id']}",
        headers=_runner_headers(),
        json={"success": True, "result": result},
    )
    assert completed.status_code == 200
    thread.join(timeout=2)
    assert not thread.is_alive()
    return holder["response"], job


def test_web_root_and_health_are_public(relay_client):
    client, _ = relay_client
    root = client.get("/")
    favicon = client.get("/favicon.ico")
    health = client.get("/api/health")

    assert root.status_code == 200
    assert "AutoCode" in root.text
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    assert root.headers["x-frame-options"] == "DENY"
    assert "img-src 'self' data: blob:" in root.headers["content-security-policy"]
    assert health.json()["runner_connected"] is False


def test_browser_and_runner_tokens_are_isolated(relay_client):
    client, _ = relay_client

    assert client.post("/api/auth/verify").status_code == 401
    assert client.post("/api/auth/verify", headers=_runner_headers()).status_code == 401
    assert client.post("/api/auth/verify", headers=_browser_headers()).status_code == 200
    assert client.get("/api/runner/next", headers=_browser_headers()).status_code == 401
    assert (
        client.get(f"/api/git/status?workspace_id={WORKSPACE_ID}").status_code
        == 401
    )


def test_runner_heartbeat_marks_runner_connected(relay_client):
    client, _ = relay_client

    heartbeat = client.post("/api/runner/heartbeat", headers=_runner_headers())

    assert heartbeat.status_code == 200
    assert client.get("/api/health").json()["runner_connected"] is True


def test_browser_request_reports_offline_runner(relay_client):
    client, _ = relay_client

    response = client.get("/api/bootstrap", headers=_browser_headers())

    assert response.status_code == 503
    assert "Runner" in response.json()["detail"]


def test_chat_is_relayed_to_runner(relay_client):
    client, _ = relay_client
    _connect_runner(client)

    response, job = _round_trip(
        client,
        lambda: client.post(
            "/api/chat",
            headers=_browser_headers(),
            json={
                "client_id": CLIENT_ID,
                "workspace_id": WORKSPACE_ID,
                "prompt": "inspect project",
            },
        ),
        "chat",
        {"text": "done", "status": "completed"},
    )

    assert response.status_code == 200
    assert response.json()["text"] == "done"
    assert job["payload"] == {
        "client_id": CLIENT_ID,
        "workspace_id": WORKSPACE_ID,
        "prompt": "inspect project",
        "permission_mode": "ask",
    }


def test_authenticated_download_is_relayed_and_returns_file(relay_client):
    client, _ = relay_client
    _connect_runner(client)
    content = b"%PDF-download"

    response, job = _round_trip(
        client,
        lambda: client.post(
            "/api/download",
            headers=_browser_headers(),
            json={
                "client_id": CLIENT_ID,
                "workspace_id": WORKSPACE_ID,
                "file_id": "opaque-file-id-that-is-long-enough",
            },
        ),
        "download",
        {
            "name": "简历.pdf",
            "media_type": "application/pdf",
            "data_base64": base64.b64encode(content).decode("ascii"),
        },
    )

    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"] == "application/pdf"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert job["payload"]["file_id"] == "opaque-file-id-that-is-long-enough"


def test_git_status_diff_and_action_are_relayed(relay_client):
    client, _ = relay_client
    _connect_runner(client)

    status_response, status_job = _round_trip(
        client,
        lambda: client.get(
            f"/api/git/status?workspace_id={WORKSPACE_ID}",
            headers=_browser_headers(),
        ),
        "git_status",
        {"available": True, "branch": "main", "changes": []},
    )
    diff_response, diff_job = _round_trip(
        client,
        lambda: client.post(
            "/api/git/diff",
            headers=_browser_headers(),
            json={
                "workspace_id": WORKSPACE_ID,
                "scope": "compare",
                "base": "main",
                "path": "app.py",
            },
        ),
        "git_diff",
        {"scope": "compare", "base": "main", "path": "app.py", "diff": ""},
    )
    action_response, action_job = _round_trip(
        client,
        lambda: client.post(
            "/api/git/action",
            headers=_browser_headers(),
            json={
                "workspace_id": WORKSPACE_ID,
                "action": "stage",
                "paths": ["app.py"],
            },
        ),
        "git_action",
        {"action": "stage", "git": {"available": True, "branch": "main"}},
    )

    assert status_response.status_code == 200
    assert status_job["payload"] == {"workspace_id": WORKSPACE_ID}
    assert diff_response.status_code == 200
    assert diff_job["payload"]["base"] == "main"
    assert action_response.status_code == 200
    assert action_job["payload"]["git_action"] == "stage"


def test_workspace_file_list_and_read_are_relayed(relay_client):
    client, _ = relay_client
    _connect_runner(client)

    list_response, list_job = _round_trip(
        client,
        lambda: client.get(
            f"/api/files?workspace_id={WORKSPACE_ID}",
            headers=_browser_headers(),
        ),
        "workspace_files",
        {"files": ["src/app.py"], "truncated": False},
    )
    read_response, read_job = _round_trip(
        client,
        lambda: client.post(
            "/api/files/read",
            headers=_browser_headers(),
            json={"workspace_id": WORKSPACE_ID, "path": "src/app.py"},
        ),
        "workspace_file",
        {"path": "src/app.py", "content": "print('ok')", "binary": False},
    )

    assert list_response.json()["files"] == ["src/app.py"]
    assert list_job["payload"] == {"workspace_id": WORKSPACE_ID}
    assert read_response.json()["content"] == "print('ok')"
    assert read_job["payload"]["path"] == "src/app.py"


def test_streaming_chat_relays_tokens_stages_and_final_result(relay_client):
    client, _ = relay_client
    _connect_runner(client)
    holder = {}

    def browser_request():
        with client.stream(
            "POST",
            "/api/chat/stream",
            headers=_browser_headers(),
            json={
                "client_id": CLIENT_ID,
                "workspace_id": WORKSPACE_ID,
                "prompt": "stream this",
            },
        ) as response:
            holder["status"] = response.status_code
            holder["events"] = [
                json.loads(line[6:])
                for line in response.iter_lines()
                if line.startswith("data: ")
            ]

    thread = threading.Thread(target=browser_request)
    thread.start()
    job_response = client.get(
        "/api/runner/next",
        params={"wait": 1},
        headers=_runner_headers(),
    )
    assert job_response.status_code == 200
    job = job_response.json()
    assert job["stream"] is True
    event_response = client.post(
        f"/api/runner/event/{job['job_id']}",
        headers=_runner_headers(),
        json={"type": "token", "text": "hello", "elapsed_ms": 12.5},
    )
    assert event_response.status_code == 200
    completed = client.post(
        f"/api/runner/result/{job['job_id']}",
        headers=_runner_headers(),
        json={
            "success": True,
            "result": {"text": "hello", "status": "completed"},
        },
    )
    assert completed.status_code == 200
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert holder["status"] == 200
    assert any(event.get("stage") == "claimed" for event in holder["events"])
    assert any(event.get("type") == "token" and event.get("text") == "hello" for event in holder["events"])
    final = next(event for event in holder["events"] if event.get("type") == "result")
    assert final["data"]["text"] == "hello"
    assert final["timings"]["relay_total_ms"] >= 0


def test_streaming_chat_preserves_live_work_event_contract(relay_client):
    client, _ = relay_client
    _connect_runner(client)
    holder = {}

    def browser_request():
        with client.stream(
            "POST",
            "/api/chat/stream",
            headers=_browser_headers(),
            json={
                "client_id": CLIENT_ID,
                "workspace_id": WORKSPACE_ID,
                "prompt": "show live work",
            },
        ) as response:
            holder["status"] = response.status_code
            holder["events"] = [
                json.loads(line[6:])
                for line in response.iter_lines()
                if line.startswith("data: ")
            ]

    thread = threading.Thread(target=browser_request)
    thread.start()
    job = client.get(
        "/api/runner/next",
        params={"wait": 1},
        headers=_runner_headers(),
    ).json()

    narrative = {
        "type": "work",
        "phase": "narrative",
        "work_id": "step-1-narrative",
        "content": "先查看文件。",
    }
    planned = {
        "type": "work",
        "phase": "planned",
        "tool_call_id": "call-1",
        "tool_name": "glob",
        "arguments": {"pattern": "**/*.py"},
    }
    completed = {
        **planned,
        "phase": "completed",
        "output": "main.py",
        "duration_ms": 12.5,
        "success": True,
    }
    turn = {
        "type": "turn",
        "phase": "started",
        "turn_id": "turn-1",
        "revision_id": "revision-1",
        "queued": False,
    }
    for event in (turn, narrative, planned, completed):
        response = client.post(
            f"/api/runner/event/{job['job_id']}",
            headers=_runner_headers(),
            json=event,
        )
        assert response.status_code == 200

    rejected = client.post(
        f"/api/runner/event/{job['job_id']}",
        headers=_runner_headers(),
        json={"type": "work", "unknown_protocol_field": "must fail"},
    )
    assert rejected.status_code == 422

    client.post(
        f"/api/runner/result/{job['job_id']}",
        headers=_runner_headers(),
        json={"success": True, "result": {"text": "done", "status": "completed"}},
    )
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert holder["status"] == 200
    streamed = holder["events"]
    assert turn in streamed
    assert narrative in streamed
    assert planned in streamed
    assert completed in streamed


def test_streaming_approval_continuation_can_outlive_control_timeout(relay_client):
    client, _ = relay_client
    _connect_runner(client)
    holder = {}

    def browser_request():
        with client.stream(
            "POST",
            "/api/turn/continue/stream",
            headers=_browser_headers(),
            json={
                "client_id": CLIENT_ID,
                "workspace_id": WORKSPACE_ID,
                "expected_turn_id": "turn_1",
                "batch_id": "batch_1",
            },
        ) as response:
            holder["status"] = response.status_code
            holder["events"] = [
                json.loads(line[6:])
                for line in response.iter_lines()
                if line.startswith("data: ")
            ]

    thread = threading.Thread(target=browser_request)
    thread.start()
    job = client.get(
        "/api/runner/next",
        params={"wait": 1},
        headers=_runner_headers(),
    ).json()
    assert job["action"] == "continue_turn"
    assert job["stream"] is True
    assert job["payload"]["expected_turn_id"] == "turn_1"
    assert job["payload"]["batch_id"] == "batch_1"
    client.post(
        f"/api/runner/event/{job['job_id']}",
        headers=_runner_headers(),
        json={"type": "stage", "stage": "model_started"},
    )
    client.post(
        f"/api/runner/result/{job['job_id']}",
        headers=_runner_headers(),
        json={"success": True, "result": {"text": "continued", "status": "completed"}},
    )
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert holder["status"] == 200
    assert any(event.get("stage") == "model_started" for event in holder["events"])
    final = next(event for event in holder["events"] if event["type"] == "result")
    assert final["data"]["text"] == "continued"


def test_permission_mode_is_relayed_to_runner(relay_client):
    client, _ = relay_client
    _connect_runner(client)
    response, job = _round_trip(
        client,
        lambda: client.post(
            "/api/permission-mode",
            headers=_browser_headers(),
            json={
                "client_id": CLIENT_ID,
                "workspace_id": WORKSPACE_ID,
                "permission_mode": "full_access",
            },
        ),
        "permission_mode",
        {"permission_mode": "full_access"},
    )

    assert response.status_code == 200
    assert job["payload"]["permission_mode"] == "full_access"


def test_turn_controls_and_change_actions_are_relayed(relay_client):
    client, _ = relay_client
    _connect_runner(client)

    steer_response, steer_job = _round_trip(
        client,
        lambda: client.post(
            "/api/turn/message",
            headers=_browser_headers(),
            json={
                "client_id": CLIENT_ID,
                "workspace_id": WORKSPACE_ID,
                "expected_turn_id": "turn_123",
                "mode": "steer",
                "prompt": "focus on tests",
            },
        ),
        "turn_message",
        {"accepted": True, "mode": "steer", "turn_id": "turn_123"},
    )
    change_response, change_job = _round_trip(
        client,
        lambda: client.post(
            "/api/changes/action",
            headers=_browser_headers(),
            json={
                "client_id": CLIENT_ID,
                "workspace_id": WORKSPACE_ID,
                "turn_id": "turn_123",
                "action": "undo",
            },
        ),
        "change_action",
        {"turn_id": "turn_123", "state": "undone"},
    )
    approval_response, approval_job = _round_trip(
        client,
        lambda: client.post(
            "/api/approval/decision",
            headers=_browser_headers(),
            json={
                "client_id": CLIENT_ID,
                "workspace_id": WORKSPACE_ID,
                "approval_id": "approval_123",
                "expected_turn_id": "turn_123",
                "batch_id": "batch_123",
                "action": "approve_scope",
            },
        ),
        "approval_decision",
        {
            "batch_id": "batch_123",
            "turn_id": "turn_123",
            "ready": True,
            "approvals": [],
        },
    )

    assert steer_response.status_code == 200
    assert steer_job["payload"]["expected_turn_id"] == "turn_123"
    assert steer_job["payload"]["mode"] == "steer"
    assert change_response.status_code == 200
    assert change_job["payload"]["change_action"] == "undo"
    assert approval_response.status_code == 200
    assert approval_job["payload"]["approval_id"] == "approval_123"
    assert approval_job["payload"]["approval_action"] == "approve_scope"
    assert approval_job["stream"] is False


def test_edit_turn_is_streamed_to_runner(relay_client):
    client, _ = relay_client
    _connect_runner(client)
    holder = {}

    def browser_request():
        with client.stream(
            "POST",
            "/api/turn/edit/stream",
            headers=_browser_headers(),
            json={
                "client_id": CLIENT_ID,
                "workspace_id": WORKSPACE_ID,
                "turn_id": "turn_123",
                "prompt": "edited prompt",
            },
        ) as response:
            holder["status"] = response.status_code
            holder["events"] = [
                json.loads(line[6:])
                for line in response.iter_lines()
                if line.startswith("data: ")
            ]

    thread = threading.Thread(target=browser_request)
    thread.start()
    job = client.get(
        "/api/runner/next",
        params={"wait": 1},
        headers=_runner_headers(),
    ).json()
    assert job["action"] == "edit_turn"
    assert job["payload"]["turn_id"] == "turn_123"
    client.post(
        f"/api/runner/result/{job['job_id']}",
        headers=_runner_headers(),
        json={"success": True, "result": {"text": "edited answer"}},
    )
    thread.join(timeout=3)

    assert holder["status"] == 200
    final = next(event for event in holder["events"] if event["type"] == "result")
    assert final["data"]["text"] == "edited answer"


def test_chat_accepts_attachment_without_text(relay_client):
    client, _ = relay_client
    _connect_runner(client)
    attachment = {
        "name": "note.txt",
        "media_type": "text/plain",
        "data_base64": base64.b64encode(b"hello").decode("ascii"),
    }
    response, job = _round_trip(
        client,
        lambda: client.post(
            "/api/chat",
            headers=_browser_headers(),
            json={
                "client_id": CLIENT_ID,
                "workspace_id": WORKSPACE_ID,
                "prompt": "",
                "attachments": [attachment],
            },
        ),
        "chat",
        {"text": "done", "status": "completed"},
    )

    assert response.status_code == 200
    assert job["payload"]["attachments"] == [attachment]


def test_runner_error_is_returned_to_browser(relay_client):
    client, _ = relay_client
    _connect_runner(client)
    holder = {}

    thread = threading.Thread(
        target=lambda: holder.setdefault(
            "response",
            client.get(
                f"/api/trace/{CLIENT_ID}?workspace_id={WORKSPACE_ID}",
                headers=_browser_headers(),
            ),
        )
    )
    thread.start()
    job = client.get(
        "/api/runner/next",
        params={"wait": 1},
        headers=_runner_headers(),
    ).json()
    client.post(
        f"/api/runner/result/{job['job_id']}",
        headers=_runner_headers(),
        json={"success": False, "error": "No active session.", "status_code": 409},
    )
    thread.join(timeout=2)

    assert holder["response"].status_code == 409
    assert holder["response"].json()["detail"] == "No active session."


def test_web_rejects_invalid_client_id_and_blank_prompt(relay_client):
    client, _ = relay_client
    _connect_runner(client)

    invalid_id = client.post(
        "/api/chat",
        headers=_browser_headers(),
        json={
            "client_id": "../bad-id",
            "workspace_id": WORKSPACE_ID,
            "prompt": "hello",
        },
    )
    blank = client.post(
        "/api/chat",
        headers=_browser_headers(),
        json={
            "client_id": CLIENT_ID,
            "workspace_id": WORKSPACE_ID,
            "prompt": "   ",
        },
    )

    assert invalid_id.status_code == 422
    assert blank.status_code == 422
