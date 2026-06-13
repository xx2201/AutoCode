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
WORKSPACE = ROOT / "tmp" / "redis_work"
OUTPUT = ROOT / "tmp" / "redis_work_multiturn_run.md"

PROMPTS = [
    (
        "Turn 1",
        (
            "Read only the files you actually need, starting with app.py, session_store.py, templates/base.html, "
            "and templates/home.html. "
            "Add an /admin page that only allows alice (user_id == 1). "
            "Anonymous users should still be redirected to /login, and logged-in non-admin users "
            "should get a clear 403 page. Show an Admin link on the home page only for alice. "
            "Add pytest coverage with FastAPI TestClient using a fake in-memory Redis stub instead of "
            "the real server. Verify by running pytest."
        ),
    ),
    (
        "Turn 2",
        (
            "Now improve session behavior: when an authenticated user visits /home, refresh the Redis TTL "
            "and refresh the cookie max_age to SESSION_TTL. Update README.md to document both the new /admin "
            "demo and the session refresh behavior. Re-run pytest when done."
        ),
    ),
]


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


def _pytest_run() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["pytest", "-q"],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def main():
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
    token_streams: dict[str, list[str]] = {label: [] for label, _ in PROMPTS}
    turn_results: list[dict] = []
    current_turn = ""

    def on_tool(name, kwargs):
        tool_events.append({"turn": current_turn, "tool": name, "arguments": kwargs})

    def capture_hook(event: str, payload: dict):
        hook_events.append(
            {
                "turn": current_turn,
                "event": event,
                "payload": _trim_payload(payload),
            }
        )

    def approval_handler(pending):
        approvals.append(
            {
                "turn": current_turn,
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
                "turn": current_turn,
                "system": (system_message or "")[:12000],
                "messages": _trim_payload({"messages": history_messages}, max_value_chars=1200)["messages"],
                "tools": _trim_payload({"tools": tools or []}, max_value_chars=1200)["tools"],
                "response": _trim_payload({
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
                }, max_value_chars=1200),
            }
        )
        return response

    llm.chat = wrapped_chat

    before_files = {
        "README.md": _read_text(WORKSPACE / "README.md"),
        "app.py": _read_text(WORKSPACE / "app.py"),
        "session_store.py": _read_text(WORKSPACE / "session_store.py"),
        "templates/home.html": _read_text(WORKSPACE / "templates" / "home.html"),
        "PROJECT_MEMORY.md": _read_text(WORKSPACE / ".corecoder" / "PROJECT_MEMORY.md"),
    }

    redis_ping = subprocess.run(
        [
            "python",
            "-c",
            "import redis;print(redis.Redis(host='localhost', port=6379, db=0, decode_responses=True).ping())",
        ],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    for label, prompt in PROMPTS:
        current_turn = label

        def on_token(text: str, *, _label=label):
            token_streams[_label].append(text)

        final_response = agent.chat(
            prompt,
            on_token=on_token,
            on_tool=on_tool,
            approval_handler=approval_handler,
        )

        task_id = agent.task_state.task_id if agent.task_state else ""
        directory = task_dir(task_id) if task_id else None
        pytest_result = _pytest_run()
        turn_results.append(
            {
                "turn": label,
                "prompt": prompt,
                "task_id": task_id,
                "task_dir": str(directory) if directory else "",
                "final_response": final_response,
                "pytest_returncode": pytest_result.returncode,
                "pytest_stdout": pytest_result.stdout,
                "pytest_stderr": pytest_result.stderr,
                "checkpoint": _read_json(directory / "checkpoint.json") if directory else None,
                "task": _read_json(directory / "task.json") if directory else None,
                "trace": load_trace(task_id) if task_id else None,
                "audit": load_events(task_id) if task_id else [],
                "transcript": load_transcript_entries(task_id) if task_id else [],
                "project_memory": _read_text(WORKSPACE / ".corecoder" / "PROJECT_MEMORY.md"),
            }
        )

    after_files = {
        "README.md": _read_text(WORKSPACE / "README.md"),
        "app.py": _read_text(WORKSPACE / "app.py"),
        "session_store.py": _read_text(WORKSPACE / "session_store.py"),
        "templates/home.html": _read_text(WORKSPACE / "templates" / "home.html"),
        "templates/admin.html": _read_text(WORKSPACE / "templates" / "admin.html"),
        "templates/forbidden.html": _read_text(WORKSPACE / "templates" / "forbidden.html"),
        "tests/test_app.py": _read_text(WORKSPACE / "tests" / "test_app.py"),
        "PROJECT_MEMORY.md": _read_text(WORKSPACE / ".corecoder" / "PROJECT_MEMORY.md"),
        "PROJECT_CHALLENGES.md": _read_text(WORKSPACE / "PROJECT_CHALLENGES.md"),
    }

    lines: list[str] = []
    lines.append("# CoreCoder 在 redis_work 上的多轮真实运行过程")
    lines.append("")
    lines.append("这份文档不是手工模拟。")
    lines.append("它通过包装 `llm.chat(...)`、订阅 `HookBus`、记录 `on_tool`、自动审批，并在每轮任务结束后回读真实落盘文件，展示一次多轮用户对话下 agent 的完整执行过程。")
    lines.append("")
    lines.append("## 运行配置")
    lines.append("")
    lines.append(f"- Model: `{config.model}`")
    lines.append(f"- Base URL: `{config.base_url}`")
    lines.append(f"- Workspace: `{WORKSPACE}`")
    lines.append(f"- Redis ping(stdout): `{redis_ping.stdout.strip()}`")
    lines.append(f"- Redis ping(returncode): `{redis_ping.returncode}`")
    lines.append("")
    lines.append("## 运行前关键文件")
    lines.append("")
    for name, content in before_files.items():
        lines.append(f"### {name}")
        lines.append("```text")
        lines.append(content.rstrip())
        lines.append("```")
        lines.append("")

    lines.append("## 用户多轮对话")
    lines.append("")
    for result in turn_results:
        lines.append(f"### {result['turn']}")
        lines.append("```text")
        lines.append(result["prompt"])
        lines.append("```")
        lines.append("")

    lines.append("## LLM 每轮真实输入输出")
    lines.append("")
    for index, round_data in enumerate(rounds, start=1):
        lines.append(f"## Round {index} ({round_data['turn']})")
        lines.append("")
        lines.append("### system")
        lines.append("```text")
        lines.append((round_data["system"] or "").rstrip())
        lines.append("```")
        lines.append("")
        lines.append("### messages")
        lines.append("```json")
        lines.append(_json(round_data["messages"], 16000))
        lines.append("```")
        lines.append("")
        lines.append("### tools")
        lines.append("```json")
        lines.append(_json(round_data["tools"], 12000))
        lines.append("```")
        lines.append("")
        lines.append("### model_output")
        lines.append("```json")
        lines.append(_json(round_data["response"], 12000))
        lines.append("```")
        lines.append("")

    lines.append("## Hook 事件时间线")
    lines.append("")
    lines.append("```json")
    lines.append(_json(hook_events, 40000))
    lines.append("```")
    lines.append("")
    lines.append("## 实际工具调用顺序")
    lines.append("")
    lines.append("```json")
    lines.append(_json(tool_events, 20000))
    lines.append("```")
    lines.append("")
    lines.append("## 审批处理记录")
    lines.append("")
    lines.append("```json")
    lines.append(_json(approvals, 12000))
    lines.append("```")
    lines.append("")

    for result in turn_results:
        lines.append(f"## {result['turn']} 结果")
        lines.append("")
        lines.append(f"- Task ID: `{result['task_id']}`")
        lines.append(f"- Task dir: `{result['task_dir']}`")
        lines.append(f"- pytest returncode: `{result['pytest_returncode']}`")
        lines.append("")
        lines.append("### token 流式输出拼接结果")
        lines.append("```text")
        lines.append("".join(token_streams[result["turn"]]).rstrip())
        lines.append("```")
        lines.append("")
        lines.append("### 最终回复")
        lines.append("```text")
        lines.append(result["final_response"].rstrip())
        lines.append("```")
        lines.append("")
        lines.append("### pytest stdout")
        lines.append("```text")
        lines.append(result["pytest_stdout"].rstrip())
        lines.append("```")
        lines.append("")
        lines.append("### pytest stderr")
        lines.append("```text")
        lines.append(result["pytest_stderr"].rstrip())
        lines.append("```")
        lines.append("")
        lines.append("### checkpoint.json")
        lines.append("```json")
        lines.append(_json(result["checkpoint"], 16000))
        lines.append("```")
        lines.append("")
        lines.append("### task.json")
        lines.append("```json")
        lines.append(_json(result["task"], 8000))
        lines.append("```")
        lines.append("")
        lines.append("### trace.json")
        lines.append("```json")
        lines.append(_json(result["trace"], 8000))
        lines.append("```")
        lines.append("")
        lines.append("### audit.jsonl (parsed)")
        lines.append("```json")
        lines.append(_json(result["audit"], 20000))
        lines.append("```")
        lines.append("")
        lines.append("### transcript.jsonl (parsed)")
        lines.append("```json")
        lines.append(_json(result["transcript"], 20000))
        lines.append("```")
        lines.append("")
        lines.append("### PROJECT_MEMORY.md after this turn")
        lines.append("```text")
        lines.append(result["project_memory"].rstrip())
        lines.append("```")
        lines.append("")

    lines.append("## 运行后关键文件")
    lines.append("")
    for name, content in after_files.items():
        lines.append(f"### {name}")
        lines.append("```text")
        lines.append(content.rstrip())
        lines.append("```")
        lines.append("")

    OUTPUT.write_text("\n".join(lines), encoding="utf-8")
    print(OUTPUT)


if __name__ == "__main__":
    main()
