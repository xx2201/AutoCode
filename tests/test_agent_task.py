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
    agent = Agent(llm=_ToolLLM(), tools=[_CustomTool()], workspace_root=str(tmp_path), auto_approve=True)
    reply = agent.chat("use custom")
    assert reply == "done"
    assert any(m.get("content") == "custom-ok" for m in agent.messages if m.get("role") == "tool")


class _SafeBashTool(Tool):
    name = "bash"
    description = "fake bash"
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
            return LLMResponse(content="", tool_calls=[ToolCall(id="1", name="bash", arguments={"command": "python app.py"})])
        if self._calls == 2:
            return LLMResponse(content="", tool_calls=[ToolCall(id="2", name="bash", arguments={"command": "echo ok"})])
        if self._calls == 3:
            return LLMResponse(content="", tool_calls=[ToolCall(id="3", name="bash", arguments={"command": "rm -rf build"})])
        if self._calls == 4:
            return LLMResponse(content="", tool_calls=[ToolCall(id="4", name="bash", arguments={"command": "rm -rf ../outside"})])
        return LLMResponse(content="done")


def test_agent_approve_all_allows_workspace_local_risk_but_denies_outside_workspace(tmp_path):
    calls = []

    def approval_handler(pending):
        calls.append((pending.tool_name, pending.requires_manual, pending.arguments.get("command")))
        return "approve_all"

    agent = Agent(
        llm=_BashLLM(),
        tools=[_SafeBashTool()],
        workspace_root=str(tmp_path),
    )
    reply = agent.chat("run commands", approval_handler=approval_handler)
    assert reply == "done"
    assert calls == [("bash", False, "python app.py")]
    tool_outputs = [m.get("content") for m in agent.messages if m.get("role") == "tool"]
    assert "python app.py|confirmed=False" in tool_outputs
    assert "echo ok|confirmed=False" in tool_outputs
    assert "rm -rf build|confirmed=False" in tool_outputs
    assert any("dangerous command target must stay inside workspace" in item for item in tool_outputs)

