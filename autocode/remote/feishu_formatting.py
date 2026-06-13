"""Formatting helpers for Feishu remote control."""

from __future__ import annotations

import json
from pathlib import Path

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


def build_image_content(image_key: str) -> str:
    return json.dumps({"image_key": image_key}, ensure_ascii=False)


def build_file_content(file_key: str, file_name: str) -> str:
    return json.dumps({"file_key": file_key, "file_name": file_name}, ensure_ascii=False)


def split_text_chunks(text: str) -> list[str]:
    return split_message(text, limit=_FEISHU_TEXT_LIMIT)


def render_text_result(result: RemoteTurnResult) -> list[str]:
    return split_text_chunks(render_turn_result(result))


def build_live_status_card(
    *,
    title: str,
    phase: str,
    status: str,
    session_id: str,
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
        f"Session: `{session_id or 'starting...'}`",
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
        f"Session: `{result.session_id}`",
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
            _button("Approve", "approve", session_key, owner_open_id, result.session_id, "primary"),
            _button("Approve All", "approve_all", session_key, owner_open_id, result.session_id, "default"),
            _button("Reject", "reject", session_key, owner_open_id, result.session_id, "danger"),
        ],
    )


def build_error_card(title: str, text: str) -> dict:
    return _card(
        title=title,
        template="red",
        markdown=_clip_markdown(text.strip() or "Unknown error"),
        buttons=[],
    )


def build_resume_card(
    tasks: list[dict],
    session_key: str,
    owner_open_id: str,
    workspace_root: str,
) -> dict:
    workspace_name = Path(workspace_root).name or workspace_root
    elements: list[dict] = [{
        "tag": "markdown",
        "content": (
            f"Project: `{_clip_markdown(workspace_name, limit=80)}`\n"
            f"Showing the latest `{len(tasks)}` resumable sessions. Selecting one replaces the current chat context."
        ),
    }]
    for item in tasks:
        title = _clip_markdown(item.get("title") or item["session_id"], limit=100)
        elements.append({
            "tag": "markdown",
            "content": (
                f"**{title}**\n"
                f"Session: `{item['session_id']}`\n"
                f"Task: `{item.get('task_id') or '(none)'}`\n"
                f"Status: `{item['status']}`\n"
                f"Updated: `{item['saved_at']}`"
            ),
        })
        elements.append(
            {
                "tag": "column_set",
                "columns": [{
                    "tag": "column",
                    "elements": [_button("Resume", "resume", session_key, owner_open_id, item["session_id"], "primary")],
                }],
            }
        )
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Resume Session"},
            "template": "blue",
        },
        "body": {"elements": elements},
    }


def _button(
    label: str,
    action: str,
    session_key: str,
    owner_open_id: str,
    session_id: str,
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
            "session_id": session_id,
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
