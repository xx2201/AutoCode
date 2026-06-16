from autocode.runtime import Policy


def test_policy_allows_workspace_edit(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    target = tmp_path / "sample.py"
    decision = policy.evaluate_tool_call("edit_file", {"file_path": str(target)})
    assert decision.action == "allow"


def test_policy_denies_outside_workspace(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    target = tmp_path.parent / "outside.py"
    decision = policy.evaluate_tool_call("write_file", {"file_path": str(target)})
    assert decision.action == "deny"


def test_policy_protects_env_file(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    target = tmp_path / ".env"
    decision = policy.evaluate_tool_call("write_file", {"file_path": str(target)})
    assert decision.action == "deny"


def test_policy_allows_unknown_bash(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("bash", {"command": "python manage.py migrate"})
    assert decision.action == "allow"


def test_policy_denies_destructive_delete_in_workspace(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("bash", {"command": "rm -rf build"})
    assert decision.action == "deny"


def test_policy_denies_destructive_delete_outside_workspace(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("bash", {"command": "rm -rf ../outside"})
    assert decision.action == "deny"


def test_policy_denies_workspace_local_del(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("bash", {"command": "del receive.log 2>nul"})
    assert decision.action == "deny"
    assert "delete_path" in decision.reason


def test_policy_requires_manual_confirmation_for_delete_tool(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    target = tmp_path / "sample.py"
    decision = policy.evaluate_tool_call("delete_path", {"path": str(target)})
    assert decision.action == "confirm"
    assert decision.requires_manual is True


def test_policy_does_not_mistake_embedded_del_text_for_shell_delete(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call(
        "bash",
        {"command": "python -c \"print('DEL label only')\""},
    )
    assert decision.action == "allow"


def test_policy_denies_streaming_redis_monitor_in_bash(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call(
        "bash",
        {"command": "docker exec demo-redis redis-cli MONITOR"},
    )
    assert decision.action == "deny"
    assert "start_process" in decision.reason


def test_policy_denies_tail_follow_in_bash(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("bash", {"command": "tail -f backend.log"})
    assert decision.action == "deny"
    assert "start_process" in decision.reason


def test_policy_allows_read_only_bash(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("bash", {"command": "git status"})
    assert decision.action == "allow"


def test_policy_denies_bash_redirect_to_protected_env(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("bash", {"command": "echo hi > .env"})
    assert decision.action == "deny"


def test_policy_denies_shell_taskkill_even_with_pid(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("bash", {"command": "taskkill /PID 12345 /T /F"})
    assert decision.action == "deny"
    assert "use stop_process" in decision.reason


def test_policy_denies_shell_stop_process_pipeline(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call(
        "bash",
        {"command": "powershell -Command \"Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force\""},
    )
    assert decision.action == "deny"
    assert "use stop_process" in decision.reason

