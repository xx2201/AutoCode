"""Opt-in live provider evaluation: python -m eval.task_memory --runs 3.

Only synthetic diagnostics are sent. Session artifacts are isolated under eval/runs.
No shell/file-write tools, project memory writes, or external tracing are enabled.
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from autocode.agent import Agent
from autocode.config import Config
from autocode.context.manager import ContextManager
from autocode.llm import llm_class_for_provider
from autocode.state import SessionState, TurnState, load_checkpoint, load_transcript_entries, new_session_id, new_turn_id
from autocode.state import checkpoint as checkpoints
from autocode.tools.history import SearchHistoryTool, ReadHistoryTool


def run_trial(config, directory, index):
    llm = llm_class_for_provider(config.provider)(
        model=config.model, api_key=config.api_key, base_url=config.base_url,
        temperature=config.temperature, max_tokens=config.max_tokens,
    )
    workspace = directory / f"trial-{index}"
    workspace.mkdir()
    original_chat = llm.chat
    notes_calls = []

    def record_chat(messages, **kwargs):
        response = original_chat(messages=messages, **kwargs)
        if "TASK_NOTES_DELTA" in str(messages[0]):
            notes_calls.append({"response": response.content, "stop_reason": response.stop_reason})
            (workspace / "notes-responses.json").write_text(
                json.dumps(notes_calls, ensure_ascii=False, indent=2), encoding="utf-8")
        return response

    llm.chat = record_chat
    agent = Agent(llm, tools=[SearchHistoryTool(), ReadHistoryTool()], workspace_root=str(workspace),
                  sandbox_mode="read-only", approval_policy="never", max_rounds=8)
    agent.session_state = SessionState(session_id=new_session_id(), current_turn=TurnState(
        turn_id=new_turn_id(), status="completed", title="Synthetic diagnostic fixture"))
    case = f"EVAL-CASE-{index}-{uuid.uuid4().hex[:6]}"
    exact = "PREAUTH-CLOSE-" + uuid.uuid4().hex[:12]
    command = [sys.executable, "-c", "print('fixture log start\\n' * 80); print('" + case
               + " approach A failed BEFORE authentication; exact code " + exact
               + "'); print('fixture log end\\n' * 80)"]
    output = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    agent._append_message({"role": "user", "message_kind": "prompt", "content":
                           "Investigate this synthetic connection fixture. Preserve the goal; do not retry A without new evidence."})
    agent._append_message({"role": "assistant", "content": "Run local diagnostic fixture", "tool_calls": [{
        "id": "fixture-probe", "type": "function", "function": {"name": "shell_command", "arguments": json.dumps({"command": command})}}]})
    agent._append_message({"role": "tool", "tool_call_id": "fixture-probe", "tool_name": "shell_command", "content": output})
    evidence_id = agent.messages[-1]["message_id"]
    for number in range(8):
        agent._append_message({"role": "assistant", "content": f"Synthetic progress {number}: " + "unimportant detail; " * 35})
    # 缩小应用层阈值来真实触发切窗，不伪造 provider token usage。
    agent.context = ContextManager(max_tokens=3000)
    first = agent.compact_context()
    first_notes = agent._task_notes().load()
    assert first.compressed and first_notes["notes"], "first notes checkpoint missing"
    assert exact not in str([m for m in agent.messages if m.get("message_kind") != "task_context"])
    agent._append_message({"role": "assistant", "content": "New evidence: approach B reconnect behavior has not yet been tested; it remains pending."})
    for number in range(5):
        agent._append_message({"role": "assistant", "content": f"Later synthetic stage {number}; " + "no additional diagnostic evidence; " * 30})
    second = agent.compact_context()
    second_notes = agent._task_notes().load()
    assert second.compressed and second_notes["cursor"] > first_notes["cursor"]
    assert first.after_tokens < first.before_tokens and second.after_tokens < second.before_tokens
    session_id = agent.session_state.session_id
    restored = Agent(llm, tools=[SearchHistoryTool(), ReadHistoryTool()], workspace_root=str(workspace),
                     sandbox_mode="read-only", approval_policy="never", max_rounds=8)
    try:
        restored.restore_session(*load_checkpoint(session_id))
        assert restored._task_notes().load() == second_notes
        answer = restored.chat(
            f"这是历史召回验收。必须先调用 search_history 搜索 {case}，再调用 read_history 读取原始诊断工具输出。"
            "不能只引用 notes。回答 A 在认证前还是认证后失败、日志中的 exact code，并引用原始 message_id。"
            "只报告历史结果，不宣称当前测试已通过；不要执行新的诊断。"
        )
        entries = load_transcript_entries(session_id)
        tool_messages = [entry["message"] for entry in entries if entry.get("kind") == "message" and entry["message"].get("role") == "tool"]
        names = [message.get("tool_name") for message in tool_messages]
        assert "search_history" in names and "read_history" in names, "retrieval tools were not both used"
        reads = [json.loads(message["content"]) for message in tool_messages if message.get("tool_name") == "read_history"]
        assert any(result.get("message_id") == evidence_id and exact in result.get("text", "")
                   for result in reads), "read_history did not return the original diagnostic evidence"
        assert exact in answer, "exact omitted evidence was not recovered"
        assert evidence_id in answer, "original source was not cited"
        assert "认证前" in answer or "before" in answer.lower(), "failure phase is incorrect"
        return {"trial": index, "passed": True, "session_id": session_id,
                "first_tokens": [first.before_tokens, first.after_tokens],
                "second_tokens": [second.before_tokens, second.after_tokens],
                "notes_count": len(second_notes["notes"]), "tools": names,
                "evidence_id": evidence_id, "answer": answer,
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
    output = Path(tempfile.mkdtemp(prefix="task-memory-", dir=directory)).resolve()
    checkpoints.SESSIONS_DIR = output / "sessions"
    report = {"model": config.model, "provider": config.provider, "runs": [], "output": str(output)}
    print(json.dumps({"model": config.model, "provider": config.provider, "output": str(output)}, ensure_ascii=False), flush=True)
    for index in range(args.runs):
        started = time.monotonic()
        try:
            result = run_trial(config, output, index)
        except Exception as exc:
            # Provider exceptions may contain endpoint details; do not print them.
            result = {"trial": index, "passed": False, "error_type": type(exc).__name__}
            if isinstance(exc, (AssertionError, ValueError)):
                result["error"] = str(exc)
        result["seconds"] = round(time.monotonic() - started, 2)
        report["runs"].append(result)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in result.items() if key != "answer"}, ensure_ascii=False), flush=True)
    raise SystemExit(0 if all(run["passed"] for run in report["runs"]) else 1)


if __name__ == "__main__":
    main()
