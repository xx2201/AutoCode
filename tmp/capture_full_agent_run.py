import json
import subprocess
from pathlib import Path

from corecoder.agent import Agent
from corecoder.config import Config
from corecoder.llm import LLM, LiteLLM
from corecoder.state.checkpoint import task_dir
from corecoder.state.journal import load_events
from corecoder.state.trace import load_trace
from corecoder.state.transcript import load_transcript_entries


ROOT = Path(r"G:/mycode/CoreCoder")
WORKSPACE = ROOT / "tmp" / "agent_demo"
OUTPUT = ROOT / "tmp" / "full_agent_run.md"
INITIAL_MAIN = """from utils import halper


def main() -> None:
    print(halper("world"))


if __name__ == "__main__":
    main()
"""
INITIAL_UTILS = """def helper(name: str) -> str:
    return f"hello, {name}"
"""


def _json(data, max_chars: int | None = None) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if max_chars and len(text) > max_chars:
        return text[:max_chars] + f"\n... truncated ({len(text)} chars total) ..."
    return text


def _read_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _read_text(path: Path, max_chars: int | None = None) -> str:
    if not path.exists():
        return "(missing)"
    text = path.read_text(encoding="utf-8", errors="replace")
    if max_chars and len(text) > max_chars:
        return text[:max_chars] + f"\n... truncated ({len(text)} chars total) ..."
    return text


def _trim_payload(payload: dict, max_value_chars: int = 800) -> dict:
    def trim(value):
        if isinstance(value, str) and len(value) > max_value_chars:
            return value[:max_value_chars] + f"... ({len(value)} chars total)"
        if isinstance(value, list):
            return [trim(v) for v in value]
        if isinstance(value, dict):
            return {k: trim(v) for k, v in value.items()}
        return value

    return trim(payload)


def main():
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "main.py").write_text(INITIAL_MAIN, encoding="utf-8")
    (WORKSPACE / "utils.py").write_text(INITIAL_UTILS, encoding="utf-8")

    config = Config.from_env()
    llm_cls = LiteLLM if config.provider == "litellm" else LLM
    llm = llm_cls(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )

    agent = Agent(
        llm=llm,
        max_context_tokens=config.max_context_tokens,
        workspace_root=str(WORKSPACE),
        auto_approve=False,
    )

    original_chat = llm.chat
    rounds: list[dict] = []
    hook_events: list[dict] = []
    tool_events: list[dict] = []
    approvals: list[dict] = []
    token_stream: list[str] = []

    def on_tool(name, kwargs):
        tool_events.append({"tool": name, "arguments": kwargs})

    def on_token(text: str):
        token_stream.append(text)

    def capture_hook(event: str, payload: dict):
        hook_events.append(
            {
                "event": event,
                "payload": _trim_payload(payload),
            }
        )

    def approval_handler(pending):
        approvals.append(
            {
                "tool_call_id": pending.tool_call_id,
                "tool_name": pending.tool_name,
                "arguments": pending.arguments,
                "reason": pending.reason,
                "requires_manual": pending.requires_manual,
                "decision": "approve",
            }
        )
        return "approve"

    for event_name in (
        "user_message",
        "before_llm",
        "after_llm",
        "policy_decision",
        "before_tool",
        "after_tool",
        "approval_resolved",
        "task_status",
        "task_error",
        "todo_updated",
    ):
        agent.hooks.on(event_name, capture_hook)

    def wrapped_chat(messages, tools=None, on_token=None):
        system_message = None
        history_messages = messages
        if messages and messages[0].get("role") == "system":
            system_message = messages[0]["content"]
            history_messages = messages[1:]

        response = original_chat(messages=messages, tools=tools, on_token=on_token)
        rounds.append(
            {
                "system": system_message,
                "messages": history_messages,
                "tools": tools,
                "response": {
                    "content": response.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "name": tc.name,
                            "arguments": tc.arguments,
                        }
                        for tc in response.tool_calls
                    ],
                    "prompt_tokens": response.prompt_tokens,
                    "completion_tokens": response.completion_tokens,
                },
            }
        )
        return response

    llm.chat = wrapped_chat

    before_main = (WORKSPACE / "main.py").read_text(encoding="utf-8")
    before_utils = (WORKSPACE / "utils.py").read_text(encoding="utf-8")

    prompt = (
        "Read the files in the current workspace, fix the broken import, "
        "then run python main.py to verify the fix. Keep the change minimal."
    )
    final_response = agent.chat(
        prompt,
        on_token=on_token,
        on_tool=on_tool,
        approval_handler=approval_handler,
    )

    after_main = (WORKSPACE / "main.py").read_text(encoding="utf-8")
    after_utils = (WORKSPACE / "utils.py").read_text(encoding="utf-8")

    verify = subprocess.run(
        ["python", "main.py"],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    task_id = agent.task_state.task_id if agent.task_state else ""
    directory = task_dir(task_id) if task_id else None
    checkpoint_data = _read_json(directory / "checkpoint.json") if directory else None
    task_data = _read_json(directory / "task.json") if directory else None
    trace_data = load_trace(task_id) if task_id else None
    audit_events = load_events(task_id) if task_id else []
    transcript_entries = load_transcript_entries(task_id) if task_id else []

    lines: list[str] = []
    lines.append("# CoreCoder 完整运行过程抓取")
    lines.append("")
    lines.append("这份文档不是手工模拟。")
    lines.append("它通过包装 `llm.chat(...)`、订阅 `HookBus` 事件、记录 `on_tool`、自动处理审批，并在任务结束后读取真实落盘文件，完整展示一次任务从输入到持久化的全过程。")
    lines.append("")
    lines.append("## 运行配置")
    lines.append("")
    lines.append(f"- Model: `{config.model}`")
    lines.append(f"- Base URL: `{config.base_url}`")
    lines.append(f"- Workspace: `{WORKSPACE}`")
    lines.append(f"- Prompt: `{prompt}`")
    lines.append(f"- Task ID: `{task_id}`")
    lines.append(f"- Task dir: `{directory}`")
    lines.append("")
    lines.append("## 运行前文件")
    lines.append("")
    lines.append("### main.py")
    lines.append("```python")
    lines.append(before_main.rstrip())
    lines.append("```")
    lines.append("")
    lines.append("### utils.py")
    lines.append("```python")
    lines.append(before_utils.rstrip())
    lines.append("```")
    lines.append("")
    lines.append("## LLM 每轮真实输入输出")
    lines.append("")

    for index, round_data in enumerate(rounds, start=1):
        lines.append(f"## Round {index}")
        lines.append("")
        lines.append("### system")
        lines.append("```text")
        lines.append((round_data["system"] or "").rstrip())
        lines.append("```")
        lines.append("")
        lines.append("### messages")
        lines.append("```json")
        lines.append(_json(round_data["messages"], 12000))
        lines.append("```")
        lines.append("")
        lines.append("### tools")
        lines.append("```json")
        lines.append(_json(round_data["tools"], 8000))
        lines.append("```")
        lines.append("")
        lines.append("### model_output")
        lines.append("```json")
        lines.append(_json(round_data["response"], 8000))
        lines.append("```")
        lines.append("")

    lines.append("## Hook 事件时间线")
    lines.append("")
    lines.append("```json")
    lines.append(_json(hook_events, 30000))
    lines.append("```")
    lines.append("")
    lines.append("## 实际工具调用顺序")
    lines.append("")
    lines.append("```json")
    lines.append(_json(tool_events, 12000))
    lines.append("```")
    lines.append("")
    lines.append("## 审批处理记录")
    lines.append("")
    lines.append("```json")
    lines.append(_json(approvals, 8000))
    lines.append("```")
    lines.append("")
    lines.append("## token 流式输出拼接结果")
    lines.append("")
    lines.append("```text")
    lines.append("".join(token_stream).rstrip())
    lines.append("```")
    lines.append("")
    lines.append("## 最终回复")
    lines.append("")
    lines.append("```text")
    lines.append(final_response.rstrip())
    lines.append("```")
    lines.append("")
    lines.append("## 落盘文件摘要")
    lines.append("")
    lines.append("### checkpoint.json")
    lines.append("```json")
    lines.append(_json(checkpoint_data, 20000))
    lines.append("```")
    lines.append("")
    lines.append("### task.json")
    lines.append("```json")
    lines.append(_json(task_data, 8000))
    lines.append("```")
    lines.append("")
    lines.append("### trace.json")
    lines.append("```json")
    lines.append(_json(trace_data, 8000))
    lines.append("```")
    lines.append("")
    lines.append("### audit.jsonl (parsed)")
    lines.append("```json")
    lines.append(_json(audit_events, 25000))
    lines.append("```")
    lines.append("")
    lines.append("### transcript.jsonl (parsed)")
    lines.append("```json")
    lines.append(_json(transcript_entries, 25000))
    lines.append("```")
    lines.append("")
    lines.append("## 运行后文件")
    lines.append("")
    lines.append("### main.py")
    lines.append("```python")
    lines.append(after_main.rstrip())
    lines.append("```")
    lines.append("")
    lines.append("### utils.py")
    lines.append("```python")
    lines.append(after_utils.rstrip())
    lines.append("```")
    lines.append("")
    lines.append("## 验证命令结果")
    lines.append("")
    lines.append(f"- returncode: `{verify.returncode}`")
    lines.append("")
    lines.append("### stdout")
    lines.append("```text")
    lines.append(verify.stdout.rstrip())
    lines.append("```")
    lines.append("")
    lines.append("### stderr")
    lines.append("```text")
    lines.append(verify.stderr.rstrip())
    lines.append("```")
    lines.append("")
    lines.append("## 对应源码位置")
    lines.append("")
    lines.append("- Agent 主循环：[G:/mycode/CoreCoder/corecoder/agent/loop.py](G:/mycode/CoreCoder/corecoder/agent/loop.py:1)")
    lines.append("- 模型调用：[G:/mycode/CoreCoder/corecoder/llm.py](G:/mycode/CoreCoder/corecoder/llm.py:1)")
    lines.append("- 运行时执行与 hook：[G:/mycode/CoreCoder/corecoder/runtime/engine.py](G:/mycode/CoreCoder/corecoder/runtime/engine.py:1)")
    lines.append("- 安全策略：[G:/mycode/CoreCoder/corecoder/runtime/policy.py](G:/mycode/CoreCoder/corecoder/runtime/policy.py:1)")
    lines.append("- 文件工具：[G:/mycode/CoreCoder/corecoder/tools/read.py](G:/mycode/CoreCoder/corecoder/tools/read.py:1)、[G:/mycode/CoreCoder/corecoder/tools/edit.py](G:/mycode/CoreCoder/corecoder/tools/edit.py:1)")
    lines.append("- Checkpoint：[G:/mycode/CoreCoder/corecoder/state/checkpoint.py](G:/mycode/CoreCoder/corecoder/state/checkpoint.py:1)")
    lines.append("- Trace：[G:/mycode/CoreCoder/corecoder/state/trace.py](G:/mycode/CoreCoder/corecoder/state/trace.py:1)")
    lines.append("- Audit Journal：[G:/mycode/CoreCoder/corecoder/state/journal.py](G:/mycode/CoreCoder/corecoder/state/journal.py:1)")
    lines.append("- Transcript：[G:/mycode/CoreCoder/corecoder/state/transcript.py](G:/mycode/CoreCoder/corecoder/state/transcript.py:1)")
    lines.append("")

    OUTPUT.write_text("\n".join(lines), encoding="utf-8")
    print(OUTPUT)


if __name__ == "__main__":
    main()
