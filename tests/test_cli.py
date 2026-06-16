import pytest
from rich.console import Console

from autocode.cli import _clear_terminal, _load_resumable_session, _prompt_approval, _resume_candidates, _show_help, _welcome_panel
from autocode.config import Config


class _Pending:
    tool_name = "bash"
    reason = "confirmation required"


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

