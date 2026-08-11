"""Tests for core modules: config, context, session, imports."""

import os
import pathlib

from autocode import Agent, LLM, Config, ALL_TOOLS, __version__
from autocode import cli as cli_module
from autocode.context import CompressionResult, ContextManager, MemoryManager, estimate_tokens
from autocode.llm import LLMResponse
from autocode.state import SessionState
from autocode.tools import get_tool


def test_version():
    assert __version__ == "0.3.0"


def test_cli_configures_redirected_stdio_as_utf8(monkeypatch):
    calls = []

    class _Stream:
        def reconfigure(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(cli_module.sys, "stdout", _Stream())
    monkeypatch.setattr(cli_module.sys, "stderr", _Stream())

    cli_module._configure_utf8_stdio()

    assert calls == [
        {"encoding": "utf-8", "errors": "replace"},
        {"encoding": "utf-8", "errors": "replace"},
    ]


def test_public_api_exports():
    """Users should be able to import key classes from the top-level package."""
    assert Agent is not None
    assert LLM is not None
    assert Config is not None
    assert len(ALL_TOOLS) == 15


def test_config_from_env(monkeypatch):
    monkeypatch.setattr("autocode.config._load_dotenv_values", lambda: {})
    os.environ["AUTOCODE_MODEL"] = "test-model"
    os.environ["AUTOCODE_TELEGRAM_ALLOWED_CHATS"] = "123,456"
    os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-test"
    os.environ["LANGFUSE_SECRET_KEY"] = "sk-test"
    os.environ["LANGFUSE_BASE_URL"] = "https://langfuse.example"
    os.environ["TAVILY_API_KEY"] = "tvly-test"
    c = Config.from_env()
    assert c.model == "test-model"
    assert c.telegram_allowed_chat_ids == (123, 456)
    assert c.langfuse_public_key == "pk-test"
    assert c.langfuse_secret_key == "sk-test"
    assert c.langfuse_base_url == "https://langfuse.example"
    assert c.tavily_api_key == "tvly-test"
    del os.environ["AUTOCODE_MODEL"]
    del os.environ["AUTOCODE_TELEGRAM_ALLOWED_CHATS"]
    del os.environ["LANGFUSE_PUBLIC_KEY"]
    del os.environ["LANGFUSE_SECRET_KEY"]
    del os.environ["LANGFUSE_BASE_URL"]
    del os.environ["TAVILY_API_KEY"]


def test_config_defaults(monkeypatch):
    monkeypatch.setattr("autocode.config._load_dotenv_values", lambda: {})
    # temporarily clear relevant env vars
    saved = {}
    for k in [
        "AUTOCODE_MODEL",
        "AUTOCODE_API_KEY",
        "AUTOCODE_BASE_URL",
        "AUTOCODE_PROVIDER",
        "AUTOCODE_MAX_TOKENS",
        "AUTOCODE_PERMISSION_MODE",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
        "TAVILY_API_KEY",
    ]:
        if k in os.environ:
            saved[k] = os.environ.pop(k)

    c = Config.from_env()
    assert c.model == ""
    assert c.api_key == ""
    assert c.base_url is None
    assert c.provider == "anthropic"
    assert c.langfuse_public_key == ""
    assert c.langfuse_secret_key == ""
    assert c.langfuse_base_url is None
    assert c.tavily_api_key == ""
    assert c.max_tokens == 32_000
    assert c.max_context_tokens == 1_000_000
    assert c.temperature == 0.0
    assert c.permission_mode == "ask"

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


def test_context_reserves_output_budget_before_compression_thresholds():
    ctx = ContextManager(max_tokens=100_000, output_reserve_tokens=20_000)

    assert ctx.input_budget_tokens == 80_000
    assert ctx._snip_at == 40_000
    assert ctx._summarize_at == 56_000
    assert ctx._collapse_at == 72_000


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


def test_agent_passes_real_usage_plus_trailing_estimate_into_compression(tmp_path):
    class _NoopLLM:
        def __init__(self):
            self.model = "fake"
            self.total_prompt_tokens = 0
            self.total_completion_tokens = 0
            self.total_cache_read_tokens = 0
            self.total_cache_miss_tokens = 0

    agent = Agent(llm=_NoopLLM(), workspace_root=str(tmp_path), permission_mode="full_access")
    agent.messages = [{"role": "user", "content": "prompt"}]
    agent._record_prompt_usage(4321)
    trailing = [{"role": "assistant", "content": "answer " * 12}]
    agent.messages.extend(trailing)
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

    assert captured["last_prompt_tokens"] == 4321 + estimate_tokens(trailing)


def test_agent_context_usage_prefers_real_prompt_and_caps_at_window(tmp_path):
    class _NoopLLM:
        model = "fake"

    agent = Agent(
        llm=_NoopLLM(),
        workspace_root=str(tmp_path),
        max_context_tokens=10_000,
    )
    agent.messages = [{"role": "user", "content": "short"}]
    agent._record_prompt_usage(4_321)

    usage = agent.context_usage()

    assert usage == {
        "used_tokens": 4_321,
        "window_tokens": 10_000,
        "remaining_tokens": 5_679,
        "used_percent": 43.2,
    }

    agent._record_prompt_usage(20_000)
    assert agent.context_usage()["used_tokens"] == 10_000
    assert agent.context_usage()["used_percent"] == 100.0


def test_agent_context_usage_drops_stale_real_usage_after_history_rewrite(tmp_path):
    class _NoopLLM:
        model = "fake"

    agent = Agent(
        llm=_NoopLLM(),
        workspace_root=str(tmp_path),
        max_context_tokens=10_000,
    )
    agent.messages = [{"role": "user", "content": "original"}]
    agent._record_prompt_usage(8_000)
    assert agent.context_usage()["used_tokens"] == 8_000

    agent.messages[0]["content"] = "rewritten"

    assert agent.context_usage()["used_tokens"] == estimate_tokens(agent.messages)


def test_agent_context_usage_adds_messages_appended_after_real_usage(tmp_path):
    class _NoopLLM:
        model = "fake"

    agent = Agent(
        llm=_NoopLLM(),
        workspace_root=str(tmp_path),
        max_context_tokens=10_000,
    )
    agent.messages = [{"role": "user", "content": "prompt"}]
    agent._record_prompt_usage(6_000)
    trailing = [
        {"role": "assistant", "content": "answer " * 12},
        {"role": "tool", "tool_call_id": "call-1", "content": "result " * 18},
        {"role": "user", "content": "continue " * 6},
    ]
    agent.messages.extend(trailing)

    assert agent.context_usage()["used_tokens"] == 6_000 + estimate_tokens(trailing)


def test_agent_context_usage_reanchors_without_double_counting_tail(tmp_path):
    class _NoopLLM:
        model = "fake"

    agent = Agent(
        llm=_NoopLLM(),
        workspace_root=str(tmp_path),
        max_context_tokens=10_000,
    )
    agent.messages = [{"role": "user", "content": "prompt"}]
    agent._record_prompt_usage(6_000)
    agent.messages.append({"role": "assistant", "content": "answer " * 12})
    assert agent.context_usage()["used_tokens"] > 6_000

    agent._record_prompt_usage(6_500)

    assert agent.context_usage()["used_tokens"] == 6_500


def test_agent_context_usage_restores_anchor_and_estimates_trailing_messages(tmp_path):
    class _NoopLLM:
        model = "fake"

    anchor = [{"role": "user", "content": "prompt"}]
    trailing = [{"role": "assistant", "content": "answer " * 12}]
    state = SessionState(
        session_id="restored",
        context_used_tokens=6_000,
        context_anchor_messages=len(anchor),
        context_anchor_digest=Agent._messages_digest(anchor),
    )
    agent = Agent(
        llm=_NoopLLM(),
        workspace_root=str(tmp_path),
        max_context_tokens=10_000,
    )

    agent.restore_session(state, [*anchor, *trailing])

    assert agent.context_usage()["used_tokens"] == 6_000 + estimate_tokens(trailing)


def test_agent_ignores_unanchored_usage_from_legacy_checkpoint(tmp_path):
    class _NoopLLM:
        model = "fake"

    agent = Agent(
        llm=_NoopLLM(),
        workspace_root=str(tmp_path),
        max_context_tokens=10_000,
    )
    messages = [{"role": "user", "content": "legacy"}]
    agent.restore_session(
        SessionState(session_id="legacy", context_used_tokens=9_000),
        messages,
    )

    assert agent.context_usage()["used_tokens"] == estimate_tokens(agent.messages)


def test_memory_manager_separates_rules_and_project_memory(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("项目规则\n", encoding="utf-8")
    manager = MemoryManager(str(workspace))
    memory_path = manager.memory_file_path()
    memory_path.parent.mkdir(parents=True)
    memory_path.write_text(
        "# Project Memory\n\n## 项目经验\n\n- 测试前运行 npm ci\n",
        encoding="utf-8",
    )

    assert "## Project Rules" in manager.build_rules_block()
    block = manager.build_project_memory_block(query="怎么运行测试")
    assert "测试前运行 npm ci" in block
    assert memory_path == workspace / ".autocode" / "PROJECT_MEMORY.md"


def test_memory_refresh_rewrites_one_markdown_file_from_task_trajectory(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = MemoryManager(str(workspace))
    memory_path = manager.memory_file_path()
    memory_path.parent.mkdir(parents=True)
    memory_path.write_text(
        "# Project Memory\n\n## 项目经验\n\n- 项目使用 npm\n",
        encoding="utf-8",
    )

    class FakeLLM:
        def __init__(self):
            self.calls = []

        def chat(self, messages, tools=None, on_token=None):
            self.calls.append(messages)
            return LLMResponse(
                content=(
                    "# Project Memory\n\n"
                    "## 用户偏好\n\n- 修改前先说明方案\n\n"
                    "## 项目经验\n\n- 项目统一使用 pnpm\n\n"
                    "## 已知问题\n\n- Windows 子进程需要强制 UTF-8\n"
                )
            )

    llm = FakeLLM()
    assert manager.refresh_project_memory(
        [
            {"role": "user", "content": "不要使用 npm，统一使用 pnpm"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "function": {
                            "name": "shell",
                            "arguments": '{"command":"pnpm test"}',
                        }
                    }
                ],
            },
            {
                "role": "tool",
                "tool_name": "shell",
                "tool_arguments": {"command": "pnpm test"},
                "content": "10 passed",
            },
        ],
        llm,
        force=True,
    )
    saved = memory_path.read_text(encoding="utf-8")
    assert "项目统一使用 pnpm" in saved
    assert "项目使用 npm" not in saved
    assert "## 用户偏好" in saved
    assert "## 项目经验" in saved
    assert "## 已知问题" in saved
    assert not memory_path.with_suffix(".tmp").exists()
    assert len(llm.calls) == 1
    prompt = llm.calls[0][1]["content"]
    assert "项目使用 npm" in prompt
    assert "tool call: shell" in prompt
    assert "tool result: shell" in prompt
    assert "10 passed" in prompt


def test_memory_refresh_no_change_preserves_existing_file(tmp_path):
    manager = MemoryManager(str(tmp_path))
    memory_path = manager.memory_file_path()
    memory_path.parent.mkdir(parents=True)
    original = "# Project Memory\n\n## 项目经验\n\n- 使用 pytest\n"
    memory_path.write_text(original, encoding="utf-8")

    class FakeLLM:
        def chat(self, messages, tools=None, on_token=None):
            return LLMResponse(content="NO_CHANGE")

    assert not manager.refresh_project_memory(
        [{"role": "user", "content": "解释一个临时错误"}],
        FakeLLM(),
        force=True,
    )
    assert memory_path.read_text(encoding="utf-8") == original


def test_memory_manager_can_schedule_async_refresh(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("run: python app.py", encoding="utf-8")
    manager = MemoryManager(str(workspace))

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def clone(self):
            return self

        def chat(self, messages, tools=None, on_token=None):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(content="README.md")
            return LLMResponse(content="[]")

    llm = FakeLLM()
    messages = [{"role": "user", "content": "请记住项目真实 conda 环境"}]

    assert manager.schedule_project_memory_refresh(messages, llm, force=True) is True


def test_memory_refresh_scheduling_does_not_call_model_on_response_thread(tmp_path):
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
    assert manager.schedule_project_memory_refresh(
        [{"role": "user", "content": "hello"}],
        FakeLLM(),
        force=True,
    )
    assert len(submitted) == 1


def test_agent_prompt_snapshot_keeps_rules_and_memory_in_stable_system_prompt(tmp_path):
    from autocode.agent import Agent

    (tmp_path / "AGENTS.md").write_text("规则一\n", encoding="utf-8")
    manager = MemoryManager(str(tmp_path))
    memory_path = manager.memory_file_path()
    memory_path.parent.mkdir(parents=True)
    memory_path.write_text(
        "# Project Memory\n\n## 项目经验\n\n- 项目记忆一\n",
        encoding="utf-8",
    )

    class _NoopLLM:
        def __init__(self):
            self.model = "fake"
            self.total_prompt_tokens = 0
            self.total_completion_tokens = 0
            self.total_cache_read_tokens = 0
            self.total_cache_miss_tokens = 0

    agent = Agent(llm=_NoopLLM(), workspace_root=str(tmp_path), permission_mode="full_access")
    agent._ensure_task("处理任务")
    agent.task_state.todos = [{"content": "读文件", "status": "pending"}]
    agent.messages = [{"role": "user", "content": "开始"}]

    snapshot = agent._create_prompt_snapshot(query="开始")
    request_messages = agent._request_messages(snapshot)
    second_request = agent._request_messages(snapshot)

    assert request_messages[0]["role"] == "system"
    assert "# Rules Memory" in request_messages[0]["content"]
    assert "## Progress Updates" in request_messages[0]["content"]
    assert "without exposing private chain-of-thought" in request_messages[0]["content"]
    assert "Do not repeat intentions as though the tool result had not arrived" in request_messages[0]["content"]
    assert "# Retrieved Project Memory" in request_messages[0]["content"]
    assert "项目记忆一" in request_messages[0]["content"]
    assert request_messages[0]["content"] == second_request[0]["content"]
    assert snapshot.digest == agent.task_state.prompt_snapshot["digest"]
    assert request_messages[-1]["role"] == "user"
    assert "[Runtime state for this turn." in request_messages[-1]["content"]
    assert "# Current Todo" in request_messages[-1]["content"]


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
    read = get_tool("read")
    edit = get_tool("edit_file")
    path = tmp_path / "sample.py"
    path.write_text("aaa\nbbb\n")
    read.execute(file_path=str(path))
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

