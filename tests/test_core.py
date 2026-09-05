"""Tests for core modules: config, context, session, imports."""

import os
import pathlib

import pytest

from autocode import Agent, LLM, Config, ALL_TOOLS, __version__
from autocode import cli as cli_module
from autocode.context import CompressionResult, ContextManager, MemoryManager, estimate_tokens
from autocode.llm import LLMResponse
from autocode.infra import WorkspaceFS
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
    assert len(ALL_TOOLS) == 25


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
        "AUTOCODE_APPROVAL_POLICY",
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
    assert c.approval_policy == "ask"

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


def test_context_does_not_snip_at_old_percentage_thresholds():
    from copy import deepcopy
    ctx = ContextManager(max_tokens=10000)
    for used in (5000, 7000, 9000):
        msgs = [{"role": "tool", "content": "middle evidence" * 200}]
        original = deepcopy(msgs)
        assert not ctx.maybe_compress(msgs, used_tokens=used).compressed
        assert msgs == original


def test_context_compress():
    ctx = ContextManager(max_tokens=2000)
    msgs = []
    for i in range(20):
        msgs.append({"role": "user", "content": f"msg {i} " + "a" * 200})
        msgs.append({"role": "tool", "tool_call_id": f"t{i}", "content": "b" * 2000})
    before = estimate_tokens(msgs)
    result = ctx.maybe_compress(msgs, checkpoint=lambda: 'durable notes')
    after = estimate_tokens(msgs)
    assert result.compressed is True
    assert result.layers
    assert after < before
    assert len(msgs) < 40  # should be compressed


def test_context_reset_is_summary_free_and_drops_old_window():
    ctx = ContextManager(max_tokens=2000)
    msgs = [{"role": "user", "content": "old prompt"}, {"role": "assistant", "content": "old answer"}]
    result = ctx.maybe_compress(msgs, used_tokens=2000, checkpoint=lambda: "history entry point")
    assert result.layers == ("new_context",)
    assert msgs == [{"role": "user", "message_kind": "task_context", "content": "history entry point"}]


def test_context_checkpoint_failure_does_not_mutate_even_snipped_outputs():
    from copy import deepcopy

    ctx = ContextManager(max_tokens=2000)
    msgs = [{"role": "user", "content": "goal"},
            {"role": "assistant", "content": "old " * 2000},
            {"role": "tool", "content": "logs\n" * 1000}]
    original = deepcopy(msgs)

    def fail():
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        ctx.maybe_compress(msgs, checkpoint=fail)
    assert msgs == original


def test_context_reserves_output_budget_before_compression_thresholds():
    ctx = ContextManager(max_tokens=100_000, output_reserve_tokens=20_000)
    assert ctx.input_budget_tokens == 80_000
    assert ctx.base_limit == 76_000
    assert not ctx.status(71_999)["remind"]
    assert ctx.status(72_000)["remind"]
    assert ctx.status(76_000)["fallback"]
    assert not ctx.status(79_999)["force"]
    assert ctx.status(80_000)["force"]


def test_context_can_trigger_compression_from_last_provider_usage():
    ctx = ContextManager(max_tokens=2000)
    msgs = [{"role": role, "content": f"turn {i}"}
            for i in range(3) for role in ("user", "assistant")]
    assert not ctx.maybe_compress(list(msgs)).compressed
    assert ctx.maybe_compress(list(msgs), used_tokens=2000,
                              checkpoint=lambda: "notes").compressed


def test_agent_passes_real_total_usage_plus_trailing_estimate_into_compression(tmp_path):
    class _NoopLLM:
        def __init__(self):
            self.model = "fake"
            self.total_prompt_tokens = 0
            self.total_completion_tokens = 0
            self.total_cache_read_tokens = 0
            self.total_cache_miss_tokens = 0

    agent = Agent(llm=_NoopLLM(), workspace_root=str(tmp_path), approval_policy="never")
    agent.messages = [{"role": "user", "content": "prompt"}]
    agent._record_context_usage(4321)
    trailing = [{"role": "assistant", "content": "answer " * 12}]
    agent.messages.extend(trailing)
    captured = {}

    def _fake_maybe_compress(messages, used_tokens=None, **kwargs):
        captured["used_tokens"] = used_tokens
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

    assert captured["used_tokens"] == 4321 + estimate_tokens(trailing)


def test_window_switch_does_not_trigger_project_summary(tmp_path):
    class _NoopLLM:
        model = "fake"
        _call_with_retry = object()

    agent = Agent(llm=_NoopLLM(), workspace_root=str(tmp_path), approval_policy="never")
    original_messages = [
        {"role": "user", "content": "old context"},
        {"role": "assistant", "content": "old answer"},
    ]
    agent.messages = [dict(message) for message in original_messages]
    scheduled = []
    agent.memory.schedule_project_memory_refresh = (
        lambda messages, llm, force=False: scheduled.append(list(messages)) or True
    )

    def _compress(messages, **kwargs):
        messages[0]["content"] = "compressed context"
        return CompressionResult(
            compressed=True,
            layers=("tool_snip",),
            before_tokens=100,
            after_tokens=50,
            before_messages=2,
            after_messages=2,
        )

    agent.context.maybe_compress = _compress
    agent._maybe_compress_messages()

    assert scheduled == []

    scheduled.clear()
    agent.context.maybe_compress = lambda *args, **kwargs: CompressionResult(
        compressed=False,
        layers=(),
        before_tokens=50,
        after_tokens=50,
        before_messages=2,
        after_messages=2,
    )
    agent._maybe_compress_messages()

    assert scheduled == []


def test_agent_context_usage_prefers_real_total_and_caps_at_window(tmp_path):
    class _NoopLLM:
        model = "fake"

    agent = Agent(
        llm=_NoopLLM(),
        workspace_root=str(tmp_path),
        max_context_tokens=10_000,
    )
    agent.messages = [{"role": "user", "content": "short"}]
    agent._record_context_usage(4_321)

    usage = agent.context_usage()

    assert usage == {
        "used_tokens": 4_321,
        "window_tokens": 10_000,
        "remaining_tokens": 5_679,
        "used_percent": 43.2,
    }

    agent._record_context_usage(20_000)
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
    agent._record_context_usage(8_000)
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
    agent._record_context_usage(6_000)
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
    agent._record_context_usage(6_000)
    agent.messages.append({"role": "assistant", "content": "answer " * 12})
    assert agent.context_usage()["used_tokens"] > 6_000

    agent._record_context_usage(6_500)

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


def test_agent_restores_persisted_sandbox_mode(tmp_path):
    class _NoopLLM:
        model = "fake"

    agent = Agent(
        llm=_NoopLLM(),
        workspace_root=str(tmp_path),
        sandbox_mode="workspace-write",
    )

    agent.restore_session(
        SessionState(session_id="restored", sandbox_mode="read-only"),
        [],
    )

    assert agent.sandbox_policy.mode == "read-only"


def test_agent_upgrades_legacy_session_with_effective_sandbox_mode(tmp_path):
    class _NoopLLM:
        model = "fake"

    state = SessionState(session_id="legacy")
    agent = Agent(
        llm=_NoopLLM(),
        workspace_root=str(tmp_path),
        sandbox_mode="read-only",
    )

    agent.restore_session(state, [])

    assert state.sandbox_mode == "read-only"


def test_agent_persists_mode_changes_and_fences_running_processes(tmp_path):
    class _NoopLLM:
        model = "fake"

    agent = Agent(llm=_NoopLLM(), workspace_root=str(tmp_path))
    agent.set_sandbox_mode("read-only")

    assert agent.session_state is not None
    assert agent.session_state.sandbox_mode == "read-only"

    agent.processes.has_running_processes = lambda: True
    with pytest.raises(RuntimeError, match="background processes"):
        agent.set_sandbox_mode("danger-full-access")
    assert agent.sandbox_policy.mode == "read-only"


def test_agent_permission_preset_updates_and_restores_both_knobs(tmp_path):
    class _NoopLLM:
        model = "fake"

    agent = Agent(llm=_NoopLLM(), workspace_root=str(tmp_path))
    selected = agent.set_permission_preset("danger-full-access")

    assert selected == "danger-full-access"
    assert agent.sandbox_policy.mode == "danger-full-access"
    assert agent.policy.approval_policy == "never"
    assert agent.session_state is not None
    assert agent.session_state.permission_preset == "danger-full-access"
    assert agent.session_state.sandbox_mode == "danger-full-access"
    assert agent.session_state.approval_policy == "never"

    restored = Agent(llm=_NoopLLM(), workspace_root=str(tmp_path))
    restored.restore_session(agent.session_state, [])

    assert restored.sandbox_policy.mode == "danger-full-access"
    assert restored.policy.approval_policy == "never"


def test_agent_permission_preset_change_is_atomic_when_sandbox_is_busy(tmp_path):
    class _NoopLLM:
        model = "fake"

    agent = Agent(llm=_NoopLLM(), workspace_root=str(tmp_path))
    agent.processes.has_running_processes = lambda: True

    with pytest.raises(RuntimeError, match="background processes"):
        agent.set_permission_preset("danger-full-access")

    assert agent.sandbox_policy.mode == "workspace-write"
    assert agent.policy.approval_policy == "ask"


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


def test_memory_manager_preserves_complete_authoritative_rules(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agents_rules = "agents-start\n" + "a" * 2_001 + "\nagents-end\n"
    claude_rules = "claude-start\n" + "c" * 1_201 + "\nclaude-end\n"
    (workspace / "AGENTS.md").write_text(agents_rules, encoding="utf-8")
    (workspace / "CLAUDE.md").write_text(claude_rules, encoding="utf-8")

    block = MemoryManager(str(workspace)).build_rules_block()

    assert agents_rules.rstrip() in block
    assert claude_rules.rstrip() in block


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


def test_memory_manager_applies_explicit_remember_update_and_forget(tmp_path):
    manager = MemoryManager(str(tmp_path))

    remembered = manager.apply_project_memory(
        action="remember",
        section="user_preference",
        content="项目使用 pnpm 管理前端依赖。",
    )
    assert remembered.startswith("Remembered")
    assert "项目使用 pnpm" in manager.build_project_memory_block()

    updated = manager.apply_project_memory(
        action="update",
        section="user_preference",
        content="项目使用 pnpm 管理前端依赖。",
        replacement="项目使用 bun 管理前端依赖。",
    )
    assert updated.startswith("Updated")
    saved = manager.build_project_memory_block()
    assert "项目使用 bun" in saved
    assert "项目使用 pnpm" not in saved

    forgotten = manager.apply_project_memory(
        action="forget",
        section="user_preference",
        content="项目使用 bun 管理前端依赖。",
    )
    assert forgotten.startswith("Forgot")
    assert not manager.memory_file_path().exists()


def test_memory_manager_enforces_exact_updates_secrets_and_twelve_item_limit(tmp_path):
    manager = MemoryManager(str(tmp_path))

    with pytest.raises(ValueError, match="not found"):
        manager.apply_project_memory(
            action="update",
            section="project_knowledge",
            content="不存在的旧记忆",
            replacement="新记忆",
        )

    with pytest.raises(ValueError, match="must not store secrets"):
        manager.apply_project_memory(
            action="remember",
            section="user_preference",
            content="API key = sk-1234567890abcdef",
        )

    for index in range(12):
        manager.apply_project_memory(
            action="remember",
            section="project_knowledge",
            content=f"项目记忆 {index}",
        )

    with pytest.raises(ValueError, match="maximum of 12 items"):
        manager.apply_project_memory(
            action="remember",
            section="known_issue",
            content="第十三条记忆",
        )


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

    agent = Agent(llm=_NoopLLM(), workspace_root=str(tmp_path), approval_policy="never")
    agent._ensure_turn("处理任务")
    agent.messages = [{"role": "user", "content": "开始"}]
    waited = []
    agent.memory.wait_for_pending_refresh = lambda timeout=None: waited.append(timeout)

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
    assert waited == [None]
    assert snapshot.digest == agent.turn_state.prompt_snapshot["digest"]
    assert request_messages == [
        request_messages[0],
        {"role": "user", "content": "开始"},
    ]
    assert second_request == request_messages


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
    edit = get_tool("edit_file").clone()
    edit._fs = WorkspaceFS(str(tmp_path))
    path = tmp_path / "sample.py"
    path.write_text("aaa\nbbb\n")
    read.execute(file_path=str(path))
    edit.execute(file_path=str(path), old_string="aaa", new_string="zzz")
    assert any(str(path) in p for p in _changed_files)
    _changed_files.clear()


def test_write_tracks_changed_files(tmp_path):
    from autocode.tools.edit import _changed_files
    _changed_files.clear()
    write = get_tool("write_file").clone()
    write._fs = WorkspaceFS(str(tmp_path))
    path = tmp_path / "tracked.txt"
    write.execute(file_path=str(path), content="tracked\n")
    assert any("tracked" not in p and path.name in p for p in _changed_files) or len(_changed_files) > 0
    _changed_files.clear()

