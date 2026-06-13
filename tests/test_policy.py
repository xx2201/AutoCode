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


def test_policy_allows_read_only_bash(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("bash", {"command": "git status"})
    assert decision.action == "allow"


def test_policy_denies_bash_redirect_to_protected_env(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("bash", {"command": "echo hi > .env"})
    assert decision.action == "deny"

