"""Evidence-based task memory regression tests (no network or user sessions)."""

import json
from copy import deepcopy

import pytest

from autocode.agent import Agent
from autocode.context.manager import ContextManager
from autocode.context.task_history import TaskHistory, TaskNotes
from autocode.llm import LLMResponse
from autocode.message_content import content_text
from autocode.message_projection import serialize_anthropic_messages, serialize_chat_completions
from autocode.state import TranscriptLogger, load_checkpoint
from autocode.state import checkpoint as checkpoints
from autocode.tools.history import SearchHistoryTool, ReadHistoryTool


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoints, "SESSIONS_DIR", tmp_path / "sessions")
    return TaskHistory("history-test")


def append(history, message_id, text, turn="turn-1", role="user", **extra):
    TranscriptLogger().append_message(history.session_id, {
        "message_id": message_id, "turn_id": turn, "role": role, "content": text, **extra,
    })


class DeltaLLM:
    api_format = "chat_completions"

    def __init__(self):
        self.requests = []

    def chat(self, messages, **kwargs):
        payload = json.loads(content_text(messages[-1]["content"]))
        self.requests.append(payload)
        return LLMResponse(content=json.dumps({"upsert": [], "remove": []}))


def test_search_read_middle_of_long_output_pagination_and_scoping(history):
    append(history, "call", "running", role="assistant", tool_calls=[{
        "id": "tc", "type": "function", "function": {"name": "probe", "arguments": "{}"},
    }])
    text = "前置日志" * 3000 + "认证前连接被关闭 EXACT-729" + "后置日志" * 3000
    append(history, "result", text, role="tool", tool_name="probe", tool_call_id="tc")
    append(history, "next", "inspect the failure", role="assistant")
    append(TaskHistory("other-session"), "private", "EXACT-729 unrelated task")
    results = history.search("EXACT-729")["results"]
    assert [item["message_id"] for item in results] == ["result"]
    assert "认证前连接被关闭" in results[0]["snippet"]
    assert history.read("result")["previous_message_id"] == "call"
    assert history.read("result")["next_message_id"] == "next"
    recovered, offset = "", 0
    while True:
        page = history.read("result", offset, length=701)
        recovered += page["text"]
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert recovered == text
    with pytest.raises(ValueError, match="not found"):
        history.read("private")


def test_unicode_case_insensitive_search_preserves_original_character_offsets(history):
    append(history, "unicode", "ß" * 2000 + "Exact-Marker" + "尾部" * 500)
    result = history.search("exact-marker")["results"][0]
    assert result["offset"] == 1880
    assert "Exact-Marker" in result["snippet"]


def test_superseded_sources_and_derived_notes_are_unavailable(history):
    append(history, "old", "require red", turn="old-turn")
    append(history, "active", "require blue", turn="new-turn")
    notes = TaskNotes(history)
    notes._save({"version": 1, "cursor": [2, 12], "notes": {
        "color": {"key": "color", "kind": "constraint", "text": "red", "sources": ["old"]},
        "derived": {"key": "derived", "kind": "decision", "text": "red then blue", "sources": ["old", "active"]},
    }})
    TranscriptLogger().append_turn_superseded(history.session_id, {"superseded_turn_id": "old-turn"})
    assert history.search("red")["results"] == []
    with pytest.raises(ValueError):
        history.read("old")
    assert notes.load()["notes"] == {}
    assert history.read("active")["text"] == "require blue"


@pytest.mark.parametrize("api_format", ["chat_completions", "messages"])
def test_incremental_checkpoint_pages_all_evidence_and_resumes(history, api_format):
    long_text = "a" * 33000 + "tail evidence"
    append(history, "long", long_text)
    llm = DeltaLLM()
    llm.api_format = api_format
    notes = TaskNotes(history)
    notes.checkpoint(llm)
    pages = [page for request in llm.requests for page in request["new_evidence"]]
    assert "".join(page["text"] for page in pages) == long_text
    assert len(llm.requests) >= 3
    count = len(llm.requests)
    TaskNotes(TaskHistory(history.session_id)).checkpoint(llm)
    assert len(llm.requests) == count
    append(history, "new", "new failure")
    notes.checkpoint(llm)
    assert [page["message_id"] for page in llm.requests[-1]["new_evidence"]] == ["new"]


def test_partial_checkpoint_failure_resumes_only_uncommitted_pages(history):
    append(history, "long", "x" * 40000)
    notes, llm = TaskNotes(history), DeltaLLM()
    original_save = notes._save
    saves = []

    def fail_second(state):
        saves.append(deepcopy(state))
        if len(saves) == 2:
            raise OSError("disk full")
        original_save(state)

    notes._save = fail_second
    with pytest.raises(OSError, match="disk full"):
        notes.checkpoint(llm)
    committed = notes.load()["cursor"]
    retry = DeltaLLM()
    TaskNotes(history).checkpoint(retry)
    assert retry.requests[0]["new_evidence"][0]["offset"] == committed[1]


@pytest.mark.parametrize("response", [
    "not JSON", '{"upsert": [], "remove": ["unknown"]}',
    '{"upsert": [{"key":"invented","kind":"fact","text":"unproven","sources":["missing"]}],"remove":[]}',
    '{"upsert": [], "remove": [], "unexpected": true}',
])
def test_invalid_notes_never_advance_cursor(history, response):
    append(history, "source", "evidence")
    llm = type("Bad", (), {"chat": lambda self, **kwargs: LLMResponse(content=response)})()
    with pytest.raises(ValueError):
        TaskNotes(history).checkpoint(llm)
    assert TaskNotes(history).load()["cursor"] == [0, 0]


def test_exact_json_fence_is_supported_but_truncated_output_is_rejected(history):
    append(history, "source", "evidence")
    class Fenced:
        def chat(self, **kwargs):
            return LLMResponse(content='```json\n{"upsert": [], "remove": []}\n```')
    TaskNotes(history).checkpoint(Fenced())
    saved = TaskNotes(history).load()["cursor"]
    append(history, "new", "new evidence")
    class Truncated:
        def chat(self, **kwargs):
            return LLMResponse(content='{"upsert": [], "remove": []}', stop_reason="max_tokens")
    with pytest.raises(ValueError, match="truncated"):
        TaskNotes(history).checkpoint(Truncated())
    assert TaskNotes(history).load()["cursor"] == saved


def test_retrieval_results_are_not_recursively_indexed(history):
    append(history, "raw", "original failure")
    append(history, "copy", "original failure", role="tool", tool_name="read_history")
    append(history, "call", "", role="assistant", tool_calls=[{
        "id": "r", "function": {"name": "search_history", "arguments": '{"query":"original"}'},
    }])
    assert [item["message_id"] for item in history.search("original")["results"]] == ["raw"]


def test_original_images_are_opt_in_and_not_embedded_in_notes(history):
    from types import SimpleNamespace
    from autocode.tools.base import ToolResult

    media = {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}}
    append(history, "image", "diagnostic screenshot", role="tool", model_content=[media])
    assert history.search("screenshot")["results"][0]["media_count"] == 1
    assert "base64" not in json.dumps(history.search("screenshot"))
    llm = DeltaLLM()
    TaskNotes(history).checkpoint(llm)
    assert "base64" not in json.dumps(llm.requests)
    tool = ReadHistoryTool()
    tool._parent_agent = SimpleNamespace(session_state=SimpleNamespace(session_id=history.session_id))
    result = tool.execute("image", include_media=True)
    assert isinstance(result, ToolResult)
    assert result.model_content == [media]
    projected = serialize_anthropic_messages("system", [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "r", "type": "function", "function": {"name": "read_history", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "r", "content": result.text, "model_content": result.model_content},
    ])
    assert "aGVsbG8=" in json.dumps(projected)


def test_unversioned_legacy_history_is_not_revived_after_edit(history):
    TranscriptLogger().append_message(history.session_id, {"role": "user", "content": "legacy instruction"})
    assert history.read("transcript:1")["text"] == "legacy instruction"
    TranscriptLogger().append_turn_superseded(history.session_id, {"superseded_turn_id": "unknown-legacy-turn"})
    assert not history.search("legacy")["results"]
    with pytest.raises(ValueError):
        history.read("transcript:1")


def test_malformed_complete_transcript_fails_without_advancing_notes(history):
    path = checkpoints.session_dir(history.session_id) / "transcript.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"kind": invalid}\n', encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        TaskNotes(history).checkpoint(DeltaLLM())
    assert not (path.parent / "task_notes.json").exists()


def test_notes_updates_preserve_unmodified_entries_and_provenance(history):
    state = {"notes": {
        "goal": {"key": "goal", "kind": "goal", "text": "original objective", "sources": ["a"]},
        "failure": {"key": "failure", "kind": "failure", "text": "A failed", "sources": ["b"]},
    }}
    original_goal = deepcopy(state["notes"]["goal"])
    TaskNotes._apply(state, {"upsert": [{"key": "failure", "kind": "failure", "text": "A failed before auth", "sources": ["c"]}], "remove": []}, [{"message_id": "c"}])
    assert state["notes"]["goal"] == original_goal
    assert state["notes"]["failure"]["sources"] == ["b", "c"]
    oversized = {"upsert": [{"key": str(i), "kind": "fact", "text": "x", "sources": ["c"]} for i in range(21)], "remove": []}
    with pytest.raises(ValueError, match="budget"):
        TaskNotes._apply(state, oversized, [{"message_id": "c"}])
    assert len(state["notes"]) == 2


def test_atomic_replace_failure_preserves_previous_state(history, monkeypatch):
    append(history, "a", "fact")
    notes = TaskNotes(history)
    notes._save({"version": 1, "cursor": [0, 0], "notes": {}})
    def fail(*args):
        raise OSError("replace denied")
    monkeypatch.setattr("autocode.context.task_history.os.replace", fail)
    with pytest.raises(OSError, match="replace denied"):
        notes.checkpoint(DeltaLLM())
    assert notes.load()["cursor"] == [0, 0]
    assert not list(checkpoints.session_dir(history.session_id).glob(".task-notes-*"))


def test_emergency_window_keeps_latest_prompt_and_complete_tool_batch():
    messages = [{"role": "user", "content": "goal", "message_id": "prompt", "message_kind": "prompt"}]
    for index in range(8):
        calls = [{"id": f"call-{index}-{n}", "type": "function", "function": {"name": "probe", "arguments": "{}"}} for n in range(4)]
        messages.append({"role": "assistant", "content": "", "tool_calls": calls})
        messages.extend({"role": "tool", "tool_call_id": call["id"], "content": "x" * 200} for call in calls)
    result = ContextManager(max_tokens=2000).maybe_compress(messages, checkpoint=lambda: "notes")
    assert result.compressed
    assert messages[1]["message_id"] == "prompt"
    assert len(messages[2]["tool_calls"]) == 4
    for serializer in (serialize_anthropic_messages, serialize_chat_completions):
        projected = serializer("system", messages)
        assert "call-7-0" in json.dumps(projected)
        assert "call-6-0" not in json.dumps(projected)


def test_mid_turn_steer_does_not_replace_the_editable_original_prompt():
    messages = [{"role": "user", "message_kind": "prompt", "message_id": "original", "content": "original goal"}]
    messages.extend({"role": "assistant", "content": "old progress" * 100} for _ in range(5))
    messages.append({"role": "user", "message_kind": "steer", "content": "additional constraint"})
    messages.extend({"role": "assistant", "content": "new progress" * 100} for _ in range(5))
    ContextManager(max_tokens=1000).maybe_compress(messages, checkpoint=lambda: "goal and additional constraint")
    assert messages[1]["message_id"] == "original"


def test_agent_restore_edit_and_history_tool_binding(history, tmp_path):
    class LLM(DeltaLLM):
        model = "fake"
        total_prompt_tokens = total_completion_tokens = 0

        def chat(self, messages, tools=None, **kwargs):
            if "TASK_NOTES_DELTA" in str(messages[0]):
                payload = json.loads(content_text(messages[-1]["content"]))
                source = payload["new_evidence"][0]["message_id"]
                return LLMResponse(content=json.dumps({"upsert": [{"key": "goal", "kind": "goal", "text": "original goal", "sources": [source]}], "remove": []}))
            return LLMResponse(content="done")

    agent = Agent(LLM(), tools=[SearchHistoryTool(), ReadHistoryTool()], workspace_root=str(tmp_path), approval_policy="never")
    agent.chat("original goal")
    turn_id = agent.turn_state.turn_id
    for i in range(10):
        agent._append_message({"role": "assistant", "content": f"old step {i} " * 100})
    agent.context = ContextManager(max_tokens=1000)
    assert agent.compact_context().compressed
    session_id = agent.session_state.session_id
    restored = Agent(LLM(), tools=[SearchHistoryTool(), ReadHistoryTool()], workspace_root=str(tmp_path), approval_policy="never")
    restored.restore_session(*load_checkpoint(session_id))
    assert "original goal" in restored._task_notes().render()
    assert restored.edit_last_turn(turn_id, "replacement goal") == "done"
    assert not json.loads(restored.tool_registry["search_history"].execute("original"))["results"]
    assert restored._task_notes().load()["notes"] == {}
    assert "original goal" not in str(restored._request_messages())
    agent.close()
    restored.close()
