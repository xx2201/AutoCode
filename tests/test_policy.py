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


def test_policy_allows_unknown_shell_command(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("shell_command", {"command": "python manage.py migrate"})
    assert decision.action == "allow"


def test_policy_denies_destructive_delete_in_workspace(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("shell_command", {"command": "rm -rf build"})
    assert decision.action == "deny"


def test_policy_denies_destructive_delete_outside_workspace(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("shell_command", {"command": "rm -rf ../outside"})
    assert decision.action == "deny"


def test_policy_denies_workspace_local_del(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("shell_command", {"command": "del receive.log 2>nul"})
    assert decision.action == "deny"
    assert "delete_path" in decision.reason


def test_policy_requires_manual_confirmation_for_delete_tool(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    target = tmp_path / "sample.py"
    decision = policy.evaluate_tool_call("delete_path", {"path": str(target)})
    assert decision.action == "confirm"
    assert decision.requires_manual is True


def test_policy_requires_manual_confirmation_for_web_fetch(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call(
        "web_fetch",
        {"url": "https://example.com", "prompt": "summarize"},
    )
    assert decision.action == "confirm"
    assert decision.requires_manual is True
    assert decision.approval_scope == "web_fetch:https://example.com:443"
    assert "example.com" in decision.approval_label


def test_policy_groups_web_fetch_by_protocol_host_and_port(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    first = policy.evaluate_tool_call(
        "web_fetch",
        {"url": "https://example.com/a"},
    )
    second = policy.evaluate_tool_call(
        "web_fetch",
        {"url": "https://example.com/b"},
    )
    other_port = policy.evaluate_tool_call(
        "web_fetch",
        {"url": "https://example.com:8443/b"},
    )

    assert first.approval_scope == second.approval_scope
    assert first.approval_scope != other_port.approval_scope


def test_full_access_skips_confirmations_but_keeps_hard_denies(tmp_path):
    policy = Policy(workspace_root=str(tmp_path), permission_mode="full_access")
    target = tmp_path / "generated.txt"

    assert policy.evaluate_tool_call(
        "web_fetch",
        {"url": "https://example.com"},
    ).action == "allow"
    assert policy.evaluate_tool_call(
        "delete_path",
        {"path": str(target)},
    ).action == "allow"
    assert policy.evaluate_tool_call(
        "shell_command",
        {"command": "rm -rf *"},
    ).action == "deny"
    assert policy.evaluate_tool_call(
        "write_file",
        {"file_path": str(tmp_path / ".env")},
    ).action == "deny"


def test_policy_rejects_unknown_permission_mode(tmp_path):
    try:
        Policy(workspace_root=str(tmp_path), permission_mode="unrestricted")
    except ValueError as exc:
        assert "Unsupported permission mode" in str(exc)
    else:
        raise AssertionError("invalid permission mode must fail")


def test_policy_does_not_mistake_embedded_del_text_for_shell_delete(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call(
        "shell_command",
        {"command": "python -c \"print('DEL label only')\""},
    )
    assert decision.action == "allow"


def test_policy_denies_streaming_redis_monitor_in_bash(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call(
        "shell_command",
        {"command": "docker exec demo-redis redis-cli MONITOR"},
    )
    assert decision.action == "deny"
    assert "start_process" in decision.reason


def test_policy_denies_tail_follow_in_bash(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("shell_command", {"command": "tail -f backend.log"})
    assert decision.action == "deny"
    assert "start_process" in decision.reason


def test_policy_allows_read_only_bash(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("shell_command", {"command": "git status"})
    assert decision.action == "allow"


def test_policy_denies_bash_redirect_to_protected_env(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("shell_command", {"command": "echo hi > .env"})
    assert decision.action == "deny"


def test_policy_denies_shell_taskkill_even_with_pid(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("shell_command", {"command": "taskkill /PID 12345 /T /F"})
    assert decision.action == "deny"
    assert "use stop_process" in decision.reason


def test_policy_denies_shell_stop_process_pipeline(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call(
        "shell_command",
        {"command": "powershell -Command \"Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force\""},
    )
    assert decision.action == "deny"
    assert "use stop_process" in decision.reason

