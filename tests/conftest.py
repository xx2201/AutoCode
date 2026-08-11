from __future__ import annotations

import pytest

from autocode.state import checkpoint as checkpoint_module


@pytest.fixture(autouse=True)
def isolate_session_storage(tmp_path, monkeypatch):
    """Keep every test session out of the user's persistent AutoCode storage."""
    sessions_dir = tmp_path / "autocode-sessions"
    monkeypatch.setenv("AUTOCODE_SESSIONS_DIR", str(sessions_dir))
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", sessions_dir)
