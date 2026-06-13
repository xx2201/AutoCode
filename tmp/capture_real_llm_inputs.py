import json
import subprocess
from pathlib import Path

from corecoder.agent import Agent
from corecoder.config import Config
from corecoder.llm import LLM, LiteLLM


ROOT = Path(r"G:/mycode/CoreCoder")
WORKSPACE = ROOT / "tmp" / "agent_demo"
OUTPUT = ROOT / "tmp" / "real_llm_rounds.md"


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


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
        auto_approve=True,
    )

    original_chat = llm.chat
    rounds: list[dict] = []
    tool_events: list[dict] = []

    def on_tool(name, kwargs):
        tool_events.append({"tool": name, "arguments": kwargs})

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

    prompt = "Read the files in the current workspace, fix the broken import, then run python main.py to verify the fix. Keep the change minimal."
    final_response = agent.chat(prompt, on_tool=on_tool)

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

    lines: list[str] = []
    lines.append("# CoreCoder 真实模型输入输出抓取")
    lines.append("")
    lines.append("这份文档不是手工模拟，而是通过临时包装 `llm.chat(...)`，把真实运行时每一轮送给模型的输入和模型输出抓下来。")
    lines.append("")
    lines.append("## 运行配置")
    lines.append("")
    lines.append(f"- Model: `{config.model}`")
    lines.append(f"- Base URL: `{config.base_url}`")
    lines.append(f"- Workspace: `{WORKSPACE}`")
    lines.append(f"- Prompt: `{prompt}`")
    lines.append(f"- Task ID: `{agent.task_state.task_id if agent.task_state else ''}`")
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
    lines.append("## 每轮真实注入与输出")
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
        lines.append(_json(round_data["messages"]))
        lines.append("```")
        lines.append("")
        lines.append("### tools")
        lines.append("```json")
        lines.append(_json(round_data["tools"]))
        lines.append("```")
        lines.append("")
        lines.append("### model_output")
        lines.append("```json")
        lines.append(_json(round_data["response"]))
        lines.append("```")
        lines.append("")

    lines.append("## 实际工具调用顺序")
    lines.append("")
    lines.append("```json")
    lines.append(_json(tool_events))
    lines.append("```")
    lines.append("")
    lines.append("## 最终回复")
    lines.append("")
    lines.append("```text")
    lines.append(final_response.rstrip())
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

    OUTPUT.write_text("\n".join(lines), encoding="utf-8")
    print(OUTPUT)


if __name__ == "__main__":
    main()
