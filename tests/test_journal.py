from autocode.state import checkpoint as checkpoint_module
from autocode.state import AuditLogger, load_events


def test_audit_logger_appends_events(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)

    logger = AuditLogger()
    logger.handle("user_message", {"session_id": "session_audit", "turn_id": "turn_audit", "content_preview": "hello"})
    logger.handle("after_llm", {"session_id": "session_audit", "turn_id": "turn_audit", "step_index": 1, "prompt_tokens": 10, "completion_tokens": 5})

    events = load_events("session_audit")
    assert len(events) == 2
    assert events[0]["event"] == "user_message"
    assert events[1]["event"] == "after_llm"

