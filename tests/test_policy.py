import pytest

from autocode.runtime import Policy
from autocode.state import PolicyDecision


def test_default_policy_allows_every_tool_without_name_classification(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    calls = [
        ("write_file", {"file_path": str(tmp_path.parent / "outside.py")}),
        ("delete_path", {"path": str(tmp_path / ".env")}),
        ("shell_command", {"command": "rm -rf ../outside"}),
        ("web_fetch", {"url": "https://example.com"}),
        ("mcp_remote_delete", {"id": "1"}),
    ]

    for tool_name, arguments in calls:
        assert policy.evaluate_tool_call(tool_name, arguments).action == "allow"


def test_pre_execute_policies_form_an_ordered_waterfall(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    observed = []

    def outer(execution, next_policy):
        observed.append(("outer-before", execution.tool_name, dict(execution.arguments)))
        decision = next_policy()
        observed.append(("outer-after", decision.action))
        return decision

    def inner(execution, next_policy):
        observed.append(("inner", execution.tool_name))
        return PolicyDecision("ask", "deployment requires approval")

    policy.on_pre_execute(outer)
    policy.on_pre_execute(inner)

    decision = policy.evaluate_tool_call("deploy", {"environment": "production"})

    assert decision == PolicyDecision("ask", "deployment requires approval")
    assert observed == [
        ("outer-before", "deploy", {"environment": "production"}),
        ("inner", "deploy"),
        ("outer-after", "ask"),
    ]


def test_pre_execute_policy_can_deny_before_tool_execution(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    policy.on_pre_execute(
        lambda execution, next_policy: (
            PolicyDecision("deny", "production deletion is forbidden")
            if execution.tool_name == "delete_production"
            else next_policy()
        )
    )

    decision = policy.evaluate_tool_call("delete_production", {"id": "1"})

    assert decision == PolicyDecision("deny", "production deletion is forbidden")


def test_monotonic_guard_runs_after_allow_and_can_only_deny(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    policy.on_pre_execute(lambda execution, next_policy: PolicyDecision("allow"))
    policy.add_guard(
        lambda execution: (
            "agent scope forbids this tool"
            if execution.tool_name == "restricted_tool"
            else None
        )
    )

    assert policy.evaluate_tool_call("restricted_tool", {}).action == "allow"
    assert policy.evaluate_guards("safe_tool", {}).action == "allow"
    assert policy.evaluate_guards("restricted_tool", {}) == PolicyDecision(
        "deny",
        "agent scope forbids this tool",
    )


def test_never_rejects_ask_but_does_not_change_allow_or_deny(tmp_path):
    policy = Policy(workspace_root=str(tmp_path), approval_policy="never")

    def policy_rule(execution, next_policy):
        if execution.tool_name == "needs_approval":
            return PolicyDecision("ask", "approval required")
        if execution.tool_name == "forbidden":
            return PolicyDecision("deny", "forbidden by deployment")
        return next_policy()

    policy.on_pre_execute(policy_rule)

    assert policy.evaluate_tool_call("ordinary", {}).action == "allow"
    assert policy.evaluate_tool_call("needs_approval", {}).action == "deny"
    assert policy.evaluate_tool_call("forbidden", {}) == PolicyDecision(
        "deny",
        "forbidden by deployment",
    )


def test_policy_registration_disposer_removes_exact_handler(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    dispose = policy.on_pre_execute(
        lambda execution, next_policy: PolicyDecision("deny", "temporarily blocked")
    )

    assert policy.evaluate_tool_call("echo", {}).action == "deny"
    dispose()
    assert policy.evaluate_tool_call("echo", {}).action == "allow"


def test_policy_rejects_invalid_decisions_and_repeated_next(tmp_path):
    policy = Policy(workspace_root=str(tmp_path))
    policy.on_pre_execute(lambda execution, next_policy: "allow")
    with pytest.raises(TypeError, match="PolicyDecision"):
        policy.evaluate_tool_call("echo", {})

    repeated = Policy(workspace_root=str(tmp_path))

    def call_next_twice(execution, next_policy):
        next_policy()
        return next_policy()

    repeated.on_pre_execute(call_next_twice)
    with pytest.raises(RuntimeError, match="more than once"):
        repeated.evaluate_tool_call("echo", {})


def test_policy_rejects_unknown_approval_policy(tmp_path):
    with pytest.raises(ValueError, match="Unsupported approval policy"):
        Policy(workspace_root=str(tmp_path), approval_policy="unrestricted")
