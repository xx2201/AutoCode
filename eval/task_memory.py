"""Opt-in live context-window evaluation. Synthetic data only; no tracing or workspace tools."""

import argparse
import json
import tempfile
import time
import uuid
from pathlib import Path

from autocode.agent import Agent
from autocode.config import Config
from autocode.context.manager import ContextManager
from autocode.llm import llm_class_for_provider
from autocode.state import load_checkpoint, load_transcript_entries
from autocode.state import checkpoint as checkpoints
from autocode.tools.history import SearchHistoryTool, ReadHistoryTool, ListHistoryTool
from autocode.tools.notes import WriteNoteTool, AppendNoteTool, ReadNoteTool, ListNotesTool, SearchNotesTool, NewContextTool

TOOLS = [SearchHistoryTool, ReadHistoryTool, ListHistoryTool, WriteNoteTool, AppendNoteTool,
         ReadNoteTool, ListNotesTool, SearchNotesTool, NewContextTool]


def run_trial(config, directory, index):
    llm = llm_class_for_provider(config.provider)(
        model=config.model, api_key=config.api_key, base_url=config.base_url,
        temperature=config.temperature, max_tokens=4096)
    workspace = directory / f"trial-{index}"
    workspace.mkdir()
    def create():
        return Agent(llm, tools=[tool() for tool in TOOLS], workspace_root=str(workspace),
                     max_context_tokens=config.max_context_tokens, max_output_tokens=4096,
                     sandbox_mode="read-only", approval_policy="never", max_rounds=16)
    agent, restored = create(), create()
    switch_notes = []
    agent.hooks.on("context_compaction", lambda event, payload: switch_notes.append(agent._task_notes().load()))
    case = f"CASE-{index}-{uuid.uuid4().hex[:8]}"
    exact = "PREAUTH-" + uuid.uuid4().hex[:12]
    requests = []
    original_chat = llm.chat
    def record_chat(messages, **kwargs):
        assert "TASK_NOTES_DELTA" not in str(messages)
        response = original_chat(messages=messages, **kwargs)
        requests.append({"tool_names": [call.name for call in response.tool_calls],
                         "stop_reason": response.stop_reason})
        return response
    llm.chat = record_chat
    try:
        first = agent.chat(
            "这是隔离验收，只处理以下合成日志，不执行外部操作。按顺序完成："
            "1. 调用 write_note 写 index.md，只写目标、案例标识、需要回查历史，不要写错误码或完整日志。"
            "2. 调用 new_context 一次。3. 新窗口调用 read_note 读取 index.md，"
            "然后 search_history 和 read_history 找回原始日志。4. 回答准确错误码和来源 message_id，然后结束。"
            f"合成日志：{case}: approach A failed BEFORE authentication; exact code {exact}."
        )
        session_id = agent.session_state.session_id
        entries = load_transcript_entries(session_id)
        messages = [e["message"] for e in entries if e.get("kind") == "message"]
        calls = [m.get("tool_name") for m in messages if m.get("role") == "tool"]
        assert agent.session_state.context_window >= 1, "model did not switch context"
        assert all(name in calls for name in ["write_note", "new_context", "read_note", "read_history"]), calls
        assert "search_history" in calls or "list_history" in calls, "no history discovery tool used"
        notes = agent._task_notes().load()
        assert notes["files"], "model did not persist notes"
        assert switch_notes and exact not in str(switch_notes[0]), "exact code was already in notes before first recovery"
        assert exact in first, "first-window recovery missed exact code"
        evidence = next(m["message_id"] for m in messages if m.get("message_kind") == "prompt" and exact in m["content"])
        assert evidence in first, "missing original source citation"
        restore_case = "RESTORE-" + case
        restore_exact = "PREAUTH-" + uuid.uuid4().hex[:12]
        agent._append_message({"role": "assistant", "content":
                               f"Synthetic second fixture: {restore_case} failed BEFORE authentication; exact code {restore_exact}"})
        restore_evidence = agent.messages[-1]["message_id"]
        # 第二次由程序硬边界触发；只缩小应用窗口，不伪造 provider usage。
        before_padding = agent._estimated_context_tokens()
        agent.context = ContextManager(before_padding + 3000)
        agent._append_message({"role": "assistant", "content": "synthetic padding " * 1000})
        assert before_padding < agent.context.input_budget_tokens <= agent._estimated_context_tokens()
        forced = agent._maybe_compress_messages()
        assert forced.compressed, "hard limit did not reset"
        assert restore_exact not in str(agent.messages), "old evidence leaked into fresh window"
        assert restore_exact not in str(agent._task_notes().load()), "second fixture leaked into notes"
        agent.persist_session()
        restored.restore_session(*load_checkpoint(session_id))
        assert restored._task_notes().load() == notes
        answer = restored.chat(
            f"恢复后继续验收。先读 index.md，再查找 {restore_case} 并读取对应的 Synthetic second fixture 原始消息，"
            "不是第一个案例。报告错误码、认证前还是认证后、来源 message_id。只报告历史结果。")
        assert restore_exact in answer and restore_evidence in answer, "restart recovery failed"
        assert "认证前" in answer or "before" in answer.lower()
        all_entries = load_transcript_entries(session_id)
        read_results = [json.loads(e["message"]["content"]) for e in all_entries
                        if e.get("kind") == "message" and e["message"].get("tool_name") == "read_history"
                        and e["message"].get("role") == "tool" and e["message"]["content"].startswith("{")]
        assert any(r.get("message_id") == evidence and exact in r.get("text", "") for r in read_results)
        assert any(r.get("message_id") == restore_evidence and restore_exact in r.get("text", "") for r in read_results)
        return {"trial": index, "passed": True, "session_id": session_id,
                "windows": restored.session_state.context_window, "tools": calls, "requests": requests,
                "evidence_id": evidence, "exact": exact, "answer": answer,
                "notes_exclude_exact_before_switch": True, "forced_reset": forced.compressed,
                "restore_evidence_id": restore_evidence,
                "hard_input_limit": agent.context.input_budget_tokens,
                "usage_before_padding": before_padding,
                "prompt_tokens": llm.total_prompt_tokens, "completion_tokens": llm.total_completion_tokens}
    finally:
        agent.close(shutdown_observability=False)
        restored.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.runs <= 20:
        parser.error("runs must be 1-20")
    config = Config.from_env()
    directory = Path("eval/runs")
    directory.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="context-windows-", dir=directory)).resolve()
    checkpoints.SESSIONS_DIR = output / "sessions"
    report = {"model": config.model, "provider": config.provider, "max_output_tokens": 4096,
              "runs": [], "output": str(output)}
    print(json.dumps({k: v for k, v in report.items() if k != "runs"}), flush=True)
    for index in range(args.runs):
        started = time.monotonic()
        try:
            result = run_trial(config, output, index)
        except Exception as exc:
            result = {"trial": index, "passed": False, "error_type": type(exc).__name__}
            if isinstance(exc, (AssertionError, ValueError)):
                result["error"] = str(exc)
        result["seconds"] = round(time.monotonic() - started, 2)
        report["runs"].append(result)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in result.items() if k not in {"answer", "requests"}}, ensure_ascii=False), flush=True)
    raise SystemExit(0 if all(r["passed"] for r in report["runs"]) else 1)


if __name__ == "__main__":
    main()
