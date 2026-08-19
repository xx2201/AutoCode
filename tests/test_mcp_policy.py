from autocode.runtime import Policy


def test_mcp_tools_use_the_same_default_policy_as_local_tools(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))

    decision = policy.evaluate_tool_call("mcp_fake_echo", {"message": "hi"})

    assert decision.action == "allow"
