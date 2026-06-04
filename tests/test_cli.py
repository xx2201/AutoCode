from corecoder.cli import _autosave_session, _prompt_approval
from corecoder import session as session_module


class _Pending:
    tool_name = "bash"
    reason = "confirmation required"


def test_prompt_approval_accepts_slash_approve(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda: "/approve")
    assert _prompt_approval(_Pending()) == "approve"


def test_prompt_approval_accepts_slash_approve_all(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda: "/approve-all")
    assert _prompt_approval(_Pending()) == "approve_all"


def test_prompt_approval_rejects_slash_reject(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda: "/reject")
    assert _prompt_approval(_Pending()) is False


def test_autosave_session_creates_and_updates_same_file(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    sid = _autosave_session([{"role": "user", "content": "first"}], "model-a", None)
    assert sid is not None
    same = _autosave_session([{"role": "user", "content": "second"}], "model-b", sid)
    assert same == sid
    loaded = session_module.load_session(sid)
    assert loaded == ([{"role": "user", "content": "second"}], "model-b")
