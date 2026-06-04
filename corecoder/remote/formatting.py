"""Formatting helpers for chat-based remote control."""

from __future__ import annotations

from .manager import RemoteTurnResult

_TELEGRAM_MAX_LENGTH = 4096


def render_turn_result(result: RemoteTurnResult) -> str:
    parts = [result.text.strip() or "(no response)"]
    if result.task_id:
        parts.append(
            f"\nTask: {result.task_id}\n"
            f"Status: {result.status or 'unknown'}\n"
            f"Approve-all: {'on' if result.auto_approve_for_task else 'off'}"
        )
    if result.pending_tool:
        command_block = ""
        if result.pending_tool == "bash" and result.pending_arguments:
            command = result.pending_arguments.get("command", "")
            if command:
                command_block = f"- Command: {command}\n"
        parts.append(
            "\nApproval needed:\n"
            f"- Tool: {result.pending_tool}\n"
            f"{command_block}"
            f"- Reason: {result.pending_reason or 'confirmation required'}\n"
            "Reply with /approve, /approve-all, or /reject."
        )
    return "\n".join(part for part in parts if part).strip()


def render_task_list(tasks: list[dict]) -> str:
    if not tasks:
        return "No saved task checkpoints."
    lines = []
    for item in tasks:
        lines.append(
            f"- {item['task_id']} | {item['status']} | step {item['step_index']} | "
            f"{item['model']} | {item['saved_at']}"
        )
    return "\n".join(lines)


def split_message(text: str, limit: int = _TELEGRAM_MAX_LENGTH) -> list[str]:
    text = text.strip()
    if not text:
        return ["(empty)"]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
