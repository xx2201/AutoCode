from autocode.cli import _load_resumable_session, _prompt_approval, _resume_candidates


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

