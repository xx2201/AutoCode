import pytest

from autocode.agent import Agent
from autocode.llm import LLMResponse
from autocode.llm import ToolCall
from autocode.tools.base import Tool


class _CustomTool(Tool):
    name = "custom"
    description = "Custom tool"
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, **kwargs) -> str:
        return "custom-ok"


class _ToolLLM:
    def __init__(self):
        self.model = "fake"
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._calls = 0

    def chat(self, messages, tools=None, on_token=None):
        self._calls += 1
        if self._calls == 1:
            from autocode.llm import ToolCall
            return LLMResponse(content="", tool_calls=[ToolCall(id="1", name="custom", arguments={})])
        return LLMResponse(content="done")


def test_agent_uses_instance_tool_registry(tmp_path):
    agent = Agent(llm=_ToolLLM(), tools=[_CustomTool()], workspace_root=str(tmp_path), permission_mode="full_access")
    reply = agent.chat("use custom")
    assert reply == "done"
    assert any(m.get("content") == "custom-ok" for m in agent.messages if m.get("role") == "tool")


def test_agent_rejects_output_truncated_response_instead_of_completing(tmp_path):
    class _TruncatedLLM:
        model = "test"

        def chat(self, messages, tools=None, on_token=None):
            return LLMResponse(
                content="",
                completion_tokens=32_000,
                stop_reason="max_tokens",
            )

    agent = Agent(
        llm=_TruncatedLLM(),
        tools=[],
        workspace_root=str(tmp_path),
        max_context_tokens=100_000,
        max_output_tokens=32_000,
    )

    with pytest.raises(RuntimeError, match="AUTOCODE_MAX_TOKENS=32000"):
        agent.chat("continue")

    assert agent.turn_state.status == "failed"
    assert not any(message["role"] == "assistant" for message in agent.messages)


class _SafeShellTool(Tool):
    name = "shell_command"
    description = "fake shell"
    parameters = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }

    def execute(self, command: str, timeout: int = 120, _confirmed_sensitive: bool = False) -> str:
        return f"{command}|confirmed={_confirmed_sensitive}"


class _BashLLM:
    def __init__(self):
        self.model = "fake"
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._calls = 0

    def chat(self, messages, tools=None, on_token=None):
        self._calls += 1
        if self._calls == 1:
            return LLMResponse(content="", tool_calls=[ToolCall(id="1", name="shell_command", arguments={"command": "python app.py"})])
        if self._calls == 2:
            return LLMResponse(content="", tool_calls=[ToolCall(id="2", name="shell_command", arguments={"command": "echo ok"})])
        if self._calls == 3:
            return LLMResponse(content="", tool_calls=[ToolCall(id="3", name="shell_command", arguments={"command": "rm -rf build"})])
        if self._calls == 4:
            return LLMResponse(content="", tool_calls=[ToolCall(id="4", name="shell_command", arguments={"command": "rm -rf ../outside"})])
        return LLMResponse(content="done")


def test_agent_full_access_allows_safe_tools_but_keeps_hard_denies(tmp_path):
    calls = []

    def approval_handler(pending):
        calls.append((pending.tool_name, pending.requires_manual, pending.arguments.get("command")))
        return "approve_scope"

    agent = Agent(
        llm=_BashLLM(),
        tools=[_SafeShellTool()],
        workspace_root=str(tmp_path),
    )
    reply = agent.chat("run commands", approval_handler=approval_handler)
    assert reply == "done"
    assert calls == []
    tool_outputs = [m.get("content") for m in agent.messages if m.get("role") == "tool"]
    assert "python app.py|confirmed=False" in tool_outputs
    assert "echo ok|confirmed=False" in tool_outputs
    blocked = [item for item in tool_outputs if "delete via shell is not allowed" in item]
    assert len(blocked) == 2
    assert all("use delete_path instead" in item for item in blocked)

