"""Per-session exact LLM round capture."""

from __future__ import annotations

import json
import threading
import time

from .checkpoint import session_dir


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _split_system_message(messages: list[dict]) -> tuple[str, list[dict]]:
    if messages and messages[0].get("role") == "system":
        return str(messages[0].get("content", "")), messages[1:]
    return "", messages


def _response_payload(
    *,
    content: str,
    tool_calls: list[dict],
    prompt_tokens: int,
    completion_tokens: int,
) -> dict:
    return {
        "content": content,
        "tool_calls": tool_calls,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }


class LLMRoundRecorder:
    """Persist the exact LLM inputs/outputs for each session round."""

    def __init__(self):
        self._lock = threading.Lock()

    def append_round(
        self,
        session_id: str,
        *,
        task_id: str,
        step_index: int,
        model: str,
        messages: list[dict],
        tools: list[dict],
        response_content: str,
        response_tool_calls: list[dict],
        prompt_tokens: int,
        completion_tokens: int,
    ):
        directory = session_dir(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": _now(),
            "session_id": session_id,
            "task_id": task_id,
            "step_index": step_index,
            "model": model,
            "messages": messages,
            "tools": tools,
            "response": _response_payload(
                content=response_content,
                tool_calls=response_tool_calls,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ),
        }
        jsonl_path = directory / "llm_rounds.jsonl"
        md_path = directory / "llm_rounds.md"
        with self._lock:
            with jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            rounds = _load_entries_from_path(jsonl_path)
            md_path.write_text(render_llm_rounds_markdown(rounds), encoding="utf-8")


def load_llm_round_entries(session_id: str) -> list[dict]:
    return _load_entries_from_path(session_dir(session_id) / "llm_rounds.jsonl")


def _load_entries_from_path(path) -> list[dict]:
    if not path.exists():
        return []

    rounds = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rounds.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rounds


def render_llm_rounds_markdown(rounds: list[dict]) -> str:
    lines: list[str] = []
    lines.append("# LLM Rounds")
    lines.append("")
    lines.append("这份文件由运行时自动生成，记录每一轮真实送入模型的输入和模型返回，不是事后重建。")
    lines.append("")

    for index, round_data in enumerate(rounds, start=1):
        system_content, history_messages = _split_system_message(list(round_data.get("messages", [])))
        lines.append(f"## Round {index}")
        lines.append("")
        lines.append(f"- Session: `{round_data.get('session_id', '')}`")
        lines.append(f"- Task: `{round_data.get('task_id', '')}`")
        lines.append(f"- Step: `{round_data.get('step_index', 0)}`")
        lines.append(f"- Time: `{round_data.get('timestamp', '')}`")
        lines.append(f"- Model: `{round_data.get('model', '')}`")
        lines.append("")
        if system_content:
            lines.append("### system")
            lines.append("```text")
            lines.append(system_content.rstrip())
            lines.append("```")
            lines.append("")
        lines.append("### messages")
        lines.append("```json")
        lines.append(_json(history_messages))
        lines.append("```")
        lines.append("")
        lines.append("### tools")
        lines.append("```json")
        lines.append(_json(round_data.get("tools", [])))
        lines.append("```")
        lines.append("")
        lines.append("### model_output")
        lines.append("```json")
        lines.append(_json(round_data.get("response", {})))
        lines.append("```")
        lines.append("")

    if not rounds:
        lines.append("暂无 LLM 轮次记录。")
        lines.append("")

    return "\n".join(lines)
