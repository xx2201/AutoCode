from corecoder import checkpoint as checkpoint_module
from corecoder.trace import TraceRecorder, format_trace, load_trace


def test_trace_recorder_aggregates_events(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "TASKS_DIR", tmp_path)

    recorder = TraceRecorder()
    recorder.handle("user_message", {"task_id": "task_trace", "content_preview": "hi"})
    recorder.handle("after_llm", {
        "task_id": "task_trace",
        "step_index": 1,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "tool_calls": 1,
    })
    recorder.handle("policy_decision", {
        "task_id": "task_trace",
        "tool_name": "bash",
        "decision": {"action": "confirm", "reason": "unsafe"},
    })
    recorder.handle("before_tool", {
        "task_id": "task_trace",
        "tool_name": "edit_file",
        "arguments": {"file_path": "corecoder/session.py"},
    })
    recorder.handle("after_tool", {
        "task_id": "task_trace",
        "tool_name": "edit_file",
        "result": "Edited corecoder/session.py",
    })
    recorder.handle("task_status", {"task_id": "task_trace", "status": "completed"})

    trace = load_trace("task_trace")
    assert trace is not None
    assert trace["status"] == "completed"
    assert trace["llm_calls"] == 1
    assert trace["tool_calls"] == 1
    assert trace["approval_requests"] == 1
    assert trace["prompt_tokens"] == 10
    assert trace["completion_tokens"] == 5
    assert "corecoder/session.py" in trace["modified_files"]


def test_format_trace_contains_key_fields():
    text = format_trace({
        "task_id": "task_1",
        "status": "completed",
        "steps": 2,
        "llm_calls": 1,
        "tool_calls": 1,
        "approval_requests": 0,
        "blocked_tool_calls": 0,
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "errors": 0,
        "modified_files": ["a.py"],
        "tools": {"edit_file": 1},
        "duration_seconds": 1.25,
    })
    assert "Task: task_1" in text
    assert "Tool calls: 1" in text
    assert "Modified files: a.py" in text
