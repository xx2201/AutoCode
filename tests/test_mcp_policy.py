from autocode.runtime import Policy


def test_policy_requires_confirmation_for_mcp_tools(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))

    decision = policy.evaluate_tool_call("mcp_fake_echo", {"message": "hi"})

    assert decision.action == "confirm"
    assert decision.reason == "external MCP tool call"
    assert decision.requires_manual is False

