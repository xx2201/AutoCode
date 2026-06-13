"""Tests for core modules: config, context, session, imports."""

import os
import pathlib

from autocode import Agent, LLM, Config, ALL_TOOLS, __version__
from autocode.context import ContextManager, MemoryManager, estimate_tokens
from autocode.llm import LLMResponse
from autocode.tools import get_tool


def test_version():
    assert __version__ == "0.3.0"


def test_public_api_exports():
    """Users should be able to import key classes from the top-level package."""
    assert Agent is not None
    assert LLM is not None
    assert Config is not None
    assert len(ALL_TOOLS) == 12


def test_config_from_env(monkeypatch):
    monkeypatch.setattr("autocode.config._load_dotenv", lambda: None)
    os.environ["AUTOCODE_MODEL"] = "test-model"
    os.environ["AUTOCODE_TELEGRAM_ALLOWED_CHATS"] = "123,456"
    c = Config.from_env()
    assert c.model == "test-model"
    assert c.telegram_allowed_chat_ids == (123, 456)
    del os.environ["AUTOCODE_MODEL"]
    del os.environ["AUTOCODE_TELEGRAM_ALLOWED_CHATS"]


def test_config_defaults(monkeypatch):
    monkeypatch.setattr("autocode.config._load_dotenv", lambda: None)
    # temporarily clear relevant env vars
    saved = {}
    for k in ["AUTOCODE_MODEL", "AUTOCODE_API_KEY", "AUTOCODE_BASE_URL", "AUTOCODE_MAX_TOKENS", "AUTOCODE_AUTO_APPROVE", "OPENAI_API_KEY", "OPENAI_BASE_URL"]:
        if k in os.environ:
            saved[k] = os.environ.pop(k)

    c = Config.from_env()
    assert c.model == ""
    assert c.api_key == ""
    assert c.base_url is None
    assert c.max_tokens == 4096
    assert c.max_context_tokens == 1_000_000
    assert c.temperature == 0.0
    assert c.auto_approve is False

    os.environ.update(saved)


def test_config_ignores_openai_fallback(monkeypatch):
    monkeypatch.setattr("autocode.config._load_dotenv", lambda: None)
    saved = {}
    for k in ["AUTOCODE_API_KEY", "AUTOCODE_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL"]:
        if k in os.environ:
            saved[k] = os.environ.pop(k)

    os.environ["OPENAI_API_KEY"] = "should-not-be-used"
    os.environ["OPENAI_BASE_URL"] = "https://example.com/v1"
    c = Config.from_env()
    assert c.api_key == ""
    assert c.base_url is None

    for k in ["OPENAI_API_KEY", "OPENAI_BASE_URL"]:
        os.environ.pop(k, None)
    os.environ.update(saved)


def test_load_dotenv_reads_workspace_env_only(monkeypatch, tmp_path):
    import autocode.config as config_mod

    workspace = tmp_path / "workspace"
    project = workspace / "demo"
    project.mkdir(parents=True)
    workspace_env = workspace / ".env"
    workspace_env.write_text("AUTOCODE_MODEL=workspace-model\n", encoding="utf-8")

    loaded = []

    def fake_load_dotenv(path, override=False):
        loaded.append((pathlib.Path(path), override))
        for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)

    class FakeDotenvModule:
        @staticmethod
        def load_dotenv(path, override=False):
            fake_load_dotenv(path, override=override)

    monkeypatch.setitem(__import__("sys").modules, "dotenv", FakeDotenvModule())
    monkeypatch.chdir(project)
    monkeypatch.delenv("AUTOCODE_MODEL", raising=False)
    config_mod._load_dotenv()

    assert os.environ["AUTOCODE_MODEL"] == "workspace-model"
    assert loaded[-1][0] == workspace_env


def test_config_workspace_defaults_to_cwd(monkeypatch, tmp_path):
    monkeypatch.setattr("autocode.config._load_dotenv", lambda: None)
    workspace = tmp_path / "redis_work"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("AUTOCODE_WORKSPACE_ROOT", raising=False)

    config = Config.from_env()

    assert pathlib.Path(config.workspace_root) == workspace


# --- Context ---

def test_estimate_tokens():
    msgs = [{"role": "user", "content": "hello world"}]
    t = estimate_tokens(msgs)
    assert t > 0
    assert t < 100


def test_context_snip():
    ctx = ContextManager(max_tokens=3000)
    msgs = [
        {"role": "tool", "tool_call_id": "t1", "content": "x\n" * 1000},
    ]
    before = estimate_tokens(msgs)
    ctx._snip_tool_outputs(msgs)
    after = estimate_tokens(msgs)
    assert after < before


def test_context_compress():
    ctx = ContextManager(max_tokens=2000)
    msgs = []
    for i in range(20):
        msgs.append({"role": "user", "content": f"msg {i} " + "a" * 200})
        msgs.append({"role": "tool", "tool_call_id": f"t{i}", "content": "b" * 2000})
    before = estimate_tokens(msgs)
    result = ctx.maybe_compress(msgs, None)
    after = estimate_tokens(msgs)
    assert result.compressed is True
    assert result.layers
    assert after < before
    assert len(msgs) < 40  # should be compressed


def test_context_large_window_uses_larger_recent_tail_and_summary_budget():
    ctx = ContextManager(max_tokens=1_000_000)
    assert ctx._summary_keep_recent == 24
    assert ctx._collapse_keep_recent == 12
    assert ctx._summary_input_chars == 120_000


def test_memory_manager_reads_project_memory(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("项目规则\n", encoding="utf-8")
    (workspace / "CLAUDE.md").write_text("项目备注\n", encoding="utf-8")

    manager = MemoryManager(str(workspace))
    memory_path = manager.memory_file_path()
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.write_text("# Project Memory\n\n- 使用 conda 环境 langgraph\n", encoding="utf-8")
    block = manager.build_memory_block()

    assert "## Project Rules" in block
    assert "## Project Memory" in block
    assert "使用 conda 环境 langgraph" in block
    assert "## Recent Sessions" not in block


def test_memory_manager_refreshes_project_memory_without_duplicates(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = MemoryManager(str(workspace))

    class FakeLLM:
        def chat(self, messages, tools=None, on_token=None):
            return LLMResponse(content="- [fact] 当前项目使用 conda 环境 langgraph\n- [pitfall] README 中启动命令已过期")

    messages = [
        {"role": "user", "content": "请确认这个项目真实使用的环境和启动方式"},
        {"role": "tool", "content": "environment.yml: name: langgraph\nREADME.md: python old.py"},
    ]

    assert manager.refresh_project_memory(messages, FakeLLM()) is True
    assert manager.refresh_project_memory(messages, FakeLLM()) is False

    memory_path = manager.memory_file_path()
    content = memory_path.read_text(encoding="utf-8")
    assert "# Project Memory" in content
    assert content.count("- [fact] 当前项目使用 conda 环境 langgraph") == 1
    assert content.count("- [pitfall] README 中启动命令已过期") == 1


def test_memory_manager_refresh_rewrites_project_memory_with_llm_judgment(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = MemoryManager(str(workspace))
    memory_path = manager.memory_file_path()
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.write_text(
        "# Project Memory\n\n"
        "- Project root: `G:/demo` contains `main.py` and `utils.py`\n"
        "- Entry point: `python main.py`\n"
        "- [pitfall] PowerShell here-string must end at column 1\n"
        "- [pitfall] PowerShell here string must end at column 1\n",
        encoding="utf-8",
    )

    class FakeLLM:
        def chat(self, messages, tools=None, on_token=None):
            return LLMResponse(content="- [pitfall] PowerShell here-string terminator must stay at column 1")

    messages = [
        {"role": "user", "content": "总结这个项目里真正值得长期记住的坑"},
        {"role": "tool", "content": "PowerShell here-string terminator must stay at column 1"},
    ]

    assert manager.refresh_project_memory(messages, FakeLLM(), force=True) is True

    content = memory_path.read_text(encoding="utf-8")
    assert "Project root" not in content
    assert "Entry point" not in content
    assert content.count("- [pitfall] PowerShell here-string terminator must stay at column 1") == 1


def test_memory_manager_allows_model_selected_runbook_lines(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = MemoryManager(str(workspace))

    class FakeLLM:
        def chat(self, messages, tools=None, on_token=None):
            return LLMResponse(content=(
                "- 后台进程命名格式 `proc_20260610_xxx`，日志落在 `.autocode\\\\processes\\\\`\n"
                "- 跑通流程固定顺序：先启动 worker -> 再发消息 -> 最后 stop_process\n"
                "- [pitfall] Windows 下重定向 stdout 的子 Python 进程要强制 UTF-8 输出\n"
            ))

    messages = [
        {"role": "user", "content": "总结这里真正值得长期记住的内容"},
        {"role": "tool", "content": "背景进程日志和 UTF-8 输出验证"},
    ]

    assert manager.refresh_project_memory(messages, FakeLLM(), force=True) is True

    content = manager.memory_file_path().read_text(encoding="utf-8")
    assert "proc_20260610_xxx" in content
    assert "固定顺序" in content
    assert "强制 UTF-8 输出" in content


def test_memory_manager_can_schedule_async_refresh(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = MemoryManager(str(workspace))

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def clone(self):
            return self

        def chat(self, messages, tools=None, on_token=None):
            self.calls += 1
            return LLMResponse(content="- [fact] 项目真实 conda 环境是 langgraph")

    llm = FakeLLM()
    messages = [{"role": "user", "content": "请记住项目真实 conda 环境"}]

    assert manager.schedule_project_memory_refresh(messages, llm, force=True) is True
    manager.wait_for_pending_refresh(timeout=2)

    content = manager.memory_file_path().read_text(encoding="utf-8")
    assert "langgraph" in content
    assert llm.calls == 1


# --- Cost estimation ---

def test_cost_estimation_known_model():
    from autocode.llm import LLM
    llm = LLM.__new__(LLM)
    llm.model = "gpt-5.4"
    llm.total_prompt_tokens = 1_000_000
    llm.total_completion_tokens = 500_000
    cost = llm.estimated_cost
    assert cost is not None
    assert cost == 2.5 + 7.5  # $2.5/M in + $15/M out * 0.5M

def test_cost_estimation_unknown_model():
    from autocode.llm import LLM
    llm = LLM.__new__(LLM)
    llm.model = "some-custom-model"
    llm.total_prompt_tokens = 1000
    llm.total_completion_tokens = 500
    assert llm.estimated_cost is None


# --- Changed files tracking ---

def test_edit_tracks_changed_files(tmp_path):
    from autocode.tools.edit import _changed_files
    _changed_files.clear()
    edit = get_tool("edit_file")
    path = tmp_path / "sample.py"
    path.write_text("aaa\nbbb\n")
    edit.execute(file_path=str(path), old_string="aaa", new_string="zzz")
    assert any(str(path) in p for p in _changed_files)
    _changed_files.clear()


def test_write_tracks_changed_files(tmp_path):
    from autocode.tools.edit import _changed_files
    _changed_files.clear()
    write = get_tool("write_file")
    path = tmp_path / "tracked.txt"
    write.execute(file_path=str(path), content="tracked\n")
    assert any("tracked" not in p and path.name in p for p in _changed_files) or len(_changed_files) > 0
    _changed_files.clear()

