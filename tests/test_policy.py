from corecoder.policy import Policy


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


def test_policy_confirms_unknown_bash(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("bash", {"command": "python manage.py migrate"})
    assert decision.action == "confirm"
    assert decision.requires_manual is False


def test_policy_requires_manual_confirmation_for_destructive_delete(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("bash", {"command": "rm -rf build"})
    assert decision.action == "confirm"
    assert decision.requires_manual is True


def test_policy_allows_read_only_bash(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    decision = policy.evaluate_tool_call("bash", {"command": "git status"})
    assert decision.action == "allow"
