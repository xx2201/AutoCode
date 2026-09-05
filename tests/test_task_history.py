"""Context lifecycle, provenance, persistence and retrieval contracts; isolated sessions."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from autocode.agent import Agent
from autocode.context.manager import ContextManager
from autocode.context.task_history import TaskHistory, TaskNotes
from autocode.llm import LLMResponse, ToolCall
from autocode.state import TranscriptLogger, load_checkpoint, SessionState, TurnState
from autocode.state import checkpoint as checkpoints
from autocode.tools.history import SearchHistoryTool, ReadHistoryTool, ListHistoryTool
from autocode.tools.notes import WriteNoteTool, AppendNoteTool, ReadNoteTool, ListNotesTool, SearchNotesTool, NewContextTool
from autocode.tools.base import ToolResult
from autocode.message_projection import serialize_anthropic_messages, serialize_chat_completions

TOOLS = [SearchHistoryTool, ReadHistoryTool, ListHistoryTool, WriteNoteTool, AppendNoteTool,
         ReadNoteTool, ListNotesTool, SearchNotesTool, NewContextTool]


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoints, "SESSIONS_DIR", tmp_path / "sessions")
    return TaskHistory("history-test")


def append(history, message_id, text, turn="turn-1", role="user", **extra):
    TranscriptLogger().append_message(history.session_id, {
        "message_id": message_id, "turn_id": turn, "role": role, "content": text, **extra,
    })


class DoneLLM:
    model = "fake"
    total_prompt_tokens = total_completion_tokens = 0

    def chat(self, messages, **kwargs):
        assert "TASK_NOTES_DELTA" not in str(messages)
        return LLMResponse(content="done")


def make_agent(tmp_path, llm=None):
    return Agent(llm or DoneLLM(), tools=[tool() for tool in TOOLS],
                 workspace_root=str(tmp_path), approval_policy="never")


@pytest.mark.parametrize("used,reminded,fallback,switched", [
    (175_000, False, False, False),
        (215_656, True, False, False),
        (227_636, True, True, False),
    (239_616, False, False, True),
])
def test_context_decisions_share_valid_anchor_despite_larger_character_estimate(
        tmp_path, used, reminded, fallback, switched):
    agent = make_agent(tmp_path)
    try:
        agent.chat("review this task")
        agent.context = ContextManager(256_000, 16_384)
        agent._append_message({"role": "assistant", "content": "x" * 720_000})
        agent._record_context_usage(used)
        assert agent._estimated_context_tokens() == used
        result = agent._maybe_compress_messages()
        assert result.compressed is switched
        assert result.before_tokens == used
        assert agent.session_state.context_reminded is reminded
        assert agent.session_state.context_fallback is fallback
    finally:
        agent.close()


def test_invalidated_anchor_uses_current_content_for_hard_limit(tmp_path):
    agent = make_agent(tmp_path)
    try:
        agent.chat("review this task")
        agent.context = ContextManager(256_000, 16_384)
        agent._append_message({"role": "assistant", "content": "x" * 720_000})
        agent._record_context_usage(175_000)
        agent.messages[-1]["content"] += " changed"
        assert agent._valid_last_context_tokens() == 0
        assert agent._estimated_context_tokens() >= agent.context.input_budget_tokens
        assert agent._maybe_compress_messages().compressed
    finally:
        agent.close()


def test_history_full_read_pagination_scope_and_images(history):
    append(history, "call", "probe", role="assistant", tool_calls=[{
        "id": "tc", "type": "function", "function": {"name": "probe", "arguments": "{}"}}])
    text = "前置日志" * 3000 + "认证前连接关闭 EXACT-729" + "后置日志" * 3000
    media = {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}}
    append(history, "result", text, role="tool", tool_name="probe", tool_call_id="tc", model_content=[media])
    append(history, "next", "inspect", role="assistant")
    append(TaskHistory("other"), "private", "EXACT-729")
    assert [r["message_id"] for r in history.search("EXACT-729")["results"]] == ["result"]
    assert history.read("result")["text"] == text
    assert history.read("result")["previous_message_id"] == "call"
    assert history.read("result")["next_message_id"] == "next"
    assert history.read("result", include_media=True)["media"] == [media]
    assert "base64" not in json.dumps(history.search("EXACT-729"))
    recovered, offset = "", 0
    while True:
        page = history.read("result", offset, length=701)
        recovered += page["text"]
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert recovered == text
    with pytest.raises(ValueError):
        history.read("private")


def test_notes_store_complete_files_and_do_not_inject_them(history):
    append(history, "evidence", "fact")
    notes = TaskNotes(history)
    text = "保留完整因果关系\n" * 1000
    for index in range(25):
        notes.write(f"details/{index}.md", text, ["evidence"])
    notes.write("index.md", "See details/0.md", ["evidence"])
    notes.write("index.md", "\nNext task", append=True)
    assert notes.read("details/0.md")["text"] == text
    assert notes.read("index.md", start_line=-1)["text"] == "Next task"
    assert notes.search("因果", prefix="details/0", limit=1)["next_offset"] == 1
    page = notes.list_files(limit=20)
    assert page["next_offset"] == 20
    assert len(notes.list_files(offset=20)["files"]) == 6
    assert len(notes.render()) < 1000 and text not in notes.render()
    restored = TaskNotes(TaskHistory(history.session_id))
    assert restored.load() == notes.load()


@pytest.mark.parametrize("path", ["../x", "/x", "a//b", "./x", "a/../x", "a\\b", ""])
def test_virtual_note_paths_cannot_escape(history, path):
    with pytest.raises(ValueError):
        TaskNotes(history).write(path, "text")


def test_utf8_file_limit_is_not_a_character_limit(history):
    notes = TaskNotes(history)
    notes.write("large.md", "x" * 1_000_000)
    with pytest.raises(ValueError, match="UTF-8"):
        notes.write("large.md", "字" * 400_000)
    assert notes.read("large.md")["total_chars"] == 1_000_000
    notes.write("second.md", "additional storage is allowed")


def test_edit_invalidates_notes_even_when_sources_were_not_explicit(history):
    append(history, "old", "require red", turn="old")
    notes = TaskNotes(history)
    notes.write("index.md", "red")
    notes.write("detail.md", "derived red", ["old"])
    TranscriptLogger().append_turn_superseded(history.session_id, {"superseded_turn_id": "old"})
    append(history, "new", "blue", turn="new")
    assert not notes.load()["files"]
    assert not history.search("red")["results"]
    with pytest.raises(ValueError):
        history.read("old")
    notes.write("index.md", "blue", ["new"])
    assert notes.read("index.md")["text"] == "blue"


def test_atomic_write_failure_and_legacy_migration(history, monkeypatch):
    append(history, "a", "fact")
    legacy = checkpoints.session_dir(history.session_id) / "task_notes.json"
    legacy.write_text(json.dumps({"version": 1, "notes": {
        "goal": {"text": "old objective", "sources": ["a"]}}, "cursor": [1, 4]}), encoding="utf-8")
    notes = TaskNotes(history)
    assert notes.read("migrated/goal.md")["text"] == "old objective"
    original = deepcopy(notes.load())
    def fail(*args):
        raise OSError("replace denied")
    monkeypatch.setattr("autocode.context.task_history.os.replace", fail)
    with pytest.raises(OSError):
        notes.write("index.md", "new")
    assert notes.load() == original
    assert legacy.exists()
    assert not list(legacy.parent.glob(".notes-*"))


def test_history_lists_windows_and_excludes_retrieval_copies(history):
    append(history, "old", "evidence", window_id=0)
    append(history, "new", "later", window_id=1)
    append(history, "copy", "evidence", role="tool", tool_name="read_history")
    assert history.list_items(window_id=0)["total"] == 1
    assert history.list_items(limit=1)["next_offset"] == 1
    assert [r["message_id"] for r in history.search("evidence")["results"]] == ["old"]


def test_new_context_tool_defers_reset_and_preserves_processes(history, tmp_path):
    agent = make_agent(tmp_path)
    try:
        agent.chat("original goal")
        before = deepcopy(agent.messages)
        agent.tool_registry["write_note"].execute(path="index.md", text="continue original goal")
        agent.tool_registry["new_context"].execute()
        assert agent.messages == before
        process_manager = agent.processes
        agent._maybe_compress_messages()
        assert len(agent.messages) == 1
        assert agent.session_state.context_window == 1
        assert not agent.session_state.new_context_requested
        assert agent.processes is process_manager
        assert agent._task_notes().read("index.md")["text"] == "continue original goal"
        for serialize in (serialize_anthropic_messages, serialize_chat_completions):
            assert "original goal" not in str(serialize("system", agent.messages))
    finally:
        agent.close()


def test_reminder_fallback_forced_reset_restore_and_edit(history, tmp_path):
    agent = make_agent(tmp_path)
    restored = make_agent(tmp_path)
    try:
        agent.chat("original goal")
        turn = agent.turn_state.turn_id
        agent.context = ContextManager(10000, 1000, reminder_tokens=1000, fallback_buffer_tokens=1000)
        agent._record_context_usage(7000)
        agent._maybe_compress_messages()
        assert agent.session_state.context_reminded
        count = len(agent.messages)
        agent._maybe_compress_messages()
        assert len(agent.messages) == count
        agent._record_context_usage(8000)
        agent._maybe_compress_messages()
        assert agent.session_state.context_fallback
        agent._task_notes().write("index.md", "original goal")
        agent.persist_session()
        restored.restore_session(*load_checkpoint(agent.session_state.session_id))
        assert restored.session_state.context_fallback
        agent._record_context_usage(9000)
        assert agent._maybe_compress_messages().compressed
        agent.persist_session()
        restored.restore_session(*load_checkpoint(agent.session_state.session_id))
        assert restored.session_state.context_window == 1
        assert not restored.session_state.context_reminded
        assert restored.edit_last_turn(turn, "replacement goal") == "done"
        assert not restored._task_notes().load()["files"]
        assert not restored._task_notes().history.search("original")["results"]
    finally:
        agent.close()
        restored.close()


def test_note_tools_never_speculatively_write(history, tmp_path):
    from autocode.runtime.streaming import StreamingToolExecutor
    agent = make_agent(tmp_path)
    try:
        agent.chat("task")
        executor = StreamingToolExecutor(runtime=agent.runtime, turn_state=agent.turn_state,
                                         session_id=agent.session_state.session_id)
        call = ToolCall(id="write", name="write_note", arguments={"path": "index.md", "text": "must not save"})
        assert not executor.add_tool(call)
        executor.discard()
        assert not agent._task_notes().load()["files"]
        assert not agent.session_state.new_context_requested
    finally:
        agent.close()


def test_budgeted_reads_preserve_next_offset(history, tmp_path):
    agent = make_agent(tmp_path)
    try:
        agent.chat("task")
        agent.context = ContextManager(20000, 1000)
        text = "中间日志" * 3000
        agent._task_notes().write("long.md", text)
        result = json.loads(agent.tool_registry["read_note"].execute(path="long.md"))
        assert result["next_offset"] and text.startswith(result["text"])
        assert len(json.dumps(result, ensure_ascii=False)) <= 3000
        assert agent._task_notes().read("long.md")["text"] == text
    finally:
        agent.close()


def test_malformed_history_prevents_window_replacement(history, tmp_path):
    agent = make_agent(tmp_path)
    try:
        agent.chat("goal")
        path = checkpoints.session_dir(agent.session_state.session_id) / "transcript.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write("{bad json}\n")
        original = deepcopy(agent.messages)
        with pytest.raises(json.JSONDecodeError):
            agent.compact_context()
        assert agent.messages == original
    finally:
        agent.close()


@pytest.mark.parametrize("api_format", ["messages", "chat_completions"])
def test_window_switch_waits_for_complete_tool_batch(history, tmp_path, api_format):
    class BatchLLM(DoneLLM):
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(tool_calls=[
                    ToolCall(id="save", name="write_note", arguments={"path": "index.md", "text": "first"}),
                    ToolCall(id="switch", name="new_context", arguments={}),
                    ToolCall(id="append", name="append_note", arguments={"path": "index.md", "text": " second"}),
                ])
                assert "first second" not in str(messages)
            return LLMResponse(content="done")

    llm = BatchLLM()
    llm.api_format = api_format
    agent = make_agent(tmp_path, llm)
    at_switch = []
    agent.hooks.on("context_compaction", lambda event, payload: at_switch.append(
        (agent._task_notes().read("index.md")["text"],
         [e["message"].get("tool_call_id") for _, e in agent._task_notes().history.entries()
          if e.get("kind") == "message" and e["message"].get("role") == "tool"])))
    try:
        assert agent.chat("batch fixture") == "done"
        assert at_switch == [("first second", ["save", "switch", "append"])]
        assert agent.session_state.context_window == 1
    finally:
        agent.close()
