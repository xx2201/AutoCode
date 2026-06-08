"""Tests for core modules: config, context, session, imports."""

import os
import pathlib

from autocode import Agent, LLM, Config, ALL_TOOLS, __version__
from autocode.context import ContextManager, MemoryManager, estimate_tokens
from autocode.llm import LLMResponse
from autocode.state import save_session, load_session, list_sessions
from autocode.tools import get_tool


def test_version():
    assert __version__ == "0.3.0"


def test_public_api_exports():
    """Users should be able to import key classes from the top-level package."""
    assert Agent is not None
    assert LLM is not None
    assert Config is not None
    assert len(ALL_TOOLS) == 8


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


def test_load_dotenv_falls_back_to_repo_env(monkeypatch, tmp_path):
    import autocode.config as config_mod

    repo_root = tmp_path / "repo"
    pkg_dir = repo_root / "autocode"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "config.py").write_text("# stub\n", encoding="utf-8")
    repo_env = repo_root / ".env"
    repo_env.write_text("AUTOCODE_MODEL=repo-model\n", encoding="utf-8")

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
    monkeypatch.setattr(config_mod, "__file__", str(pkg_dir / "config.py"))
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("AUTOCODE_MODEL", raising=False)
    config_mod._load_dotenv()

    assert os.environ["AUTOCODE_MODEL"] == "repo-model"
    assert loaded[-1][0] == repo_env


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
    (workspace / ".autocode").mkdir()
    (workspace / ".autocode" / "PROJECT_MEMORY.md").write_text("# Project Memory\n\n- 使用 conda 环境 langgraph\n", encoding="utf-8")
    monkeypatch.setattr("autocode.context.memory.TaskStore.recent_task_summaries", staticmethod(lambda limit=3: ["- task_x (completed, step 3, model test)"]))

    manager = MemoryManager(str(workspace))
    block = manager.build_memory_block()

    assert "## Project Rules" in block
    assert "## Project Memory" in block
    assert "使用 conda 环境 langgraph" in block
    assert "## Recent Tasks" in block


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

    memory_path = workspace / ".autocode" / "PROJECT_MEMORY.md"
    content = memory_path.read_text(encoding="utf-8")
    assert "# Project Memory" in content
    assert content.count("- [fact] 当前项目使用 conda 环境 langgraph") == 1
    assert content.count("- [pitfall] README 中启动命令已过期") == 1


def test_memory_manager_refresh_rewrites_project_memory_with_llm_judgment(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".autocode").mkdir()
    (workspace / ".autocode" / "PROJECT_MEMORY.md").write_text(
        "# Project Memory\n\n"
        "- Project root: `G:/demo` contains `main.py` and `utils.py`\n"
        "- Entry point: `python main.py`\n"
        "- [pitfall] PowerShell here-string must end at column 1\n"
        "- [pitfall] PowerShell here string must end at column 1\n",
        encoding="utf-8",
    )

    manager = MemoryManager(str(workspace))

    class FakeLLM:
        def chat(self, messages, tools=None, on_token=None):
            return LLMResponse(content="- [pitfall] PowerShell here-string terminator must stay at column 1")

    messages = [
        {"role": "user", "content": "总结这个项目里真正值得长期记住的坑"},
        {"role": "tool", "content": "PowerShell here-string terminator must stay at column 1"},
    ]

    assert manager.refresh_project_memory(messages, FakeLLM(), force=True) is True

    content = (workspace / ".autocode" / "PROJECT_MEMORY.md").read_text(encoding="utf-8")
    assert "Project root" not in content
    assert "Entry point" not in content
    assert content.count("- [pitfall] PowerShell here-string terminator must stay at column 1") == 1


# --- Session ---

def test_session_save_load():
    msgs = [{"role": "user", "content": "test message"}]
    sid = save_session(msgs, "test-model", "pytest_test_session")
    loaded = load_session("pytest_test_session")
    assert loaded is not None
    assert loaded[0] == msgs
    assert loaded[1] == "test-model"
    # cleanup
    pathlib.Path.home().joinpath(".autocode/sessions/pytest_test_session.json").unlink()


def test_session_name_is_sanitized():
    msgs = [{"role": "user", "content": "test message"}]
    sid = save_session(msgs, "test-model", "../Research Notes!")

    assert sid == "Research-Notes"
    path = pathlib.Path.home().joinpath(".autocode/sessions/Research-Notes.json")
    assert path.exists()
    assert load_session("../Research Notes!") is not None
    path.unlink()


def test_session_is_written_as_utf8():
    msgs = [{"role": "user", "content": "你好，世界"}]
    save_session(msgs, "test-model", "utf8_session")
    path = pathlib.Path.home().joinpath(".autocode/sessions/utf8_session.json")
    raw = path.read_bytes()
    assert b"\xe4\xbd\xa0\xe5\xa5\xbd" in raw
    path.unlink()


def test_session_not_found():
    assert load_session("nonexistent_session_id") is None


def test_list_sessions():
    sessions = list_sessions()
    assert isinstance(sessions, list)


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

