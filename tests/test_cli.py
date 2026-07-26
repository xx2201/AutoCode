from types import SimpleNamespace

import pytest
from rich.console import Console

from autocode.config import Config
from autocode.cli import (
    _clear_terminal,
    _context_toolbar,
    _format_token_count,
    _load_resumable_session,
    _prompt_approval,
    _render_conversation_history,
    _resume_candidates,
    _show_help,
    _welcome_panel,
    main,
)


def test_render_conversation_history_hides_legacy_visual_carrier(monkeypatch):
    console = Console(record=True, width=120)
    monkeypatch.setattr("autocode.cli.console", console)

    _render_conversation_history(
        [
            {"role": "user", "content": "真实问题"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Visual content loaded by tools: read."},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,abc"},
                    },
                ],
            },
            {"role": "assistant", "content": "真实回答"},
        ]
    )

    output = console.export_text(styles=False)
    assert "真实问题" in output
    assert "真实回答" in output
    assert "Visual content loaded by tools" not in output
    assert "[image]" not in output


class _Pending:
    tool_name = "bash"
    reason = "confirmation required"


def test_context_toolbar_uses_same_window_metrics_as_agent():
    agent = SimpleNamespace(
        context_usage=lambda: {
            "used_tokens": 127_000,
            "window_tokens": 258_000,
            "remaining_tokens": 131_000,
            "used_percent": 49.2,
        }
    )

    toolbar = _context_toolbar(agent)

    assert "49.2% used" in toolbar[1][1]
    assert "127.0k / 258.0k tokens" in toolbar[2][1]
    assert _format_token_count(1_500_000) == "1.5m"


def test_prompt_approval_accepts_dialog_approve(monkeypatch):
    monkeypatch.setattr("autocode.cli.pt_prompt", lambda *args, **kwargs: "/approve")
    assert _prompt_approval(_Pending()) == "approve"


def test_prompt_approval_accepts_dialog_approve_all(monkeypatch):
    monkeypatch.setattr("autocode.cli.pt_prompt", lambda *args, **kwargs: "/approve_all")
    assert _prompt_approval(_Pending()) == "approve_all"


def test_prompt_approval_rejects_dialog_selection(monkeypatch):
    monkeypatch.setattr("autocode.cli.pt_prompt", lambda *args, **kwargs: "/reject")
    assert _prompt_approval(_Pending()) is False


def test_prompt_approval_returns_none_when_postponed(monkeypatch):
    monkeypatch.setattr("autocode.cli.pt_prompt", lambda *args, **kwargs: "/later")
    assert _prompt_approval(_Pending()) is None


def test_resume_candidates_filter_current_workspace(monkeypatch):
    monkeypatch.setattr(
        "autocode.cli.list_sessions",
        lambda workspace_root=None, limit=10: [{"session_id": "session_1"}] if workspace_root == "G:/repo/a" else [],
    )

    assert _resume_candidates("G:/repo/a") == [{"session_id": "session_1"}]


def test_load_resumable_session_rejects_other_workspace(monkeypatch):
    monkeypatch.setattr(
        "autocode.cli.list_sessions",
        lambda workspace_root=None, limit=200: [{"session_id": "session_here"}] if workspace_root == "G:/repo/a" else [],
    )
    monkeypatch.setattr("autocode.cli.load_checkpoint", lambda session_id: ("loaded", session_id))

    assert _load_resumable_session("session_elsewhere", "G:/repo/a") is None
    assert _load_resumable_session("session_here", "G:/repo/a") == ("loaded", "session_here")


def test_clear_terminal_writes_clear_sequence_for_tty(monkeypatch):
    calls = []
    system_calls = []

    monkeypatch.setattr("autocode.cli._terminal_is_interactive", lambda: True)
    monkeypatch.setattr("autocode.cli.os.name", "nt")
    monkeypatch.setattr("autocode.cli.os.system", lambda command: system_calls.append(command) or 0)
    monkeypatch.setattr("autocode.cli._write_terminal_sequence", lambda text: calls.append(text))

    _clear_terminal()

    assert system_calls == ["cls"]
    assert calls == ["\x1b[3J\x1b[2J\x1b[H"]


def test_clear_terminal_skips_non_tty(monkeypatch):
    calls = []
    system_calls = []

    monkeypatch.setattr("autocode.cli._terminal_is_interactive", lambda: False)
    monkeypatch.setattr("autocode.cli.os.system", lambda command: system_calls.append(command) or 0)
    monkeypatch.setattr("autocode.cli._write_terminal_sequence", lambda text: calls.append(text))

    _clear_terminal()

    assert system_calls == []
    assert calls == []


def test_welcome_panel_contains_workspace_and_cat():
    panel = _welcome_panel(
        Config(model="MiniMax-M3", workspace_root="G:/repo/demo", base_url="https://example.com/v1"),
        "MiniMax-M3",
    )

    console = Console(record=True, width=120)
    console.print(panel)
    text = console.export_text(styles=False)

    assert "Workspace" in text
    assert "G:/repo/demo" in text
    assert "( o.o )" in text
    assert "Type /help for commands." in text


def test_help_mentions_mcp_command(monkeypatch):
    console = Console(record=True, width=120)
    monkeypatch.setattr("autocode.cli.console", console)

    _show_help()

    text = console.export_text(styles=False)
    assert "/mcp" in text


def test_cli_registers_current_workspace_before_running_agent(monkeypatch, tmp_path):
    registered = []
    config = Config(
        model="fake-model",
        api_key="secret",
        workspace_root=str(tmp_path),
    )

    class _Registry:
        def register(self, workspace_path):
            registered.append(workspace_path)

    class _McpManager:
        def start_background(self):
            return None

        def wait_until_ready(self):
            return None

    class _Agent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def _sync_mcp_tools(self):
            return None

    monkeypatch.setattr(
        "autocode.cli._parse_args",
        lambda: SimpleNamespace(
            model=None,
            base_url=None,
            api_key=None,
            prompt="inspect",
            resume=None,
        ),
    )
    monkeypatch.setattr("autocode.cli.Config.from_env", lambda: config)
    monkeypatch.setattr("autocode.cli.WorkspaceRegistry", _Registry)
    monkeypatch.setattr("autocode.cli.LLM", lambda **kwargs: object())
    monkeypatch.setattr("autocode.cli.get_shared_mcp_manager", lambda *args: _McpManager())
    monkeypatch.setattr("autocode.cli.build_agent_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr("autocode.cli.Agent", _Agent)
    monkeypatch.setattr("autocode.cli._run_once", lambda agent, prompt: None)

    main()

    assert registered == [str(tmp_path)]

