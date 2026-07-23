"""Tests for core modules: config, context, session, imports."""

import os
import pathlib

from autocode import Agent, LLM, Config, ALL_TOOLS, __version__
from autocode.context import CompressionResult, ContextManager, MemoryManager, estimate_tokens
from autocode.llm import LLMResponse
from autocode.tools import get_tool


def test_version():
    assert __version__ == "0.3.0"


def test_public_api_exports():
    """Users should be able to import key classes from the top-level package."""
    assert Agent is not None
    assert LLM is not None
    assert Config is not None
    assert len(ALL_TOOLS) == 14


def test_config_from_env(monkeypatch):
    monkeypatch.setattr("autocode.config._load_dotenv_values", lambda: {})
    os.environ["AUTOCODE_MODEL"] = "test-model"
    os.environ["AUTOCODE_TELEGRAM_ALLOWED_CHATS"] = "123,456"
    os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-test"
    os.environ["LANGFUSE_SECRET_KEY"] = "sk-test"
    os.environ["LANGFUSE_BASE_URL"] = "https://langfuse.example"
    c = Config.from_env()
    assert c.model == "test-model"
    assert c.telegram_allowed_chat_ids == (123, 456)
    assert c.langfuse_public_key == "pk-test"
    assert c.langfuse_secret_key == "sk-test"
    assert c.langfuse_base_url == "https://langfuse.example"
    del os.environ["AUTOCODE_MODEL"]
    del os.environ["AUTOCODE_TELEGRAM_ALLOWED_CHATS"]
    del os.environ["LANGFUSE_PUBLIC_KEY"]
    del os.environ["LANGFUSE_SECRET_KEY"]
    del os.environ["LANGFUSE_BASE_URL"]


def test_config_defaults(monkeypatch):
    monkeypatch.setattr("autocode.config._load_dotenv_values", lambda: {})
    # temporarily clear relevant env vars
    saved = {}
    for k in [
        "AUTOCODE_MODEL",
        "AUTOCODE_API_KEY",
        "AUTOCODE_BASE_URL",
        "AUTOCODE_MAX_TOKENS",
        "AUTOCODE_AUTO_APPROVE",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
    ]:
        if k in os.environ:
            saved[k] = os.environ.pop(k)

    c = Config.from_env()
    assert c.model == ""
    assert c.api_key == ""
    assert c.base_url is None
    assert c.langfuse_public_key == ""
    assert c.langfuse_secret_key == ""
    assert c.langfuse_base_url is None
    assert c.max_tokens == 4096
    assert c.max_context_tokens == 1_000_000
    assert c.temperature == 0.0
    assert c.auto_approve is False

    os.environ.update(saved)


def test_config_ignores_openai_fallback(monkeypatch):
    monkeypatch.setattr("autocode.config._load_dotenv_values", lambda: {})
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


def test_load_dotenv_values_reads_workspace_env_only(monkeypatch, tmp_path):
    import autocode.config as config_mod

    workspace = tmp_path / "workspace"
    project = workspace / "demo"
    project.mkdir(parents=True)
    workspace_env = workspace / ".env"
    workspace_env.write_text("AUTOCODE_MODEL=workspace-model\n", encoding="utf-8")
    monkeypatch.chdir(project)
    values = config_mod._load_dotenv_values()

    assert values["AUTOCODE_MODEL"] == "workspace-model"


def test_config_workspace_defaults_to_cwd(monkeypatch, tmp_path):
    monkeypatch.setattr("autocode.config._load_dotenv_values", lambda: {})
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


def test_context_summarize_old_keeps_complete_recent_turns():
    ctx = ContextManager(max_tokens=2000)
    msgs = [
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "plan 1"},
        {"role": "tool", "tool_call_id": "t1", "content": "tool 1"},
        {"role": "user", "content": "turn 2"},
        {"role": "assistant", "content": "plan 2"},
        {"role": "tool", "tool_call_id": "t2", "content": "tool 2"},
        {"role": "assistant", "content": "done 2"},
        {"role": "user", "content": "turn 3"},
        {"role": "assistant", "content": "plan 3"},
    ]

    original_tail = msgs[3:]
    changed = ctx._summarize_old(msgs, llm=None, keep_recent=2)

    assert changed is True
    assert msgs[0]["content"].startswith("[Context compressed - conversation summary]")
    assert msgs[1]["role"] == "assistant"
    assert msgs[2:] == original_tail


def test_context_hard_collapse_prefers_last_complete_turn():
    ctx = ContextManager(max_tokens=2000)
    ctx._collapse_keep_recent = 1
    msgs = [
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "done 1"},
        {"role": "user", "content": "turn 2"},
        {"role": "assistant", "content": "step 2"},
        {"role": "tool", "tool_call_id": "t2", "content": "tool 2"},
    ]

    original_tail = msgs[2:]
    ctx._hard_collapse(msgs, llm=None)

    assert msgs[0]["content"].startswith("[Hard context reset]")
    assert msgs[1]["role"] == "assistant"
    assert msgs[2:] == original_tail


def test_context_large_window_uses_larger_recent_tail_and_summary_budget():
    ctx = ContextManager(max_tokens=1_000_000)
    assert ctx._summary_keep_recent == 5
    assert ctx._collapse_keep_recent == 2
    assert ctx._summary_input_chars == 120_000


def test_context_can_trigger_compression_from_last_real_prompt_tokens():
    ctx = ContextManager(max_tokens=2000)
    msgs = [
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "plan 1"},
        {"role": "user", "content": "turn 2"},
        {"role": "assistant", "content": "plan 2"},
        {"role": "user", "content": "turn 3"},
        {"role": "assistant", "content": "plan 3"},
    ]

    no_real_usage = ctx.maybe_compress([dict(m) for m in msgs], None)
    with_real_usage = ctx.maybe_compress([dict(m) for m in msgs], None, last_prompt_tokens=1500)

    assert no_real_usage.compressed is False
    assert with_real_usage.compressed is True
    assert "summarize_old" in with_real_usage.layers


def test_agent_passes_last_real_prompt_tokens_into_compression(tmp_path):
    class _NoopLLM:
        def __init__(self):
            self.model = "fake"
            self.total_prompt_tokens = 0
            self.total_completion_tokens = 0
            self.total_cache_read_tokens = 0
            self.total_cache_miss_tokens = 0

    agent = Agent(llm=_NoopLLM(), workspace_root=str(tmp_path), auto_approve=True)
    agent._last_prompt_tokens = 4321
    captured = {}

    def _fake_maybe_compress(messages, llm=None, last_prompt_tokens=0):
        captured["last_prompt_tokens"] = last_prompt_tokens
        return CompressionResult(
            compressed=False,
            layers=(),
            before_tokens=0,
            after_tokens=0,
            before_messages=len(messages),
            after_messages=len(messages),
        )

    agent.context.maybe_compress = _fake_maybe_compress
    agent._maybe_compress_messages()

    assert captured["last_prompt_tokens"] == 4321


def test_memory_manager_reads_project_memory(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("项目规则\n", encoding="utf-8")
    (workspace / "CLAUDE.md").write_text("项目备注\n", encoding="utf-8")

    manager = MemoryManager(str(workspace))
    memory_path = manager.memory_file_path()
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.write_text("# Project Memory\n\n- 使用 conda 环境 langgraph\n", encoding="utf-8")
    rules_block = manager.build_rules_block()
    project_memory_block = manager.build_project_memory_block()

    assert "## Project Rules" in rules_block
    assert "使用 conda 环境 langgraph" in project_memory_block
    assert "## Recent Sessions" not in project_memory_block


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


def test_memory_refresh_scheduling_does_not_scan_workspace_on_response_thread(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = MemoryManager(str(workspace))
    submitted = []

    class DeferredExecutor:
        def submit(self, function, *args):
            submitted.append((function, args))

            class DeferredFuture:
                def add_done_callback(self, callback):
                    self.callback = callback

            return DeferredFuture()

    class FakeLLM:
        def clone(self):
            return self

    manager._executor.shutdown(wait=False, cancel_futures=True)
    manager._executor = DeferredExecutor()
    manager._project_file_inventory_key = lambda: (_ for _ in ()).throw(
        AssertionError("workspace scan must stay in the background refresh")
    )

    assert manager.schedule_project_memory_refresh(
        [{"role": "user", "content": "hello"}],
        FakeLLM(),
        force=True,
    )
    assert len(submitted) == 1


def test_agent_request_messages_keep_rules_in_system_and_runtime_state_in_tail(tmp_path):
    from autocode.agent import Agent

    (tmp_path / "AGENTS.md").write_text("规则一\n", encoding="utf-8")
    memory_dir = tmp_path / ".autocode"
    memory_dir.mkdir()
    (memory_dir / "PROJECT_MEMORY.md").write_text("# Project Memory\n\n- 项目记忆一\n", encoding="utf-8")

    class _NoopLLM:
        def __init__(self):
            self.model = "fake"
            self.total_prompt_tokens = 0
            self.total_completion_tokens = 0
            self.total_cache_read_tokens = 0
            self.total_cache_miss_tokens = 0

    agent = Agent(llm=_NoopLLM(), workspace_root=str(tmp_path), auto_approve=True)
    agent._ensure_task("处理任务")
    agent.task_state.todos = [{"content": "读文件", "status": "pending"}]
    agent.messages = [{"role": "user", "content": "开始"}]

    request_messages = agent._request_messages()

    assert request_messages[0]["role"] == "system"
    assert "# Rules Memory" in request_messages[0]["content"]
    assert "# Task" not in request_messages[0]["content"]
    assert request_messages[-1]["role"] == "user"
    assert "[Runtime state for this turn." in request_messages[-1]["content"]
    assert "# Project Memory" in request_messages[-1]["content"]
    assert "# Current Todo" in request_messages[-1]["content"]


def test_memory_manager_uses_llm_selected_project_file_evidence(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "backend").mkdir(parents=True)
    (workspace / "frontend").mkdir(parents=True)
    (workspace / "README.md").write_text("启动方式: 先启动 backend 再启动 frontend", encoding="utf-8")
    (workspace / "backend" / "models.py").write_text(
        "from sqlalchemy import Column, Integer\n\nid = Column(Integer, primary_key=True)\n",
        encoding="utf-8",
    )
    (workspace / "frontend" / "vite.config.js").write_text(
        "export default { server: { proxy: { '/api': 'http://localhost:8000' } } }\n",
        encoding="utf-8",
    )
    (workspace / "backend" / ".venv").mkdir()
    (workspace / "backend" / ".venv" / "ignored.py").write_text("ignored", encoding="utf-8")

    manager = MemoryManager(str(workspace))

    class FakeLLM:
        def __init__(self):
            self.calls = []

        def chat(self, messages, tools=None, on_token=None):
            self.calls.append(messages)
            if len(self.calls) == 1:
                return LLMResponse(content="README.md\nfrontend/vite.config.js")
            return LLMResponse(content="- 后端监听 8000，前端通过 /api 代理联调")

    llm = FakeLLM()
    messages = [{"role": "user", "content": "总结这个 demo 真正值得记住的项目事实"}]

    assert manager.refresh_project_memory(messages, llm, force=True) is True
    assert len(llm.calls) == 2

    selector_prompt = llm.calls[0][1]["content"]
    summary_system_prompt = llm.calls[1][0]["content"]
    summary_user_prompt = llm.calls[1][1]["content"]
    assert "Project file inventory:" in selector_prompt
    assert "- README.md (" in selector_prompt
    assert "- backend/models.py (" in selector_prompt
    assert "- frontend/vite.config.js (" in selector_prompt
    assert ".venv/ignored.py" not in selector_prompt
    assert "Never infer from generic library best practices" in summary_system_prompt
    assert "[README.md]" in summary_user_prompt
    assert "[frontend/vite.config.js]" in summary_user_prompt
    assert "启动方式: 先启动 backend 再启动 frontend" in summary_user_prompt
    assert "proxy" in summary_user_prompt
    assert "[backend/models.py]" not in summary_user_prompt


def test_memory_refresh_key_changes_when_project_inventory_changes(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("first", encoding="utf-8")

    manager = MemoryManager(str(workspace))
    source = manager._flatten_messages([{"role": "user", "content": "hello"}])
    key_before = manager._memory_refresh_key(source, manager._project_file_inventory_key())

    (workspace / "README.md").write_text("second", encoding="utf-8")
    key_after = manager._memory_refresh_key(source, manager._project_file_inventory_key())

    assert key_before != key_after


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

