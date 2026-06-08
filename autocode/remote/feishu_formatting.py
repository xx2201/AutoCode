"""Formatting helpers for Feishu remote control."""

from __future__ import annotations

import json

from .formatting import render_turn_result, split_message
from .manager import RemoteTurnResult

_FEISHU_TEXT_LIMIT = 3000


def parse_text_content(content: str) -> str:
    if not content:
        return ""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return content.strip()
    text = payload.get("text", "")
    return str(text).strip()


def build_text_content(text: str) -> str:
    return json.dumps({"text": text}, ensure_ascii=False)


def split_text_chunks(text: str) -> list[str]:
    return split_message(text, limit=_FEISHU_TEXT_LIMIT)


def render_text_result(result: RemoteTurnResult) -> list[str]:
    return split_text_chunks(render_turn_result(result))


def build_live_status_card(
    *,
    title: str,
    phase: str,
    status: str,
    task_id: str,
    step_index: int,
    llm_calls: int,
    tool_calls: int,
    prompt_tokens: int,
    completion_tokens: int,
    last_tool: str = "",
    detail: str = "",
    auto_approve_for_task: bool = False,
    template: str = "blue",
) -> dict:
    lines = [
        f"**{_clip_markdown(title or '(untitled task)')}**",
        "",
        f"Phase: `{phase}`",
        f"Status: `{status or 'running'}`",
        f"Task: `{task_id or 'starting...'}`",
        f"Step: `{step_index}`",
        f"LLM Calls: `{llm_calls}`",
        f"Tool Calls: `{tool_calls}`",
        f"Prompt Tokens: `{prompt_tokens}`",
        f"Completion Tokens: `{completion_tokens}`",
        f"Approve_all: `{'on' if auto_approve_for_task else 'off'}`",
    ]
    if last_tool:
        lines.append(f"Last Tool: `{last_tool}`")
    if detail:
        lines.extend(["", _clip_markdown(detail, limit=800)])
    return _card(title="AutoCode Live", template=template, markdown="\n".join(lines), buttons=[])


def build_approval_card(
    result: RemoteTurnResult,
    session_key: str,
    owner_open_id: str,
) -> dict:
    command = ""
    if result.pending_tool == "bash" and result.pending_arguments:
        command = result.pending_arguments.get("command", "")

    lines = [
        _clip_markdown(result.text.strip() or "(no response)"),
        "",
        f"Task: `{result.task_id}`",
        f"Status: `{result.status or 'unknown'}`",
        f"Approve_all: `{'on' if result.auto_approve_for_task else 'off'}`",
        f"Tool: `{result.pending_tool or 'unknown'}`",
        f"Reason: {result.pending_reason or 'confirmation required'}",
    ]
    if command:
        lines.extend(["", "**Command**", f"```bash\n{command}\n```"])
    if result.pending_requires_manual:
        lines.extend(["", "⚠ This is marked as a high-risk command and still requires manual approval."])

    return _card(
        title="AutoCode Approval",
        template="orange" if not result.pending_requires_manual else "red",
        markdown="\n".join(lines),
        buttons=[
            _button("Approve", "approve", session_key, owner_open_id, result.task_id, "primary"),
            _button("Approve All", "approve_all", session_key, owner_open_id, result.task_id, "default"),
            _button("Reject", "reject", session_key, owner_open_id, result.task_id, "danger"),
        ],
    )


def build_action_status_card(task_id: str, action: str) -> dict:
    return _card(
        title="Approval Submitted",
        template="blue",
        markdown=f"Task: `{task_id or 'unknown'}`\nAction: `{action}`\nAutoCode is continuing this task.",
        buttons=[],
    )


def build_error_card(title: str, text: str) -> dict:
    return _card(
        title=title,
        template="red",
        markdown=_clip_markdown(text.strip() or "Unknown error"),
        buttons=[],
    )


def _button(
    label: str,
    action: str,
    session_key: str,
    owner_open_id: str,
    task_id: str,
    style: str,
) -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": style,
        "value": {
            "command": action,
            "session_key": session_key,
            "owner_open_id": owner_open_id,
            "task_id": task_id,
        },
    }


def _card(title: str, template: str, markdown: str, buttons: list[dict]) -> dict:
    elements: list[dict] = [{"tag": "markdown", "content": markdown}]
    if buttons:
        elements.append(
            {
                "tag": "column_set",
                "columns": [{"tag": "column", "elements": [button]} for button in buttons],
            }
        )
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "body": {"elements": elements},
    }


def _clip_markdown(text: str, limit: int = 2400) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n... truncated ..."
