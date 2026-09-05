import json
from types import SimpleNamespace

from autocode.context.manager import ContextManager
from eval.context_conversation import PROMPTS, ReviewSources, observations


def test_unprompted_stages_do_not_request_notes_or_window_switch():
    for _, prompt in PROMPTS[:2]:
        assert not any(word in prompt for word in ("笔记", "write_note", "append_note", "new_context", "切窗"))
    assert "new_context" in PROMPTS[2][1]


def test_source_paging_preserves_all_text_and_context_policy():
    corpus = "真实源代码\n" * 40_000
    tool = ReviewSources(corpus, "next stage")
    context = ContextManager(256_000, 16_384)
    tool.agent = SimpleNamespace(context=context, messages=[], _estimated_context_tokens=lambda: 206_500)
    collected = []
    while tool.offsets["initial"] < len(corpus):
        page = json.loads(tool.execute())
        collected.append(page["text"])
        assert page["next_offset"] - page["offset"] == len(page["text"])
    assert "".join(collected) == corpus
    assert context.input_budget_tokens == 239_616
    assert context.base_limit == 223_232
    assert tool.offsets["extended"] == 0


def test_explicit_success_cannot_mask_absent_autonomous_notes_or_reminder():
    report = {"session_id": "same-session", "requests": [
        {"index": 1, "stage": "system_only", "reminded": False, "stop_reason": "max_tokens",
         "calls": [{"name": "write_note"}]},
        {"index": 2, "stage": "budget", "reminded": False, "calls": [{"name": "new_context"}]},
        {"index": 3, "stage": "explicit", "reminded": False,
         "calls": [{"name": n} for n in ("write_note", "new_context", "read_note", "read_history")]},
    ], "stages": [{"stage": "explicit", "answer": "done"}]}
    result = observations(report)
    assert not result["system_stage_note_call"]
    assert not result["budget_reminder_seen_by_model"]
    assert not result["budget_new_context_after_reminder"]
    assert result["explicit_note_call"]
    assert result["explicit_read_history_call"]
