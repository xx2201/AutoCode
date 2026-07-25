"""Tests for Agent Skills discovery and progressive loading."""

from pathlib import Path

from autocode.agent import Agent
from autocode.llm import LLMResponse
from autocode.skills import SkillManager
from autocode.tools.skill import SkillTool


def _write_skill(root: Path, name: str, content: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(content, encoding="utf-8")
    return path


def test_catalog_exposes_metadata_without_loading_body(tmp_path):
    _write_skill(
        tmp_path / "home" / ".agents" / "skills",
        "release",
        "---\nname: release\ndescription: Publish a tested release\n---\nSECRET RELEASE BODY\n",
    )
    manager = SkillManager(str(tmp_path), home=tmp_path / "home")

    catalog = manager.catalog_block()

    assert "release" in catalog
    assert "Publish a tested release" in catalog
    assert "SECRET RELEASE BODY" not in catalog


def test_skill_loads_body_and_substitutes_arguments(tmp_path):
    skill_path = _write_skill(
        tmp_path / "home" / ".autocode" / "skills",
        "explain",
        (
            "---\nname: explain\ndescription: Explain a target\n---\n"
            "Target=$0\nAll=$ARGUMENTS\nDir=${AUTOCODE_SKILL_DIR}\n"
        ),
    )
    manager = SkillManager(str(tmp_path), home=tmp_path / "home")

    loaded = manager.load("explain", '"hello world" detailed')

    assert "Target=hello world" in loaded
    assert 'All="hello world" detailed' in loaded
    assert f"Dir={skill_path.parent}" in loaded


def test_autocode_skill_overrides_shared_agents_skill(tmp_path):
    home = tmp_path / "home"
    _write_skill(home / ".agents" / "skills", "review", "---\ndescription: shared\n---\nshared body\n")
    _write_skill(home / ".autocode" / "skills", "review", "---\ndescription: autocode\n---\nautocode body\n")

    manager = SkillManager(str(tmp_path), home=home)

    assert manager.discover()["review"].description == "autocode"
    assert "autocode body" in manager.load("review")


def test_claude_and_project_skill_directories_are_not_discovered(tmp_path):
    home = tmp_path / "home"
    _write_skill(home / ".claude" / "skills", "claude-only", "---\ndescription: claude\n---\nbody\n")
    _write_skill(tmp_path / ".agents" / "skills", "project-only", "---\ndescription: project\n---\nbody\n")

    manager = SkillManager(str(tmp_path), home=home)

    assert manager.discover() == {}


def test_model_cannot_load_user_only_skill_but_explicit_user_can(tmp_path):
    _write_skill(
        tmp_path / "home" / ".agents" / "skills",
        "deploy",
        (
            "---\ndescription: Deploy production\ndisable-model-invocation: true\n"
            "user-invocable: true\n---\nDeploy $ARGUMENTS\n"
        ),
    )
    manager = SkillManager(str(tmp_path), home=tmp_path / "home")
    tool = SkillTool()
    tool._skill_manager = manager

    assert "deploy" not in manager.catalog_block()
    assert tool.execute(name="deploy").startswith("Error:")
    assert "Deploy staging" in manager.explicit_invocation("/deploy staging")


def test_skill_resource_stays_inside_skill_directory(tmp_path):
    skill_path = _write_skill(
        tmp_path / "home" / ".agents" / "skills",
        "docs",
        "---\ndescription: Read bundled docs\n---\nRead reference.md\n",
    )
    (skill_path.parent / "reference.md").write_text("reference body", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    manager = SkillManager(str(tmp_path), home=tmp_path / "home")

    assert "reference body" in manager.read_resource("docs", "reference.md")
    try:
        manager.read_resource("docs", "../../../outside.txt")
    except ValueError as exc:
        assert "inside the skill directory" in str(exc)
    else:
        raise AssertionError("path traversal should be rejected")


class _CaptureLLM:
    def __init__(self):
        self.model = "fake"
        self.messages = []

    def chat(self, messages, tools=None, on_token=None):
        self.messages = messages
        return LLMResponse(content="done")


class _SkillCallingLLM:
    def __init__(self):
        self.model = "fake"
        self.calls = 0

    def chat(self, messages, tools=None, on_token=None):
        from autocode.llm import ToolCall

        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(id="skill-1", name="skill", arguments={"name": "review"})],
            )
        skill_result = next(message["content"] for message in messages if message.get("role") == "tool")
        return LLMResponse(content=skill_result)


def test_agent_injects_explicit_skill_and_catalog_is_progressive(tmp_path):
    home = tmp_path / "home"
    _write_skill(
        home / ".agents" / "skills",
        "review",
        "---\ndescription: Review current changes\n---\nREVIEW BODY $ARGUMENTS\n",
    )
    llm = _CaptureLLM()
    agent = Agent(llm=llm, workspace_root=str(tmp_path))
    agent.skills = SkillManager(str(tmp_path), home=home)
    agent._sync_mcp_tools()

    reply = agent.chat("/review src/app.py")

    assert reply == "done"
    assert "Review current changes" in llm.messages[0]["content"]
    assert "REVIEW BODY" not in llm.messages[0]["content"]
    user_message = next(message for message in llm.messages if message["role"] == "user")
    assert "REVIEW BODY src/app.py" in user_message["content"]


def test_agent_can_load_skill_through_registered_tool(tmp_path):
    home = tmp_path / "home"
    _write_skill(
        home / ".agents" / "skills",
        "review",
        "---\ndescription: Review current changes\n---\nREVIEW TOOL BODY\n",
    )
    agent = Agent(llm=_SkillCallingLLM(), workspace_root=str(tmp_path))
    agent.skills = SkillManager(str(tmp_path), home=home)
    agent._sync_mcp_tools()

    reply = agent.chat("please review this project")

    assert "REVIEW TOOL BODY" in reply
