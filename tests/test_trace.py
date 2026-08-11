from autocode.state import checkpoint as checkpoint_module
from autocode.state import TraceRecorder, format_trace, load_trace


def test_trace_recorder_aggregates_events(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)

    recorder = TraceRecorder()
    recorder.handle("user_message", {"session_id": "session_trace", "turn_id": "turn_trace", "content_preview": "hi"})
    recorder.handle("after_llm", {
        "session_id": "session_trace",
        "turn_id": "turn_trace",
        "step_index": 1,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cache_read_tokens": 6,
        "cache_miss_tokens": 4,
        "tool_calls": 1,
    })
    recorder.handle("context_compaction", {
        "session_id": "session_trace",
        "turn_id": "turn_trace",
        "saved_tokens": 120,
        "layers": ["tool_snip", "summarize_old"],
    })
    recorder.handle("policy_decision", {
        "session_id": "session_trace",
        "turn_id": "turn_trace",
        "tool_name": "shell_command",
        "decision": {"action": "confirm", "reason": "unsafe"},
    })
    recorder.handle("before_tool", {
        "session_id": "session_trace",
        "turn_id": "turn_trace",
        "tool_name": "edit_file",
        "arguments": {"file_path": "autocode/checkpoint.py"},
    })
    recorder.handle("after_tool", {
        "session_id": "session_trace",
        "turn_id": "turn_trace",
        "tool_name": "edit_file",
        "result": "Edited autocode/checkpoint.py",
    })
    recorder.handle("turn_status", {"session_id": "session_trace", "turn_id": "turn_trace", "status": "completed"})

    trace = load_trace("session_trace")
    assert trace is not None
    assert trace["status"] == "completed"
    assert trace["llm_calls"] == 1
    assert trace["tool_calls"] == 1
    assert trace["approval_requests"] == 1
    assert trace["prompt_tokens"] == 10
    assert trace["completion_tokens"] == 5
    assert trace["cache_read_tokens"] == 6
    assert trace["cache_miss_tokens"] == 4
    assert trace["compactions"] == 1
    assert trace["cache_segments"] == 2
    assert trace["context_saved_tokens"] == 120
    assert "autocode/checkpoint.py" in trace["modified_files"]


def test_format_trace_contains_key_fields():
    text = format_trace({
        "session_id": "session_1",
        "current_turn_id": "turn_1",
        "status": "completed",
        "steps": 2,
        "llm_calls": 1,
        "tool_calls": 1,
        "approval_requests": 0,
        "blocked_tool_calls": 0,
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "cache_read_tokens": 7,
        "cache_miss_tokens": 3,
        "compactions": 2,
        "cache_segments": 3,
        "context_saved_tokens": 180,
        "errors": 0,
        "modified_files": ["a.py"],
        "tools": {"edit_file": 1},
        "duration_seconds": 1.25,
    })
    assert "Session: session_1" in text
    assert "Current Turn: turn_1" in text
    assert "Tool calls: 1" in text
    assert "Prompt cache hit rate: 70.0%" in text
    assert "Cache segments: 3" in text
    assert "Modified files: a.py" in text

